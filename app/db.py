"""SQLite-хранилище: треки, кэш поиска, настройки и счётчики квоты."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "transfer.db"

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    source_key     TEXT NOT NULL DEFAULT 'my_likes',  -- откуда трек: мои лайки или плейлист
    yandex_id      TEXT NOT NULL,
    position       INTEGER NOT NULL,          -- 0 = самый свежий лайк
    liked_at       TEXT,
    title          TEXT NOT NULL,
    version        TEXT,
    artists        TEXT NOT NULL,             -- JSON-список
    album          TEXT,
    year           INTEGER,
    duration_ms    INTEGER,
    status         TEXT NOT NULL DEFAULT 'new',  -- new | auto | review | manual | not_found | skipped
    spotify_uri    TEXT,
    spotify_label  TEXT,                      -- "Артист — Название" найденного трека
    score          REAL,
    candidates     TEXT,                      -- JSON-список лучших кандидатов
    saved_library  INTEGER NOT NULL DEFAULT 0,
    in_playlist    INTEGER NOT NULL DEFAULT 0,
    isrc           TEXT,
    PRIMARY KEY (source_key, yandex_id)
);
CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
CREATE TABLE IF NOT EXISTS search_cache (
    query    TEXT PRIMARY KEY,
    response TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
            _migrate(_conn)
        return _conn


def _kv_raw(c: sqlite3.Connection, key: str):
    row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row and row[0] is not None else None


def _migrate(c: sqlite3.Connection) -> None:
    """Приводит старую базу к новой схеме, не теряя данные."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(tracks)")}
    if "isrc" not in cols:
        c.execute("ALTER TABLE tracks ADD COLUMN isrc TEXT")
    if "source_key" not in cols:
        # v1 → v2: одна таблица на один источник → несколько источников (лайки + плейлисты)
        old_source = _kv_raw(c, "source") or {}
        key = old_source.get("key") or "my_likes"
        c.execute("BEGIN")
        c.execute("ALTER TABLE tracks RENAME TO tracks_v1")
        c.execute(SCHEMA.split("CREATE INDEX")[0].strip().rstrip(";"))
        cols_v1 = [r[1] for r in c.execute("PRAGMA table_info(tracks_v1)")]
        names = ", ".join(cols_v1)
        c.execute(f"INSERT INTO tracks(source_key, {names}) SELECT ?, {names} FROM tracks_v1", (key,))
        c.execute("DROP TABLE tracks_v1")
        meta = {
            "title": old_source.get("title") or "Мне нравится",
            "playlist_name": old_source.get("playlist_name") or "Яндекс Музыка: Мне нравится",
        }
        if old_source.get("url"):
            meta["url"] = old_source["url"]
        old_playlist = _kv_raw(c, "playlist")
        if old_playlist:
            meta["playlist"] = old_playlist
        has_rows = c.execute("SELECT 1 FROM tracks LIMIT 1").fetchone()
        if has_rows or old_source:
            c.execute("INSERT OR REPLACE INTO kv(key, value) VALUES('sources', ?)",
                      (json.dumps({key: meta}, ensure_ascii=False),))
            c.execute("INSERT OR REPLACE INTO kv(key, value) VALUES('active_source', ?)", (json.dumps(key),))
        c.execute("DELETE FROM kv WHERE key IN ('source', 'playlist')")
        c.execute("COMMIT")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tracks_source ON tracks(source_key, status)")


def execute(sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
    with _lock:
        return conn().execute(sql, params)


def query(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, params).fetchall()


# --- key/value ---------------------------------------------------------------

def get(key: str, default: Any = None) -> Any:
    rows = query("SELECT value FROM kv WHERE key=?", (key,))
    if not rows or rows[0]["value"] is None:
        return default
    return json.loads(rows[0]["value"])


def put(key: str, value: Any) -> None:
    execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def delete(key: str) -> None:
    execute("DELETE FROM kv WHERE key=?", (key,))


# --- кэш поиска --------------------------------------------------------------

def _cache_key(q: str) -> str:
    # регистр и лишние пробелы не влияют на ответ Spotify, поэтому не плодим лишние записи
    return " ".join((q or "").lower().split())


def cache_get(q: str) -> Any | None:
    rows = query("SELECT response FROM search_cache WHERE query IN (?, ?)", (_cache_key(q), q))
    return json.loads(rows[0]["response"]) if rows else None


def cache_put(q: str, response: Any) -> None:
    execute(
        "INSERT OR REPLACE INTO search_cache(query, response) VALUES(?, ?)",
        (_cache_key(q), json.dumps(response, ensure_ascii=False)),
    )


# --- статистика --------------------------------------------------------------

def counts(source_key: str | None = None) -> dict[str, int]:
    """Статистика по источнику (по умолчанию — по текущему). source_key='*' — по всем."""
    key = active() if source_key is None else source_key
    where, params = ("", ()) if key == "*" else ("WHERE source_key=?", (key,))
    out = {s: 0 for s in ("new", "auto", "review", "manual", "not_found", "skipped")}
    for r in query(f"SELECT status, COUNT(*) AS n FROM tracks {where} GROUP BY status", params):
        out[r["status"]] = r["n"]
    out["total"] = sum(out.values())
    out["matched"] = out["auto"] + out["manual"]
    and_ = "AND" if where else "WHERE"
    out["saved_library"] = query(f"SELECT COUNT(*) AS n FROM tracks {where} {and_} saved_library=1", params)[0]["n"]
    out["in_playlist"] = query(f"SELECT COUNT(*) AS n FROM tracks {where} {and_} in_playlist=1", params)[0]["n"]
    return out


# --- источники (мои лайки, плейлисты) ---------------------------------------

MY_LIKES = "my_likes"


def sources() -> dict[str, dict]:
    return get("sources", {}) or {}


def source(key: str | None = None) -> dict:
    return sources().get(key or active(), {})


def save_source(key: str, meta: dict) -> None:
    allsrc = sources()
    allsrc[key] = {**allsrc.get(key, {}), **meta}
    put("sources", allsrc)


def remove_source(key: str) -> None:
    allsrc = sources()
    allsrc.pop(key, None)
    put("sources", allsrc)
    execute("DELETE FROM tracks WHERE source_key=?", (key,))
    if active() == key:
        put("active_source", next(iter(allsrc), MY_LIKES))


def active() -> str:
    return get("active_source") or MY_LIKES


def set_active(key: str) -> None:
    put("active_source", key)
