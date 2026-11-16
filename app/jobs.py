"""Фоновые задачи: импорт из Яндекса, сопоставление, перенос в Spotify.

Одновременно работает одна задача. Всё состояние лежит в SQLite, поэтому
любую задачу можно остановить и запустить снова: сделанное не повторяется.
"""
from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime

from . import db, matcher, spotify_client as sp, yandex_client

_state_lock = threading.Lock()
_state = {"name": None, "running": False, "message": "", "error": None, "done": 0, "total": 0,
          "finished_at": None}
_stop = threading.Event()


def state() -> dict:
    with _state_lock:
        return dict(_state)


def _set(**kw) -> None:
    with _state_lock:
        _state.update(kw)


def stop() -> None:
    _stop.set()


def _run(name: str, fn) -> bool:
    with _state_lock:
        if _state["running"]:
            return False
        _state.update(name=name, running=True, message="Запуск…", error=None, done=0, total=0,
                      finished_at=None)
    _stop.clear()

    def wrapper():
        try:
            fn()
        except sp.QuotaPaused as e:
            _set(error=f"{e.reason}. Продолжить можно после {e.until:%d.%m %H:%M}. "
                       "Просто нажмите кнопку ещё раз в это время, работа продолжится с того же места.")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _set(error=str(e) or e.__class__.__name__)
        finally:
            _set(running=False, finished_at=datetime.now().strftime("%H:%M:%S"))

    threading.Thread(target=wrapper, daemon=True, name=f"job-{name}").start()
    return True


# --- 1. Импорт ---------------------------------------------------------------

def start_import() -> bool:
    def work():
        _set(message="Подключаюсь к Яндекс Музыке…")
        total = yandex_client.fetch_liked(progress=lambda m: _set(message=m))
        db.save_source(db.MY_LIKES, {"title": "Мне нравится", "kind": "likes",
                                     "playlist_name": db.source(db.MY_LIKES).get("playlist_name")
                                     or "Яндекс Музыка: Мне нравится"})
        db.set_active(db.MY_LIKES)
        _set(message=f"Готово: загружено {total} лайкнутых треков", done=total, total=total)
    return _run("import", work)


def start_import_link(url: str) -> bool:
    def work():
        link = yandex_client.parse_link(url)
        key = yandex_client.link_key(link)
        title, total = yandex_client.fetch_link(url, progress=lambda m: _set(message=m))
        db.save_source(key, {"title": title, "url": url, "kind": "link",
                             "playlist_name": db.source(key).get("playlist_name") or f"Яндекс Музыка: {title}"})
        db.set_active(key)
        _set(message=f"Готово: из «{title}» загружено {total} треков", done=total, total=total)
    return _run("import", work)


def start_import_my_playlists(kinds: list[int]) -> bool:
    def work():
        done_titles = []
        _set(total=len(kinds), done=0)
        for i, kind in enumerate(kinds, 1):
            if _stop.is_set():
                break
            key, title, n = yandex_client.fetch_my_playlist(
                kind, progress=lambda m, i=i: _set(message=f"Плейлист {i} из {len(kinds)}: {m}"))
            db.save_source(key, {"title": title, "kind": "my_playlist",
                                 "playlist_name": db.source(key).get("playlist_name") or title})
            done_titles.append(f"{title} ({n})")
            _set(done=i)
        _set(message="Загружены плейлисты: " + ", ".join(done_titles))
    return _run("import", work)


# --- 2. Сопоставление --------------------------------------------------------

def _source(row) -> matcher.Source:
    return matcher.Source(
        title=row["title"], version=row["version"], artists=json.loads(row["artists"] or "[]"),
        album=row["album"], duration_ms=row["duration_ms"],
        isrc=row["isrc"] if "isrc" in row.keys() else None,
    )


def match_one(row) -> None:
    src = _source(row)
    best: dict[str, tuple[float, dict]] = {}
    # Экономия квоты: сначала один точный запрос; запасные (транслитерация, свободный поиск)
    # делаем только если точный не дал уверенного совпадения.
    for i, q in enumerate(matcher.build_queries(src)):
        for cand in sp.search_tracks(q):
            sc = matcher.score(src, cand)
            if cand.get("uri") and (cand["uri"] not in best or best[cand["uri"]][0] < sc):
                best[cand["uri"]] = (sc, cand)
        top = max((v[0] for v in best.values()), default=0.0)
        if top >= matcher.AUTO_THRESHOLD:
            break
        if i >= 1 and top >= matcher.GOOD_ENOUGH:
            # почти уверены, а ещё один запрос вряд ли что-то изменит: отдаём на проверку
            break
    ranked = sorted(best.values(), key=lambda x: x[0], reverse=True)[:5]
    top_score = ranked[0][0] if ranked else 0.0
    status = matcher.classify(top_score)
    top = ranked[0][1] if ranked else None
    db.execute(
        "UPDATE tracks SET status=?, score=?, candidates=?, spotify_uri=?, spotify_label=? "
        "WHERE source_key=? AND yandex_id=?",
        (
            status, top_score,
            json.dumps([matcher.candidate_view(c, s) for s, c in ranked], ensure_ascii=False),
            top["uri"] if status == "auto" else None,
            f"{', '.join(a['name'] for a in top['artists'])} — {top['name']}" if status == "auto" else None,
            row["source_key"], row["yandex_id"],
        ),
    )


def _match_key(row) -> str:
    """Один и тот же трек в Яндексе бывает под разными id (сингл, альбом, сборник)."""
    artists = json.loads(row["artists"] or "[]")
    full = f"{row['title']} ({row['version']})" if row["version"] else row["title"]
    return matcher.normalize(artists[0] if artists else "") + "|" + matcher.normalize(full)


_COPY_COLS = ("status", "score", "candidates", "spotify_uri", "spotify_label")


def _copy_match(src_row, row) -> None:
    db.execute(
        f"UPDATE tracks SET {', '.join(c + '=?' for c in _COPY_COLS)} WHERE source_key=? AND yandex_id=?",
        tuple(src_row[c] for c in _COPY_COLS) + (row["source_key"], row["yandex_id"]),
    )


def _match_rows(rows) -> bool:
    """Ищет переданные треки. Возвращает False, если остановлено."""
    # уже решённые треки: их результат переиспользуем для дублей без запросов к Spotify
    known = {_match_key(r): r for r in db.query(
        "SELECT * FROM tracks WHERE status IN ('auto','manual','review') ORDER BY status='review'")}
    _set(total=len(rows), done=0, message=f"Ищу в Spotify: 0 из {len(rows)}")
    for i, row in enumerate(rows, 1):
        if _stop.is_set():
            _set(message=f"Остановлено. Обработано {i - 1} из {len(rows)}")
            return False
        key = _match_key(row)
        if key in known and (known[key]["source_key"], known[key]["yandex_id"]) != \
                (row["source_key"], row["yandex_id"]):
            _copy_match(known[key], row)
        else:
            match_one(row)
            fresh = db.query("SELECT * FROM tracks WHERE source_key=? AND yandex_id=?",
                             (row["source_key"], row["yandex_id"]))[0]
            if fresh["status"] in ("auto", "manual", "review"):
                known.setdefault(key, fresh)
        _set(done=i, message=f"Ищу в Spotify: {i} из {len(rows)} (запросов сегодня: {sp.requests_today()})")
    return True


def start_match(rematch_not_found: bool = False) -> bool:
    def work():
        statuses = ("new", "not_found") if rematch_not_found else ("new",)
        # ищем по всем загруженным источникам; текущий — первым
        rows = db.query(
            f"SELECT * FROM tracks WHERE status IN ({','.join('?' * len(statuses))}) "
            "ORDER BY source_key<>?, source_key, position",
            statuses + (db.active(),),
        )
        if not _match_rows(rows):
            return
        c = db.counts("*")
        _set(message=f"Готово (все источники). Автоматически: {c['auto']}, на проверку: {c['review']}, "
                     f"не найдено: {c['not_found']}")
    return _run("match", work)


# --- 3. Перенос --------------------------------------------------------------

def _mark(col: str, key: str, chunk) -> None:
    db.execute(
        f"UPDATE tracks SET {col}=1 WHERE source_key=? AND yandex_id IN ({','.join('?' * len(chunk))})",
        (key,) + tuple(r["yandex_id"] for r in chunk),
    )


def _transfer_source(key: str, to_library: bool, to_playlist: bool, playlist_name: str | None) -> bool:
    """Переносит один источник. Возвращает False, если остановлено."""
    meta = db.source(key)
    title = meta.get("title") or key
    if to_library:
        # Лайки добавляем от самого старого к самому свежему, чтобы в Spotify
        # сохранился порядок «новые сверху».
        rows = db.query(
            "SELECT yandex_id, spotify_uri FROM tracks WHERE source_key=? AND status IN ('auto','manual') "
            "AND spotify_uri IS NOT NULL AND saved_library=0 ORDER BY position DESC", (key,))
        _set(total=len(rows), done=0, message=f"«{title}» → «Любимые треки»: 0 из {len(rows)}")
        for i in range(0, len(rows), sp.LIBRARY_BATCH):
            if _stop.is_set():
                _set(message="Остановлено")
                return False
            chunk = rows[i:i + sp.LIBRARY_BATCH]
            sp.save_to_library([r["spotify_uri"] for r in chunk])
            _mark("saved_library", key, chunk)
            _set(done=i + len(chunk), message=f"«{title}» → «Любимые треки»: {i + len(chunk)} из {len(rows)}")

    if to_playlist:
        name = (playlist_name or meta.get("playlist_name") or f"Яндекс Музыка: {title}").strip()
        pl = meta.get("playlist") or {}
        if not pl.get("id") or pl.get("name") != name:
            pl = {"id": sp.create_playlist(name, "Перенесено из Яндекс Музыки"), "name": name}
            db.save_source(key, {"playlist": pl, "playlist_name": name})
            db.execute("UPDATE tracks SET in_playlist=0 WHERE source_key=?", (key,))
        # В плейлисте порядок как в Яндексе: сверху то, что было сверху.
        rows = db.query(
            "SELECT yandex_id, spotify_uri FROM tracks WHERE source_key=? AND status IN ('auto','manual') "
            "AND spotify_uri IS NOT NULL AND in_playlist=0 ORDER BY position ASC", (key,))
        _set(total=len(rows), done=0, message=f"«{title}» → плейлист «{name}»: 0 из {len(rows)}")
        for i in range(0, len(rows), sp.PLAYLIST_BATCH):
            if _stop.is_set():
                _set(message="Остановлено")
                return False
            chunk = rows[i:i + sp.PLAYLIST_BATCH]
            # один и тот же трек Spotify мог найтись для двух версий из Яндекса
            added = {r["spotify_uri"] for r in db.query(
                "SELECT spotify_uri FROM tracks WHERE source_key=? AND in_playlist=1 AND spotify_uri IS NOT NULL",
                (key,))}
            uris = []
            for r in chunk:
                if r["spotify_uri"] not in added:
                    added.add(r["spotify_uri"])
                    uris.append(r["spotify_uri"])
            if uris:
                sp.add_to_playlist(pl["id"], uris)
            _mark("in_playlist", key, chunk)
            _set(done=i + len(chunk), message=f"«{title}» → плейлист «{name}»: {i + len(chunk)} из {len(rows)}")
    return True


def start_transfer(to_library: bool, to_playlist: bool, playlist_name: str, key: str | None = None) -> bool:
    key = key or db.active()

    def work():
        db.save_source(key, {"last_transfer": {"to_library": to_library, "to_playlist": to_playlist}})
        if not _transfer_source(key, to_library, to_playlist, playlist_name):
            return
        c = db.counts(key)
        _set(message=f"Перенос завершён. В «Любимых»: {c['saved_library']}, в плейлисте: {c['in_playlist']}")
    return _run("transfer", work)


def start_sync() -> bool:
    """Только новые лайки с прошлого раза: загрузить → найти → перенести как в прошлый раз."""
    key = db.MY_LIKES

    def work():
        before = {r["yandex_id"] for r in db.query("SELECT yandex_id FROM tracks WHERE source_key=?", (key,))}
        _set(message="Проверяю новые лайки в Яндексе…")
        yandex_client.fetch_liked(progress=lambda m: _set(message=m))
        db.save_source(key, {"title": "Мне нравится", "kind": "likes"})
        db.set_active(key)
        after = {r["yandex_id"] for r in db.query("SELECT yandex_id FROM tracks WHERE source_key=?", (key,))}
        new_ids = after - before
        rows = db.query("SELECT * FROM tracks WHERE source_key=? AND status='new' ORDER BY position", (key,))
        if rows and not _match_rows(rows):
            return
        meta = db.source(key)
        opts = meta.get("last_transfer") or {"to_library": True, "to_playlist": bool(meta.get("playlist"))}
        if not _transfer_source(key, opts.get("to_library", True), opts.get("to_playlist", False), None):
            return
        c = db.counts(key)
        stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
        db.save_source(key, {"last_sync": {"at": stamp, "new": len(new_ids)}})
        _set(message=f"Синхронизация готова: новых лайков {len(new_ids)}. На проверку: {c['review']}, "
                     f"не найдено: {c['not_found']}. Всё найденное уже в Spotify.")
    return _run("sync", work)


def start_transfer_all_playlists() -> bool:
    """Каждый загруженный плейлист (кроме моих лайков) — в свой плейлист Spotify."""
    def work():
        keys = [k for k in db.sources() if k != db.MY_LIKES]
        for n, key in enumerate(keys, 1):
            if not _transfer_source(key, False, True, None):
                return
        _set(message=f"Перенесено плейлистов: {len(keys)}")
    return _run("transfer", work)
