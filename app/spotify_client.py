"""Клиент Spotify Web API под правила 2026 года.

Используются только эндпоинты, доступные приложениям в Development Mode после
миграции февраля 2026: /search (limit ≤ 10), PUT /me/library (≤ 40 URI),
POST /me/playlists, POST /playlists/{id}/items.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

import httpx

from . import db

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = ("user-library-read user-library-modify playlist-modify-private playlist-modify-public "
          "playlist-read-private ugc-image-upload")

LIBRARY_BATCH = 40
PLAYLIST_BATCH = 100
SEARCH_LIMIT = 10


class SpotifyError(Exception):
    pass


class NotAuthorized(SpotifyError):
    pass


class QuotaPaused(SpotifyError):
    """Квота исчерпана или Spotify просит ждать долго: задачу надо поставить на паузу."""

    def __init__(self, until: datetime, reason: str):
        super().__init__(reason)
        self.until = until
        self.reason = reason


def _settings() -> dict:
    return db.get("settings", {}) or {}


def redirect_uri() -> str:
    s = _settings()
    return f"http://127.0.0.1:{s.get('port', 8888)}/callback"


# --- OAuth (Authorization Code + PKCE) ---------------------------------------

def build_auth_url() -> str:
    client_id = _settings().get("spotify_client_id")
    if not client_id:
        raise NotAuthorized("Не указан Spotify Client ID")
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    db.put("spotify_pkce", {"verifier": verifier, "state": state})
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _http() -> httpx.Client:
    proxy = (_settings().get("spotify_proxy") or "").strip() or None
    return httpx.Client(proxy=proxy, timeout=30)


def _store_token(data: dict) -> None:
    tok = db.get("spotify_token", {}) or {}
    tok["access_token"] = data["access_token"]
    tok["expires_at"] = time.time() + int(data.get("expires_in", 3600)) - 60
    if data.get("refresh_token"):
        tok["refresh_token"] = data["refresh_token"]
    db.put("spotify_token", tok)


def handle_callback(code: str, state: str) -> None:
    pkce = db.get("spotify_pkce") or {}
    if not pkce or pkce.get("state") != state:
        raise SpotifyError("Неверный state в ответе Spotify. Попробуйте войти ещё раз.")
    with _http() as h:
        r = h.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(),
            "client_id": _settings().get("spotify_client_id"),
            "code_verifier": pkce["verifier"],
        })
    if r.status_code != 200:
        raise SpotifyError(f"Spotify не выдал токен: {r.status_code} {r.text[:300]}")
    _store_token(r.json())
    db.delete("spotify_pkce")


def _refresh() -> None:
    tok = db.get("spotify_token") or {}
    if not tok.get("refresh_token"):
        raise NotAuthorized("Нужно войти в Spotify")
    with _http() as h:
        r = h.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": _settings().get("spotify_client_id"),
        })
    if r.status_code != 200:
        db.delete("spotify_token")
        raise NotAuthorized("Сессия Spotify истекла, войдите заново")
    _store_token(r.json())


def is_authorized() -> bool:
    return bool((db.get("spotify_token") or {}).get("refresh_token"))


def logout() -> None:
    db.delete("spotify_token")
    db.delete("spotify_user")


def _access_token() -> str:
    tok = db.get("spotify_token") or {}
    if not tok:
        raise NotAuthorized("Нужно войти в Spotify")
    if time.time() >= tok.get("expires_at", 0):
        _refresh()
        tok = db.get("spotify_token")
    return tok["access_token"]


# --- учёт запросов и квоты ---------------------------------------------------

_req_lock = threading.Lock()
_last_request = 0.0


def requests_today() -> int:
    return int(db.get(f"spotify_requests:{date.today().isoformat()}", 0))


def daily_budget() -> int:
    return int(_settings().get("daily_budget", 1500))


def paused_until() -> datetime | None:
    v = db.get("spotify_paused_until")
    if not v:
        return None
    dt = datetime.fromisoformat(v)
    if dt <= datetime.now():
        db.delete("spotify_paused_until")
        return None
    return dt


def _pause(seconds: float, reason: str) -> QuotaPaused:
    until = datetime.now() + timedelta(seconds=seconds)
    db.put("spotify_paused_until", until.isoformat(timespec="seconds"))
    return QuotaPaused(until, reason)


def _before_request(essential: bool = False) -> None:
    """essential=True для записи (лайки/плейлист): таких запросов мало, поэтому свой дневной лимит
    и паузу после поиска не применяем. Если Spotify сам откажет, пауза поставится заново."""
    global _last_request
    until = None if essential else paused_until()
    if until:
        raise QuotaPaused(until, "Spotify временно ограничил запросы")
    if not essential and requests_today() >= daily_budget():
        tomorrow = datetime.combine(date.today() + timedelta(days=1), datetime.min.time())
        raise QuotaPaused(tomorrow, f"Достигнут дневной лимит запросов ({daily_budget()}), заданный в настройках")
    interval = float(_settings().get("request_interval", 0.35))
    with _req_lock:
        wait = _last_request + interval - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
        key = f"spotify_requests:{date.today().isoformat()}"
        db.put(key, int(db.get(key, 0)) + 1)


def request(method: str, path: str, *, params=None, json=None, essential=False, _retry=0) -> dict | None:
    _before_request(essential)
    headers = {"Authorization": f"Bearer {_access_token()}"}
    with _http() as h:
        r = h.request(method, API + path, params=params, json=json, headers=headers)

    if r.status_code == 401 and _retry < 1:
        _refresh()
        return request(method, path, params=params, json=json, essential=essential, _retry=_retry + 1)

    if r.status_code == 429:
        retry_after = int(r.headers.get("Retry-After", "30") or 30)
        reason = ""
        try:
            reason = (r.json() or {}).get("reason", "") or ""
        except Exception:
            pass
        if "QUOTA" in reason.upper() or retry_after > 120:
            raise _pause(max(retry_after, 60), "Исчерпана дневная квота Spotify (QUOTA_EXCEEDED)")
        if _retry >= 5:
            raise _pause(retry_after, "Spotify постоянно отвечает 429")
        time.sleep(retry_after + 1)
        return request(method, path, params=params, json=json, essential=essential, _retry=_retry + 1)

    if r.status_code >= 500 and _retry < 3:
        time.sleep(2 * (_retry + 1))
        return request(method, path, params=params, json=json, essential=essential, _retry=_retry + 1)

    if r.status_code == 403:
        raise SpotifyError(
            "Spotify ответил 403. Проверьте, что ваш аккаунт добавлен в User Management "
            f"приложения и что у владельца приложения есть Premium. Ответ: {r.text[:300]}"
        )
    if r.status_code >= 400:
        raise SpotifyError(f"Spotify {method} {path}: {r.status_code} {r.text[:300]}")
    if not r.content:
        return None
    try:
        return r.json()
    except ValueError:
        return None


# --- методы API --------------------------------------------------------------

def me() -> dict:
    user = request("GET", "/me") or {}
    db.put("spotify_user", {"id": user.get("id"), "name": user.get("display_name") or user.get("id")})
    return user


def search_tracks(q: str) -> list[dict]:
    cached = db.cache_get(q)
    if cached is not None:
        return cached
    data = request("GET", "/search", params={"q": q, "type": "track", "limit": SEARCH_LIMIT}) or {}
    items = [t for t in ((data.get("tracks") or {}).get("items") or []) if t]
    slim = [{
        "uri": t.get("uri"),
        "name": t.get("name"),
        "artists": [{"name": a.get("name")} for a in t.get("artists", [])],
        "album": {"name": (t.get("album") or {}).get("name")},
        "duration_ms": t.get("duration_ms"),
        "external_urls": t.get("external_urls") or {},
        "isrc": ((t.get("external_ids") or {}).get("isrc") or "").upper() or None,
    } for t in items]
    db.cache_put(q, slim)
    return slim


def save_to_library(uris: list[str]) -> None:
    for i in range(0, len(uris), LIBRARY_BATCH):
        chunk = uris[i:i + LIBRARY_BATCH]
        request("PUT", "/me/library", params={"uris": ",".join(chunk)}, essential=True)


def create_playlist(name: str, description: str = "") -> str:
    data = request("POST", "/me/playlists", json={"name": name, "public": False, "description": description},
                   essential=True) or {}
    if not data.get("id"):
        raise SpotifyError("Spotify не вернул id созданного плейлиста")
    return data["id"]


DESCRIPTION_LIMIT = 300


def set_cover(playlist_id: str, jpeg: bytes) -> None:
    """Обложка плейлиста: JPEG до 256 КБ в base64, нужен scope ugc-image-upload."""
    _before_request(essential=True)
    with _http() as h:
        r = h.put(f"{API}/playlists/{playlist_id}/images", content=base64.b64encode(jpeg),
                  headers={"Authorization": f"Bearer {_access_token()}", "Content-Type": "image/jpeg"})
    if r.status_code in (401, 403):
        raise SpotifyError("Spotify не дал поставить обложку: нажмите «Выйти» и войдите в Spotify заново "
                           "(нужно новое разрешение на загрузку картинок)")
    if r.status_code >= 400:
        raise SpotifyError(f"Spotify не принял обложку: {r.status_code} {r.text[:200]}")


def _paged(path: str, params: dict | None = None):
    params = dict(params or {})
    params.setdefault("limit", 50)
    params["offset"] = 0
    while True:
        data = request("GET", path, params=params, essential=True) or {}
        items = data.get("items") or []
        yield from items
        if not data.get("next") or not items:
            return
        params["offset"] += len(items)


def playlist_item_uris(playlist_id: str) -> set[str]:
    """URI треков, которые уже лежат в плейлисте (чтобы не добавлять дубли)."""
    out = set()
    for it in _paged(f"/playlists/{playlist_id}/items"):
        t = (it or {}).get("track") or (it or {}).get("item") or {}
        if t.get("uri"):
            out.add(t["uri"])
    return out


def find_playlist(name: str) -> str | None:
    """Свой плейлист с таким названием (чтобы не плодить копии, если база была очищена)."""
    try:
        my_id = (db.get("spotify_user") or {}).get("id")
        for pl in _paged("/me/playlists"):
            if not pl:
                continue
            owner = (pl.get("owner") or {}).get("id")
            if (pl.get("name") or "").strip() == name.strip() and (not my_id or owner == my_id):
                return pl.get("id")
    except Exception:  # noqa: BLE001  (не смогли проверить — просто создадим новый)
        return None
    return None


def add_to_playlist(playlist_id: str, uris: list[str]) -> None:
    for i in range(0, len(uris), PLAYLIST_BATCH):
        request("POST", f"/playlists/{playlist_id}/items", json={"uris": uris[i:i + PLAYLIST_BATCH]}, essential=True)


def parse_track_ref(text: str) -> str | None:
    """Ссылка open.spotify.com/track/ID, spotify:track:ID или голый ID → URI."""
    import re
    text = (text or "").strip()
    m = re.search(r"(?:track[/:])([A-Za-z0-9]{22})", text)
    if m:
        return f"spotify:track:{m.group(1)}"
    if re.fullmatch(r"[A-Za-z0-9]{22}", text):
        return f"spotify:track:{text}"
    return None
