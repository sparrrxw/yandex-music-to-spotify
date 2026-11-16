"""Системные уведомления Windows («тосты») без дополнительных библиотек: через PowerShell и WinRT.

На других системах (и если PowerShell недоступен) молча ничего не делает: прогресс всё равно виден на странице.
"""
from __future__ import annotations

import base64
import subprocess
import sys
import threading
from xml.sax.saxutils import escape

# Уведомление показываем от имени PowerShell: этот AppID зарегистрирован в любой Windows 10/11.
APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

sent: list[tuple[str, str]] = []   # для тестов и отладки: последние уведомления


def _script(title: str, text: str) -> str:
    xml = (f"<toast><visual><binding template='ToastGeneric'><text>{escape(title)}</text>"
           f"<text>{escape(text)}</text><text>Перенос музыки · http://127.0.0.1:8888</text>"
           f"</binding></visual></toast>")
    return (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml(@'\n{xml}\n'@);"
        "$t = [Windows.UI.Notifications.ToastNotification]::new($x);"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_ID}').Show($t);"
    )


def send(title: str, text: str) -> None:
    text = (text or "")[:250]
    sent.append((title, text))
    del sent[:-20]
    if sys.platform != "win32":
        return
    encoded = base64.b64encode(_script(title, text).encode("utf-16-le")).decode()

    def run():
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                           timeout=20, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:  # noqa: BLE001  (уведомление — не главное, ошибки не мешают работе)
            pass

    threading.Thread(target=run, daemon=True, name="notify").start()
