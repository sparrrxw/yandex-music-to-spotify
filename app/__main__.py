"""Запуск: python -m app  (откроет http://127.0.0.1:8888 в браузере по умолчанию)"""
import threading
import webbrowser

import uvicorn

from . import db

PORT = int((db.get("settings", {}) or {}).get("port", 8888))
URL = f"http://127.0.0.1:{PORT}"

if __name__ == "__main__":
    print(f"\n  Сервис запущен: {URL}\n  Откройте этот адрес в Brave. Для остановки закройте окно или нажмите Ctrl+C.\n")
    threading.Timer(1.5, lambda: webbrowser.open(URL)).start()
    uvicorn.run("app.main:app", host="127.0.0.1", port=PORT, log_level="warning")
