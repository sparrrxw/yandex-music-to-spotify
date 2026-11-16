"""Веб-интерфейс сервиса (FastAPI). Запуск: python -m app"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import db, jobs, matcher, spotify_client as sp, yandex_client

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
app = FastAPI(title="Яндекс Музыка → Spotify")


def _settings() -> dict:
    return db.get("settings", {}) or {}


def _flash(request: Request, text: str, kind: str = "ok") -> RedirectResponse:
    db.put("flash", {"text": text, "kind": kind})
    return RedirectResponse("/", status_code=303)


def _ctx(request: Request, **extra) -> dict:
    flash = db.get("flash")
    if flash:
        db.delete("flash")
    s = _settings()
    paused = sp.paused_until()
    return {
        "request": request,
        "s": s,
        "flash": flash,
        "counts": db.counts(),
        "job": jobs.state(),
        "spotify_ok": sp.is_authorized(),
        "spotify_user": (db.get("spotify_user") or {}).get("name"),
        "yandex_user": db.get("yandex_user"),
        "redirect_uri": sp.redirect_uri(),
        "requests_today": sp.requests_today(),
        "budget": sp.daily_budget(),
        "paused_until": paused.strftime("%d.%m %H:%M") if paused else None,
        "active": db.active(),
        "source": db.source() or None,
        "sources": [
            {"key": k, **m, "counts": db.counts(k)} for k, m in db.sources().items()
        ],
        "my_likes_key": db.MY_LIKES,
        "likes_meta": _likes_meta(),
        **extra,
    }


def _likes_meta() -> dict | None:
    meta = db.source(db.MY_LIKES)
    if not meta:
        return None
    if not meta.get("last_transfer"):
        meta = {**meta, "last_transfer": {"to_library": True, "to_playlist": bool(meta.get("playlist"))}}
    return meta


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _ctx(request, yandex_oauth=yandex_client.YANDEX_OAUTH_URL))


# --- настройки ---------------------------------------------------------------

@app.post("/settings/spotify")
def save_spotify(request: Request, client_id: str = Form(""), proxy: str = Form("")):
    s = _settings()
    s["spotify_client_id"] = client_id.strip()
    s["spotify_proxy"] = proxy.strip()
    db.put("settings", s)
    return _flash(request, "Настройки Spotify сохранены")


@app.post("/settings/yandex")
def save_yandex(request: Request, token: str = Form(""), proxy: str = Form("")):
    s = _settings()
    s["yandex_token"] = yandex_client.parse_token(token)
    s["yandex_proxy"] = proxy.strip()
    db.put("settings", s)
    try:
        name = yandex_client.check()
    except Exception as e:  # noqa: BLE001
        return _flash(request, f"Яндекс не принял токен: {e}", "error")
    return _flash(request, f"Яндекс Музыка подключена: {name}")


@app.post("/settings/limits")
def save_limits(request: Request, daily_budget: int = Form(1500), request_interval: float = Form(0.35)):
    s = _settings()
    s["daily_budget"] = max(50, daily_budget)
    s["request_interval"] = max(0.1, request_interval)
    db.put("settings", s)
    return _flash(request, "Лимиты сохранены")


@app.post("/settings/unpause")
def unpause(request: Request):
    db.delete("spotify_paused_until")
    return _flash(request, "Пауза снята. Если Spotify всё ещё ограничивает запросы, пауза вернётся.")


# --- Spotify OAuth -----------------------------------------------------------

@app.get("/login")
def login(request: Request):
    try:
        return RedirectResponse(sp.build_auth_url())
    except sp.SpotifyError as e:
        return _flash(request, str(e), "error")


@app.get("/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return _flash(request, f"Spotify отказал во входе: {error}", "error")
    try:
        sp.handle_callback(code, state)
        user = sp.me()
    except sp.SpotifyError as e:
        return _flash(request, str(e), "error")
    return _flash(request, f"Spotify подключён: {user.get('display_name') or user.get('id')}")


@app.post("/logout")
def logout(request: Request):
    sp.logout()
    return _flash(request, "Вы вышли из Spotify")


# --- задачи ------------------------------------------------------------------

def _busy(request: Request):
    return _flash(request, "Уже выполняется другая задача", "error")


@app.post("/jobs/import")
def job_import(request: Request):
    return RedirectResponse("/", status_code=303) if jobs.start_import() else _busy(request)


@app.post("/jobs/import-link")
def job_import_link(request: Request, url: str = Form("")):
    try:
        yandex_client.parse_link(url)
    except ValueError as e:
        return _flash(request, str(e), "error")
    return RedirectResponse("/", status_code=303) if jobs.start_import_link(url.strip()) else _busy(request)


@app.get("/playlists", response_class=HTMLResponse)
def playlists_page(request: Request):
    try:
        pls = yandex_client.my_playlists()
        error = None
    except Exception as e:  # noqa: BLE001
        pls, error = [], f"Не удалось получить плейлисты: {e}"
    loaded = db.sources()
    for p in pls:
        p["loaded"] = p["key"] in loaded
    return templates.TemplateResponse(request, "playlists.html", _ctx(request, playlists=pls, error=error))


@app.post("/jobs/import-playlists")
async def job_import_playlists(request: Request):
    form = await request.form()
    kinds = [int(k) for k in form.getlist("kind") if str(k).isdigit()]
    if not kinds:
        return _flash(request, "Отметьте хотя бы один плейлист", "error")
    return RedirectResponse("/", status_code=303) if jobs.start_import_my_playlists(kinds) else _busy(request)


@app.post("/sources/select")
def source_select(request: Request, key: str = Form(...)):
    if key in db.sources():
        db.set_active(key)
    return RedirectResponse("/", status_code=303)


@app.post("/sources/remove")
def source_remove(request: Request, key: str = Form(...), confirm: str = Form("")):
    if confirm != "да":
        return _flash(request, "Чтобы убрать источник, впишите «да»", "error")
    title = db.source(key).get("title", key)
    db.remove_source(key)
    return _flash(request, f"Источник «{title}» убран из списка. В Spotify ничего не удалено.")


@app.post("/jobs/match")
def job_match(request: Request, retry_not_found: str = Form("")):
    ok = jobs.start_match(rematch_not_found=bool(retry_not_found))
    return RedirectResponse("/", status_code=303) if ok else _busy(request)


@app.post("/jobs/transfer")
def job_transfer(request: Request, to_library: str = Form(""), to_playlist: str = Form(""),
                 playlist_name: str = Form("")):
    if not to_library and not to_playlist:
        return _flash(request, "Выберите, куда переносить: в «Любимые треки» и/или в плейлист", "error")
    ok = jobs.start_transfer(bool(to_library), bool(to_playlist), playlist_name.strip() or None)
    return RedirectResponse("/", status_code=303) if ok else _busy(request)


@app.post("/jobs/sync")
def job_sync(request: Request):
    return RedirectResponse("/", status_code=303) if jobs.start_sync() else _busy(request)


@app.post("/jobs/transfer-all")
def job_transfer_all(request: Request):
    return RedirectResponse("/", status_code=303) if jobs.start_transfer_all_playlists() else _busy(request)


@app.post("/jobs/stop")
def job_stop(request: Request):
    jobs.stop()
    return RedirectResponse("/", status_code=303)


@app.get("/api/status")
def api_status():
    paused = sp.paused_until()
    return JSONResponse({
        "job": jobs.state(),
        "counts": db.counts(),
        "requests_today": sp.requests_today(),
        "paused_until": paused.strftime("%d.%m %H:%M") if paused else None,
    })


# --- проверка спорных --------------------------------------------------------

def _track_view(r) -> dict:
    d = dict(r)
    d["artists"] = ", ".join(json.loads(r["artists"] or "[]"))
    d["candidates"] = json.loads(r["candidates"] or "[]")
    return d


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, status: str = "review"):
    allowed = {"review", "not_found", "auto", "manual", "skipped"}
    if status not in allowed:
        status = "review"
    rows = db.query("SELECT * FROM tracks WHERE source_key=? AND status=? ORDER BY position LIMIT 300",
                    (db.active(), status))
    return templates.TemplateResponse(request, "review.html", _ctx(
        request, rows=[_track_view(r) for r in rows], status=status))


def _apply_everywhere(yandex_id: str, sql_set: str, params: tuple, also_finished: bool = False) -> None:
    """Решение по треку применяем и к тому же треку в других плейлистах, где он ещё не решён."""
    db.execute(f"UPDATE tracks SET {sql_set} WHERE source_key=? AND yandex_id=?",
               params + (db.active(), yandex_id))
    db.execute(f"UPDATE tracks SET {sql_set} WHERE source_key<>? AND yandex_id=? "
               "AND status IN ('new','review','not_found')", params + (db.active(), yandex_id))


@app.post("/review/{yandex_id}")
def review_save(request: Request, yandex_id: str, action: str = Form(...), uri: str = Form(""),
                link: str = Form(""), status: str = Form("review")):
    rows = db.query("SELECT * FROM tracks WHERE source_key=? AND yandex_id=?", (db.active(), yandex_id))
    if not rows:
        return RedirectResponse(f"/review?status={status}", status_code=303)
    row = rows[0]
    if action == "skip":
        _apply_everywhere(yandex_id, "status='skipped', spotify_uri=NULL, spotify_label=NULL", ())
    elif action == "pick":
        chosen = sp.parse_track_ref(link) if link.strip() else uri
        if chosen:
            label = None
            for c in json.loads(row["candidates"] or "[]"):
                if c["uri"] == chosen:
                    label = f"{c['artists']} — {c['name']}"
            _apply_everywhere(
                yandex_id, "status='manual', spotify_uri=?, spotify_label=?, saved_library=0, in_playlist=0",
                (chosen, label or link.strip() or chosen))
    elif action == "reset":
        db.execute("UPDATE tracks SET status='new', spotify_uri=NULL, spotify_label=NULL "
                   "WHERE source_key=? AND yandex_id=?", (db.active(), yandex_id))
    return RedirectResponse(f"/review?status={status}#t-{row['position']}", status_code=303)


@app.post("/review/{yandex_id}/search")
def review_search(request: Request, yandex_id: str, q: str = Form(""), status: str = Form("review")):
    """Ручной поиск: свой запрос к Spotify, результаты добавляются к вариантам трека."""
    rows = db.query("SELECT * FROM tracks WHERE source_key=? AND yandex_id=?", (db.active(), yandex_id))
    if not rows or not q.strip():
        return RedirectResponse(f"/review?status={status}", status_code=303)
    row = rows[0]
    try:
        found = sp.search_tracks(q.strip())
    except sp.QuotaPaused as e:
        db.put("flash", {"text": f"{e.reason}. Ручной поиск будет доступен после {e.until:%d.%m %H:%M}. "
                                 "Пока можно вставить ссылку на трек из Spotify.", "kind": "error"})
        return RedirectResponse(f"/review?status={status}#t-{row['position']}", status_code=303)
    except sp.SpotifyError as e:
        db.put("flash", {"text": str(e), "kind": "error"})
        return RedirectResponse(f"/review?status={status}#t-{row['position']}", status_code=303)
    src = jobs._source(row)
    merged = {c["uri"]: c for c in json.loads(row["candidates"] or "[]")}
    for cand in found:
        if cand.get("uri"):
            merged[cand["uri"]] = matcher.candidate_view(cand, matcher.score(src, cand))
    ranked = sorted(merged.values(), key=lambda c: c["score"] or 0, reverse=True)[:10]
    db.execute("UPDATE tracks SET candidates=? WHERE source_key=? AND yandex_id=?",
               (json.dumps(ranked, ensure_ascii=False), db.active(), yandex_id))
    db.put("flash", {"text": f"Найдено вариантов: {len(found)}" if found else "Spotify ничего не нашёл по этому запросу",
                     "kind": "ok" if found else "error"})
    return RedirectResponse(f"/review?status={status}#t-{row['position']}", status_code=303)


@app.post("/review-bulk")
def review_bulk(request: Request, action: str = Form(...)):
    key = db.active()
    if action == "accept_best":
        for r in db.query("SELECT yandex_id, candidates FROM tracks WHERE source_key=? AND status='review'", (key,)):
            c = json.loads(r["candidates"] or "[]")
            if c:
                db.execute(
                    "UPDATE tracks SET status='manual', spotify_uri=?, spotify_label=? "
                    "WHERE source_key=? AND yandex_id=?",
                    (c[0]["uri"], f"{c[0]['artists']} — {c[0]['name']}", key, r["yandex_id"]),
                )
    elif action == "skip_not_found":
        db.execute("UPDATE tracks SET status='skipped' WHERE source_key=? AND status='not_found'", (key,))
    return RedirectResponse("/review", status_code=303)


# --- отчёт -------------------------------------------------------------------

def _csv_response(header: list, rows: list, filename: str, delimiter: str = ";") -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter)
    w.writerow(header)
    w.writerows(rows)
    data = "\ufeff" + buf.getvalue()  # BOM, чтобы Excel правильно открыл кириллицу
    return StreamingResponse(iter([data.encode("utf-8")]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.get("/report.csv")
def report():
    out = []
    for r in db.query("SELECT * FROM tracks WHERE source_key=? ORDER BY position", (db.active(),)):
        dur = r["duration_ms"] or 0
        out.append([
            r["position"] + 1, r["status"], ", ".join(json.loads(r["artists"] or "[]")), r["title"],
            r["version"] or "", r["album"] or "", f"{dur // 60000}:{dur // 1000 % 60:02d}",
            r["spotify_uri"] or "", r["spotify_label"] or "", r["score"] or "",
            "да" if r["saved_library"] else "", "да" if r["in_playlist"] else "",
        ])
    return _csv_response(["№", "Статус", "Артисты", "Название", "Версия", "Альбом", "Длительность",
                          "Spotify", "Найдено в Spotify", "Балл", "В «Любимых»", "В плейлисте"],
                         out, "transfer_report.csv")


STATUS_RU = {"review": "не подтверждён", "not_found": "не найден", "skipped": "пропущен"}


@app.get("/not_found.csv")
def not_found_csv(all: int = 0):
    """Ненайденные/пропущенные/неподтверждённые треки. Колонки подходят для импорта файлом в TuneMyMusic."""
    where = "" if all else "source_key=? AND"
    params = () if all else (db.active(),)
    allsrc = db.sources()
    out = []
    for r in db.query(f"SELECT * FROM tracks WHERE {where} status IN ('not_found','skipped','review') "
                      "ORDER BY source_key, position", params):
        artists = json.loads(r["artists"] or "[]")
        title = f"{r['title']} ({r['version']})" if r["version"] else r["title"]
        out.append([title, ", ".join(artists), r["album"] or "",
                    (allsrc.get(r["source_key"]) or {}).get("title", ""), STATUS_RU.get(r["status"], r["status"])])
    return _csv_response(["Track name", "Artist name", "Album", "Playlist name", "Статус"], out,
                         "not_found_all.csv" if all else "not_found.csv", delimiter=",")


@app.post("/reset")
def reset_all(request: Request, confirm: str = Form("")):
    if confirm != "да":
        return _flash(request, "Чтобы очистить данные, впишите «да»", "error")
    key = db.active()
    title = db.source(key).get("title", "текущий список")
    db.remove_source(key)
    return _flash(request, f"Список «{title}» очищен (настройки, вход и кэш поиска сохранены)")
