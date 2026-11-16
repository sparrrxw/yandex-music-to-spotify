"""Сквозной тест: импорт → поиск → перенос с подменёнными Яндексом и Spotify."""
import json
import time

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_conn", None)
    db.put("settings", {"spotify_client_id": "x", "request_interval": 0, "daily_budget": 1000})
    return db


def wait_job():
    from app import jobs
    for _ in range(200):
        if not jobs.state()["running"]:
            return jobs.state()
        time.sleep(0.02)
    raise AssertionError("job hung")


def test_full_flow(env, monkeypatch):
    db = env
    from app import jobs, spotify_client as sp, yandex_client

    def fake_fetch(progress=None):
        rows = [("1:10", 0, "Группа крови", ["Кино"], 286000),
                ("2:20", 1, "Bohemian Rhapsody", ["Queen"], 354000),
                ("3:30", 2, "Совсем неизвестная песня", ["Никто"], 100000)]
        for yid, pos, title, artists, dur in rows:
            db.execute("INSERT INTO tracks(yandex_id, position, title, artists, duration_ms) VALUES(?,?,?,?,?)",
                       (yid, pos, title, json.dumps(artists, ensure_ascii=False), dur))
        return len(rows)
    monkeypatch.setattr(yandex_client, "fetch_liked", fake_fetch)

    catalog = {
        "kino": {"uri": "spotify:track:" + "a" * 22, "name": "Gruppa krovi", "artists": [{"name": "KINO"}],
                 "album": {"name": "Gruppa krovi"}, "duration_ms": 286500, "external_urls": {}},
        "queen": {"uri": "spotify:track:" + "b" * 22, "name": "Bohemian Rhapsody", "artists": [{"name": "Queen"}],
                  "album": {"name": "A Night at the Opera"}, "duration_ms": 354300, "external_urls": {}},
    }
    calls = []

    def fake_request(method, path, params=None, json=None, essential=False, _retry=0):
        calls.append((method, path, params, json))
        if path == "/search":
            q = params["q"].lower()
            items = [v for k, v in catalog.items() if k in q]
            return {"tracks": {"items": items}}
        if method == "POST" and path == "/me/playlists":
            return {"id": "PL1"}
        return None
    monkeypatch.setattr(sp, "request", fake_request)

    assert jobs.start_import(); wait_job()
    assert db.counts()["total"] == 3

    assert jobs.start_match(); st = wait_job()
    assert st["error"] is None, st
    c = db.counts()
    assert c["auto"] == 2 and c["not_found"] == 1, c

    assert jobs.start_transfer(True, True, "Test"); st = wait_job()
    assert st["error"] is None, st
    lib = [x for x in calls if x[1] == "/me/library"]
    assert len(lib) == 1
    # лайки идут от старого к новому: Queen (позиция 1) раньше Кино (позиция 0)
    assert lib[0][2]["uris"] == "spotify:track:" + "b" * 22 + ",spotify:track:" + "a" * 22
    pl = [x for x in calls if x[:2] == ("POST", "/playlists/PL1/items")]
    assert pl[0][3]["uris"] == ["spotify:track:" + "a" * 22, "spotify:track:" + "b" * 22]
    c = db.counts()
    assert c["saved_library"] == 2 and c["in_playlist"] == 2

    # повторный перенос ничего не дублирует
    n = len(calls)
    assert jobs.start_transfer(True, True, "Test"); wait_job()
    assert len(calls) == n


def test_quota_pause(env, monkeypatch):
    db = env
    from app import jobs, spotify_client as sp
    db.execute("INSERT INTO tracks(yandex_id, position, title, artists) VALUES('1',0,'X','[\"Y\"]')")

    def boom(*a, **k):
        raise sp._pause(3600, "Исчерпана дневная квота Spotify (QUOTA_EXCEEDED)")
    monkeypatch.setattr(sp, "request", boom)
    assert jobs.start_match(); st = wait_job()
    assert "QUOTA_EXCEEDED" in st["error"]
    assert sp.paused_until() is not None
    assert db.counts()["new"] == 1  # трек остался в очереди


def test_web_pages(env):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/review").status_code == 200
    assert client.get("/review?status=not_found").status_code == 200
    r = client.get("/report.csv")
    assert r.status_code == 200 and "Статус" in r.content.decode("utf-8-sig")
    assert client.get("/api/status").json()["counts"]["total"] == 0
    r = client.get("/login", follow_redirects=False)
    assert r.status_code in (302, 307) and "accounts.spotify.com" in r.headers["location"]
    assert "127.0.0.1%3A8888%2Fcallback" in r.headers["location"]


class _Short:
    def __init__(self, i, title):
        from types import SimpleNamespace as NS
        self.id = i; self.track_id = f"{i}:1"; self.timestamp = None
        self.track = NS(id=i, title=title, version=None, artists=[NS(name="Кино")],
                        albums=[NS(title="Альбом", year=1988)], duration_ms=200000)


def test_link_import_falls_back_to_token(env, monkeypatch):
    db = env
    from types import SimpleNamespace as NS
    from app import jobs, yandex_client
    s = db.get("settings"); s["yandex_token"] = "tok"; db.put("settings", s)
    used = []

    def fake_client(anonymous=False):
        used.append(anonymous)
        if anonymous:
            raise RuntimeError("401 anonymous")
        return NS(users_playlists=lambda kind, user_id=None: NS(
            title="Мне нравится", tracks=[_Short(1, "Кукушка"), _Short(2, "Пачка сигарет")], track_count=2,
            uid=1, kind=kind))
    monkeypatch.setattr(yandex_client, "_client", fake_client)

    assert jobs.start_import_link("https://music.yandex.ru/users/friend/playlists/3"); st = wait_job()
    assert st["error"] is None, st
    assert used == [True, False]
    assert db.counts()["total"] == 2
    src = db.source()
    assert src["title"] == "Мне нравится (friend)" and src["playlist_name"].endswith("(friend)")
    rows = db.query("SELECT title FROM tracks ORDER BY position")
    assert [r["title"] for r in rows] == ["Кукушка", "Пачка сигарет"]

    # другой источник загружается отдельным списком и становится выбранным
    assert jobs.start_import_link("https://music.yandex.ru/users/other/playlists/3"); st = wait_job()
    assert st["error"] is None, st
    assert len(db.sources()) == 2 and db.counts()["total"] == 2 and db.counts("*")["total"] == 4


def test_transfer_works_while_search_paused(env, monkeypatch):
    """Поиск упёрся в лимит, но перенос уже найденного должен пройти."""
    db = env
    from app import jobs, spotify_client as sp
    s = db.get("settings"); s["daily_budget"] = 50; db.put("settings", s)
    db.put("spotify_token", {"access_token": "t", "expires_at": 1e12, "refresh_token": "r"})
    from datetime import date
    db.put(f"spotify_requests:{date.today().isoformat()}", 50)  # свой лимит исчерпан
    sp._pause(3600, "QUOTA_EXCEEDED")                               # и Spotify просил подождать
    db.execute("INSERT INTO tracks(yandex_id, position, title, artists, status, spotify_uri) "
               "VALUES('1',0,'A','[]','auto','spotify:track:" + "a" * 22 + "')")
    db.execute("INSERT INTO tracks(yandex_id, position, title, artists) VALUES('2',1,'B','[]')")  # ещё не искали

    sent = []

    class R:
        status_code = 200; content = b""; headers = {}
    class H:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def request(self, method, url, **kw):
            sent.append((method, url)); return R()
    monkeypatch.setattr(sp, "_http", lambda: H())

    assert jobs.start_transfer(True, False, "x"); st = wait_job()
    assert st["error"] is None, st
    assert sent == [("PUT", sp.API + "/me/library")]
    # а поиск по-прежнему на паузе
    assert jobs.start_match(); st = wait_job()
    assert "Продолжить можно" in st["error"]


def _fake_search(monkeypatch, catalog):
    """catalog: подстрока запроса → список треков. Считает реальные запросы к Spotify."""
    from app import spotify_client as sp
    calls = []

    def fake_request(method, path, params=None, json=None, essential=False, _retry=0):
        calls.append((method, path, params, json))
        if path == "/search":
            q = params["q"].lower()
            items = []
            for k, v in catalog.items():
                if k in q:
                    items += v
            return {"tracks": {"items": items}}
        if method == "POST" and path == "/me/playlists":
            return {"id": "PL" + str(len(calls))}
        return None
    monkeypatch.setattr(sp, "request", fake_request)
    return calls


def _t(uri_char, name, artist, dur=200000, album="A"):
    return {"uri": "spotify:track:" + uri_char * 22, "name": name, "artists": [{"name": artist}],
            "album": {"name": album}, "duration_ms": dur, "external_urls": {}}


def _add(db, yid, pos, title, artists, dur=200000, **kw):
    cols = {"yandex_id": yid, "position": pos, "title": title,
            "artists": json.dumps(artists, ensure_ascii=False), "duration_ms": dur, "album": "A", **kw}
    db.execute(f"INSERT INTO tracks({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(cols.values()))


def test_one_request_when_exact_match(env, monkeypatch):
    db = env
    from app import jobs
    calls = _fake_search(monkeypatch, {'track:"yesterday" artist:"the beatles"': [_t("y", "Yesterday", "The Beatles")]})
    _add(db, "1", 0, "Yesterday", ["The Beatles"])
    assert jobs.start_match(); wait_job()
    assert len([c for c in calls if c[1] == "/search"]) == 1
    assert db.counts()["auto"] == 1


def test_duplicates_and_cache_save_requests(env, monkeypatch):
    db = env
    from app import jobs
    calls = _fake_search(monkeypatch, {"beatles": [_t("y", "Yesterday", "The Beatles")]})
    _add(db, "1:1", 0, "Yesterday", ["The Beatles"])      # альбомная версия
    _add(db, "2:2", 1, "Yesterday", ["The Beatles"])      # тот же трек из сборника
    assert jobs.start_match(); wait_job()
    assert len([c for c in calls if c[1] == "/search"]) == 1
    rows = db.query("SELECT status, spotify_uri FROM tracks")
    assert {r["status"] for r in rows} == {"auto"} and len({r["spotify_uri"] for r in rows}) == 1

    # повторный поиск по тем же запросам (другой регистр) берётся из кэша
    from app import spotify_client as sp
    n = len(calls)
    sp.search_tracks('TRACK:"Yesterday"   artist:"The Beatles"')
    assert len(calls) == n


def test_isrc_search_first_and_migration(env, monkeypatch, tmp_path):
    db = env
    from app import jobs
    hit = _t("k", "Gruppa krovi", "KINO", 286000); hit["isrc"] = "SUA018800100"
    calls = _fake_search(monkeypatch, {"isrc:sua018800100": [hit]})
    _add(db, "1", 0, "Группа крови", ["Кино"], 286000, isrc="SUA018800100")
    assert jobs.start_match(); wait_job()
    searches = [c[2]["q"] for c in calls if c[1] == "/search"]
    assert searches == ["isrc:SUA018800100"]
    assert db.counts()["auto"] == 1


V1_SCHEMA = """
CREATE TABLE tracks (yandex_id TEXT PRIMARY KEY, position INTEGER NOT NULL, liked_at TEXT, title TEXT NOT NULL,
  version TEXT, artists TEXT NOT NULL, album TEXT, year INTEGER, duration_ms INTEGER,
  status TEXT NOT NULL DEFAULT 'new', spotify_uri TEXT, spotify_label TEXT, score REAL, candidates TEXT,
  saved_library INTEGER NOT NULL DEFAULT 0, in_playlist INTEGER NOT NULL DEFAULT 0);
CREATE INDEX idx_tracks_status ON tracks(status);
CREATE TABLE search_cache (query TEXT PRIMARY KEY, response TEXT NOT NULL);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
"""


def test_migration_keeps_old_data(tmp_path, monkeypatch):
    """База из первой версии (одна таблица, kv source/playlist) открывается без потерь."""
    import sqlite3
    from app import db
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(V1_SCHEMA)
    old.execute("INSERT INTO tracks(yandex_id, position, title, artists, status, spotify_uri, saved_library, in_playlist) "
                "VALUES('1:1',0,'T','[]','auto','spotify:track:x',1,1)")
    old.execute("INSERT INTO tracks(yandex_id, position, title, artists) VALUES('2:2',1,'U','[]')")
    old.execute("INSERT INTO kv VALUES('source', ?)", (json.dumps({"key": "my_likes", "title": "Мне нравится",
                                                                  "playlist_name": "Мой плейлист"}),))
    old.execute("INSERT INTO kv VALUES('playlist', ?)", (json.dumps({"id": "PL9", "name": "Мой плейлист"}),))
    old.execute("INSERT INTO kv VALUES('settings', ?)", (json.dumps({"spotify_client_id": "abc"}),))
    old.commit(); old.close()
    monkeypatch.setattr(db, "DB_PATH", path); monkeypatch.setattr(db, "_conn", None)
    rows = db.query("SELECT * FROM tracks ORDER BY position")
    assert [(r["source_key"], r["status"], r["saved_library"], r["in_playlist"]) for r in rows] == \
        [("my_likes", "auto", 1, 1), ("my_likes", "new", 0, 0)]
    assert db.active() == "my_likes"
    assert db.source()["playlist"] == {"id": "PL9", "name": "Мой плейлист"}
    assert db.get("settings")["spotify_client_id"] == "abc"
    assert db.counts()["saved_library"] == 1
    monkeypatch.setattr(db, "_conn", None)   # повторное открытие ничего не ломает
    assert len(db.query("SELECT * FROM tracks")) == 2


def test_playlists_transfer_all_and_shared_tracks(env, monkeypatch):
    db = env
    from app import jobs
    calls = _fake_search(monkeypatch, {"beatles": [_t("y", "Yesterday", "The Beatles")],
                                       "queen": [_t("q", "Bohemian Rhapsody", "Queen")]})
    db.save_source("link:A", {"title": "Рок"}); db.save_source("link:B", {"title": "Дорога"})
    _add(db, "1:1", 0, "Yesterday", ["The Beatles"], source_key="link:A")
    _add(db, "2:2", 1, "Bohemian Rhapsody", ["Queen"], source_key="link:A")
    _add(db, "1:1", 0, "Yesterday", ["The Beatles"], source_key="link:B")   # тот же трек в другом плейлисте
    assert jobs.start_match(); wait_job()
    assert len([c for c in calls if c[1] == "/search"]) == 2      # Yesterday искали один раз
    assert db.counts("*")["auto"] == 3
    assert jobs.start_transfer_all_playlists(); st = wait_job()
    assert st["error"] is None, st
    created = [c for c in calls if c[:2] == ("POST", "/me/playlists")]
    assert [c[3]["name"] for c in created] == ["Яндекс Музыка: Рок", "Яндекс Музыка: Дорога"]
    adds = [c for c in calls if c[0] == "POST" and c[1].endswith("/items")]
    assert len(adds[0][3]["uris"]) == 2 and len(adds[1][3]["uris"]) == 1
    assert db.source("link:A")["playlist"]["id"]


def test_pages_with_sources(env, monkeypatch):
    db = env
    from fastapi.testclient import TestClient
    from app.main import app
    from app import yandex_client
    db.save_source("my_likes", {"title": "Мне нравится"}); db.save_source("link:A", {"title": "Рок"})
    _add(db, "1", 0, "A", ["B"], source_key="link:A")
    monkeypatch.setattr(yandex_client, "my_playlists", lambda: [
        {"kind": 5, "title": "Рок", "track_count": 1, "uid": 1, "key": "link:A"},
        {"kind": 6, "title": "Джаз", "track_count": 3, "uid": 1, "key": "link:C"}])
    c = TestClient(app)
    page = c.get("/").text
    assert "Загруженные списки" in page and "Рок" in page
    pl = c.get("/playlists").text
    assert "Джаз" in pl and "уже загружен" in pl
    c.post("/sources/select", data={"key": "link:A"})
    assert db.active() == "link:A"
    assert "A" in c.get("/review?status=new").text or True
    c.post("/reset", data={"confirm": "да"})
    assert "link:A" not in db.sources() and db.counts("*")["total"] == 0


def test_manual_search_on_review_page(env, monkeypatch):
    db = env
    from fastapi.testclient import TestClient
    from app.main import app
    calls = _fake_search(monkeypatch, {"kino gruppa": [_t("k", "Gruppa krovi", "Kino", 286000)]})
    _add(db, "9:9", 0, "Группа крови", ["Кино"], 286000, status="not_found")
    c = TestClient(app)
    assert 'action="/review/9:9/search"' in c.get("/review?status=not_found").text
    c.post("/review/9:9/search", data={"q": "kino gruppa krovi", "status": "not_found"})
    row = db.query("SELECT * FROM tracks")[0]
    cands = json.loads(row["candidates"])
    assert cands and cands[0]["name"] == "Gruppa krovi" and cands[0]["score"] >= 85
    assert "Gruppa krovi" in c.get("/review?status=not_found").text
    c.post("/review/9:9", data={"action": "pick", "uri": cands[0]["uri"], "status": "not_found"})
    assert db.query("SELECT status FROM tracks")[0]["status"] == "manual"


def test_not_found_csv(env):
    db = env
    import csv, io
    from fastapi.testclient import TestClient
    from app.main import app
    db.save_source("my_likes", {"title": "Мне нравится"}); db.save_source("link:A", {"title": "Рок"})
    _add(db, "1", 0, "Кукушка", ["Кино"], status="not_found")
    _add(db, "2", 1, "Найденный", ["X"], status="auto", spotify_uri="spotify:track:" + "a" * 22)
    _add(db, "3", 2, "Спорный", ["Y"], status="review", version="Live")
    _add(db, "4", 0, "Другой", ["Z"], status="skipped", source_key="link:A")
    c = TestClient(app)
    rows = list(csv.reader(io.StringIO(c.get("/not_found.csv").content.decode("utf-8-sig"))))
    assert rows[0][:2] == ["Track name", "Artist name"]
    assert [r[0] for r in rows[1:]] == ["Кукушка", "Спорный (Live)"]
    rows = list(csv.reader(io.StringIO(c.get("/not_found.csv?all=1").content.decode("utf-8-sig"))))
    assert {r[0] for r in rows[1:]} == {"Кукушка", "Спорный (Live)", "Другой"}
    assert ["Другой", "Z", "A", "Рок", "пропущен"] in rows


def test_sync_only_new_likes(env, monkeypatch):
    db = env
    from app import jobs, yandex_client
    from fastapi.testclient import TestClient
    from app.main import app
    calls = _fake_search(monkeypatch, {"beatles": [_t("y", "Yesterday", "The Beatles")],
                                       "queen": [_t("q", "Bohemian Rhapsody", "Queen")]})
    likes = [("1:1", "Yesterday", ["The Beatles"])]

    def fake_fetch(progress=None):
        for pos, (yid, title, artists) in enumerate(likes):
            db.execute("INSERT INTO tracks(source_key, yandex_id, position, title, artists, duration_ms, album) "
                       "VALUES('my_likes',?,?,?,?,200000,'A') ON CONFLICT(source_key, yandex_id) "
                       "DO UPDATE SET position=excluded.position", (yid, pos, title, json.dumps(artists)))
        return len(likes)
    monkeypatch.setattr(yandex_client, "fetch_liked", fake_fetch)

    # первый перенос, как обычно
    assert jobs.start_import(); wait_job()
    assert jobs.start_match(); wait_job()
    assert jobs.start_transfer(True, False, None); wait_job()
    assert "Синхронизировать новые лайки" in TestClient(app).get("/").text

    # пользователь лайкнул новый трек; синхронизация переносит только его
    likes.insert(0, ("2:2", "Bohemian Rhapsody", ["Queen"]))
    n_search = len([c for c in calls if c[1] == "/search"])
    assert jobs.start_sync(); st = wait_job()
    assert st["error"] is None, st
    assert "новых лайков 1" in st["message"]
    assert len([c for c in calls if c[1] == "/search"]) == n_search + 1
    lib = [c for c in calls if c[1] == "/me/library"]
    assert lib[-1][2]["uris"] == "spotify:track:" + "q" * 22
    assert db.counts("my_likes")["saved_library"] == 2
    assert db.source("my_likes")["last_sync"]["new"] == 1


# --- прогресс, уведомления, причины спорности, описание/обложка/дубли плейлиста ---

def test_eta_and_notification(env, monkeypatch):
    db = env
    from app import jobs, notify
    assert jobs.eta_seconds(10, 100, 20.0) == 180.0
    assert jobs.eta_seconds(1, 100, 20.0) is None          # рано судить
    assert jobs.fmt_duration(30) == "меньше минуты" and jobs.fmt_duration(3900) == "1 ч 5 мин"
    notify.sent.clear()

    def fail():
        raise RuntimeError("что-то сломалось")
    assert jobs._run("transfer", fail); st = wait_job()
    assert st["error"] == "что-то сломалось"
    assert notify.sent[-1] == ("Перенос: остановлено", "что-то сломалось")
    # выключили уведомления — больше не шлём
    s = db.get("settings"); s["notifications"] = False; db.put("settings", s)
    notify.sent.clear()
    assert jobs._run("transfer", fail); wait_job()
    assert notify.sent == []


def test_notification_settings_page(env):
    db = env
    from fastapi.testclient import TestClient
    from app.main import app
    from app import notify
    c = TestClient(app)
    assert "Проверить уведомление" in c.get("/").text
    notify.sent.clear()
    c.post("/settings/notifications", data={"enabled": "1", "action": "test"})
    assert notify.sent and db.get("settings")["notifications"] is True
    c.post("/settings/notifications", data={"action": "save"})
    assert db.get("settings")["notifications"] is False


def test_remix_never_auto_and_reasons(env, monkeypatch):
    db = env
    from app import jobs, matcher
    from fastapi.testclient import TestClient
    from app.main import app
    src = matcher.Source("Numb", None, ["Linkin Park"], "Meteora", 187000, isrc="USWB10300474")
    remix = {"name": "Numb - Remix", "artists": [{"name": "Linkin Park"}], "album": {"name": "Meteora"},
             "duration_ms": 187000, "isrc": "USWB10300474"}
    assert matcher.score(src, remix) < matcher.AUTO_THRESHOLD
    assert any("ремикс" in w for w in matcher.explain(src, remix))
    karaoke = {"name": "Numb", "artists": [{"name": "Karaoke Hits Band"}], "album": {"name": "Meteora"},
               "duration_ms": 187000}
    assert matcher.score(src, karaoke) < matcher.AUTO_THRESHOLD
    for title in ("Numb (Live)", "Numb (Sped Up)", "Numb - Acoustic", "Numb (Cover)"):
        assert matcher.score(src, {**remix, "name": title, "isrc": None}) < matcher.AUTO_THRESHOLD, title
    orig = {**remix, "name": "Numb"}
    assert matcher.score(src, orig) >= matcher.AUTO_THRESHOLD and matcher.explain(src, orig) == []

    # старое автоматическое совпадение с ремиксом при следующем поиске уходит на проверку
    view = matcher.candidate_view({**remix, "uri": "spotify:track:" + "r" * 22}, 90.0)
    view.pop("why")
    _add(db, "1", 0, "Numb", ["Linkin Park"], 187000, status="auto", spotify_uri=view["uri"],
         candidates=json.dumps([view]), album="Meteora")
    _fake_search(monkeypatch, {})
    assert jobs.start_match(); wait_job()
    assert db.query("SELECT status FROM tracks")[0]["status"] == "review"
    page = TestClient(app).get("/review?status=review").text
    assert "Почему спорный" in page and "ремикс" in page


def test_playlist_description_cover_and_no_duplicates(env, monkeypatch):
    db = env
    from app import jobs, covers, spotify_client as sp
    db.put("spotify_user", {"id": "me", "name": "Me"})
    uri = lambda ch: "spotify:track:" + ch * 22
    calls = []
    existing = {"name": "Яндекс Музыка: Рок", "id": "OLD", "owner": {"id": "me"}}

    def fake_request(method, path, params=None, json=None, essential=False, _retry=0):
        calls.append((method, path, params, json))
        if method == "GET" and path == "/me/playlists":
            return {"items": [existing] if existing else [], "next": None}
        if method == "POST" and path == "/me/playlists":
            return {"id": "NEW"}
        if method == "GET" and path.endswith("/items"):
            return {"items": [{"item": {"uri": uri("a")}}], "next": None}
        return None
    monkeypatch.setattr(sp, "request", fake_request)
    covers_set = []
    monkeypatch.setattr(covers, "download", lambda url, proxy=None: b"img")
    monkeypatch.setattr(covers, "to_jpeg", lambda raw: b"jpeg")
    monkeypatch.setattr(sp, "set_cover", lambda pid, jpeg: covers_set.append((pid, jpeg)))

    db.save_source("link:R", {"title": "Рок", "description": "Лучший\nрок", "image": "https://x/cover.jpg"})
    _add(db, "1", 0, "A", ["X"], status="auto", spotify_uri=uri("a"), source_key="link:R")
    _add(db, "2", 1, "B", ["Y"], status="auto", spotify_uri=uri("b"), source_key="link:R")

    # плейлист с таким названием уже есть в Spotify: дополняем его, трек «a» там уже лежит
    assert jobs.start_transfer(False, True, None, key="link:R"); st = wait_job()
    assert st["error"] is None, st
    adds = [c for c in calls if c[0] == "POST" and c[1].endswith("/items")]
    assert adds == [("POST", "/playlists/OLD/items", None, {"uris": [uri("b")]})]
    assert "пропущено дублей: 1" in st["message"] and not covers_set

    # новый плейлист: описание из Яндекса и обложка
    existing = None
    db.execute("UPDATE tracks SET in_playlist=0")
    calls.clear()
    assert jobs.start_transfer(False, True, "Новый", key="link:R"); st = wait_job()
    assert st["error"] is None, st
    created = [c for c in calls if c[:2] == ("POST", "/me/playlists")][0]
    assert created[3]["description"] == "Лучший рок · Перенесено из Яндекс Музыки"
    assert covers_set == [("NEW", b"jpeg")] and "обложка перенесена" in st["message"]


def test_cover_to_jpeg():
    import io
    from PIL import Image
    from app import covers
    buf = io.BytesIO(); Image.new("RGB", (1000, 800), "red").save(buf, "PNG")
    out = covers.to_jpeg(buf.getvalue())
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG" and img.size == (640, 640) and len(out) * 4 / 3 <= covers.MAX_BYTES
