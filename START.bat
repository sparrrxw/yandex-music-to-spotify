@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Yandex Music to Spotify
if exist "python\python.exe" (
  set "PY=%~dp0python\python.exe"
  goto run
)
rem --- без встроенного Python: используем установленный и своё окружение .venv ---
if not exist .venv (
  echo Создаю виртуальное окружение...
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
  if not exist .venv\Scripts\python.exe (
    echo Не найден Python. Скачайте портативную версию или установите Python 3.10+ с python.org.
    pause
    exit /b 1
  )
  .venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -r requirements.txt
)
set "PY=%~dp0.venv\Scripts\python.exe"
:run
"%PY%" -m app
if errorlevel 1 (
  echo.
  echo Сервис завершился с ошибкой. Сделайте скриншот этого окна и отправьте его автору.
)
pause
