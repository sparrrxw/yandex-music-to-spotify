"""Чтение лайкнутых треков из Яндекс Музыки через библиотеку yandex-music."""
from __future__ import annotations

import re
from typing import Callable

from yandex_music import Client
from yandex_music.utils.request import Request

from . import db

# Публичный client_id официального приложения Яндекс Музыки. Вход через него
# возвращает токен в адресной строке (способ из документации yandex-music).
YANDEX_OAUTH_URL = (
    "https://oauth.yandex.ru/authorize?response_type=token"
    "&client_id=23cabbbdc6cd418abb4b39c32c41195d"
)


# Описание и обложка последнего загруженного плейлиста (для переноса в Spotify).
last_meta: dict = {}


def _cover_url(pl) -> str | None:
    uri = getattr(getattr(pl, "cover", None), "uri", None) or getattr(pl, "og_image", None)
    if not uri:
        return None
    uri = uri.replace("%%", "400x400")
    return uri if uri.startswith("http") else "https://" + uri


def _remember(pl) -> None:
    last_meta.clear()
    last_meta.update({"description": (getattr(pl, "description", None) or "").strip(),
                      "image": _cover_url(pl)})


def parse_token(text: str) -> str:
    """Принимает сам токен или целиком скопированный адрес с access_token=..."""
    text = (text or "").strip()
    m = re.search(r"access_token=([^&#\s]+)", text)
    return m.group(1) if m else text


def _client(anonymous: bool = False) -> Client:
    s = db.get("settings", {}) or {}
    token = None if anonymous else s.get("yandex_token")
    if not token and not anonymous:
        raise RuntimeError("Не указан токен Яндекс Музыки")
    proxy = (s.get("yandex_proxy") or "").strip() or None
    request = Request(proxy_url=proxy) if proxy else None
    return Client(token, request=request).init()


def check() -> str:
    """Проверяет токен и возвращает логин/имя аккаунта."""
    client = _client()
    acc = client.me.account if client.me else None
    if not acc or not acc.uid:
        raise RuntimeError("Яндекс не вернул данные аккаунта, токен неверный?")
    name = acc.display_name or acc.login or str(acc.uid)
    db.put("yandex_user", name)
    return name


def _store(client: Client, shorts: list, progress: Callable[[str], None] | None, source_key: str) -> int:
    """Догружает подробности треков и записывает их в таблицу tracks в заданном порядке."""
    import json
    total = len(shorts)
    if progress:
        progress(f"Найдено треков: {total}. Загружаю подробности…")
    by_id = {str(s.id): s.track for s in shorts if getattr(s, "track", None) is not None and s.track.title}
    missing = [s.track_id for s in shorts if str(s.id) not in by_id]
    for i in range(0, len(missing), 100):
        for tr in client.tracks(missing[i:i + 100]):
            by_id[str(tr.id)] = tr
        if progress:
            progress(f"Загружено {min(i + 100, len(missing))} из {len(missing)}")

    stored = 0
    for pos, short in enumerate(shorts):
        tr = by_id.get(str(short.id))
        if tr is None or not tr.title:
            continue
        album = tr.albums[0] if tr.albums else None
        db.execute(
            """INSERT INTO tracks(source_key, yandex_id, position, liked_at, title, version, artists, album, year,
                                  duration_ms, isrc)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_key, yandex_id) DO UPDATE SET position=excluded.position, liked_at=excluded.liked_at,
                   title=excluded.title, version=excluded.version, artists=excluded.artists,
                   album=excluded.album, year=excluded.year, duration_ms=excluded.duration_ms,
                   isrc=COALESCE(excluded.isrc, tracks.isrc)""",
            (
                source_key, short.track_id, pos, short.timestamp, tr.title, tr.version,
                json.dumps([a.name for a in (tr.artists or []) if a.name], ensure_ascii=False),
                album.title if album else None,
                album.year if album else None,
                tr.duration_ms,
                (getattr(tr, "isrc", None) or "").strip().upper() or None,
            ),
        )
        stored += 1
    return stored


def fetch_liked(progress: Callable[[str], None] | None = None) -> int:
    """Загружает все лайки владельца токена. Возвращает число треков."""
    client = _client()
    likes = client.users_likes_tracks()
    shorts = list(likes.tracks) if likes else []
    # Яндекс отдаёт лайки от свежих к старым; на всякий случай сортируем по времени.
    shorts.sort(key=lambda t: t.timestamp or "", reverse=True)
    return _store(client, shorts, progress, db.MY_LIKES)


# --- импорт по публичной ссылке ---------------------------------------------

_DOMAIN = r"(?:https?://)?(?:www\.)?music\.yandex\.(?:ru|com|by|kz|uz|com\.tr)"


def parse_link(url: str) -> dict:
    """Разбирает ссылку Яндекс Музыки. Возвращает {"type": ..., ...} или бросает ValueError."""
    url = (url or "").strip()
    m = re.search(_DOMAIN + r"/users/([^/?#]+)/playlists/(\d+)", url)
    if m:
        return {"type": "user_playlist", "user": m.group(1), "kind": int(m.group(2))}
    m = re.search(_DOMAIN + r"/users/([^/?#]+)/tracks", url)
    if m:
        return {"type": "user_likes", "user": m.group(1)}
    m = re.search(_DOMAIN + r"/playlists/([A-Za-z0-9.\-_]+)", url)
    if m:
        return {"type": "uuid_playlist", "uuid": m.group(1)}
    raise ValueError(
        "Не понимаю ссылку. Нужна ссылка вида https://music.yandex.ru/users/ЛОГИН/playlists/3, "
        "https://music.yandex.ru/users/ЛОГИН/tracks или https://music.yandex.ru/playlists/…"
    )


def _load_link(client: Client, link: dict) -> tuple[str, list]:
    last_meta.clear()
    if link["type"] == "user_likes":
        likes = client.users_likes_tracks(user_id=link["user"])
        shorts = list(likes.tracks) if likes else []
        shorts.sort(key=lambda t: t.timestamp or "", reverse=True)
        return f"Лайки {link['user']}", shorts
    if link["type"] == "user_playlist":
        pl = client.users_playlists(link["kind"], user_id=link["user"])
    else:
        pl = client.playlist(link["uuid"])
    if isinstance(pl, list):
        pl = pl[0] if pl else None
    if pl is None:
        raise RuntimeError("Яндекс не вернул плейлист")
    _remember(pl)
    title = pl.title or "Плейлист"
    if link["type"] == "user_playlist" and link["kind"] == 3:
        title = f"Мне нравится ({link['user']})"
    shorts = list(pl.tracks or [])
    if not shorts and (pl.track_count or 0) > 0 and pl.uid and pl.kind is not None:
        full = client.users_playlists(pl.kind, user_id=pl.uid)
        if isinstance(full, list):
            full = full[0] if full else None
        shorts = list(full.tracks or []) if full else []
    return title, shorts


def link_key(link: dict) -> str:
    import json
    return "link:" + json.dumps(link, sort_keys=True)


def my_playlists() -> list[dict]:
    """Плейлисты владельца токена (кроме «Мне нравится», она загружается отдельно)."""
    client = _client()
    uid = client.me.account.uid
    out = []
    for pl in client.users_playlists_list(user_id=uid) or []:
        if pl.kind == 3:
            continue
        out.append({"kind": pl.kind, "title": pl.title or f"Плейлист {pl.kind}",
                    "track_count": pl.track_count or 0, "uid": uid,
                    "key": link_key({"type": "user_playlist", "user": str(uid), "kind": int(pl.kind)})})
    return out


def fetch_my_playlist(kind: int, progress: Callable[[str], None] | None = None) -> tuple[str, str, int]:
    """Загружает один свой плейлист. Возвращает (ключ источника, название, число треков)."""
    client = _client()
    uid = client.me.account.uid
    link = {"type": "user_playlist", "user": str(uid), "kind": int(kind)}
    pl = client.users_playlists(int(kind), user_id=uid)
    if isinstance(pl, list):
        pl = pl[0] if pl else None
    if pl is None:
        raise RuntimeError(f"Яндекс не вернул плейлист {kind}")
    _remember(pl)
    key = link_key(link)
    return key, pl.title or f"Плейлист {kind}", _store(client, list(pl.tracks or []), progress, key)


def fetch_link(url: str, progress: Callable[[str], None] | None = None) -> tuple[str, int]:
    """Загружает треки по публичной ссылке. Сначала без токена, при неудаче — с вашим токеном."""
    link = parse_link(url)
    errors = []
    attempts = [("без токена", True)]
    if (db.get("settings", {}) or {}).get("yandex_token"):
        attempts.append(("с вашим токеном", False))
    for label, anonymous in attempts:
        try:
            if progress:
                progress(f"Открываю ссылку ({label})…")
            client = _client(anonymous=anonymous)
            title, shorts = _load_link(client, link)
            if not shorts:
                raise RuntimeError("в плейлисте нет треков или он закрыт")
            return title, _store(client, shorts, progress, link_key(link))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{label}: {e}")
    hint = "" if len(attempts) > 1 else " Подключите свой токен Яндекса (шаг 2) и попробуйте ещё раз."
    raise RuntimeError("Не удалось загрузить плейлист. " + "; ".join(errors) + "." + hint +
                       " Проверьте, что плейлист открыт (публичный).")
