@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Yandex Music to Spotify

rem --- 1) если установлен uv: он сам скачает нужный Python и библиотеки ---
set "UV="
where uv >nul 2>nul && set "UV=uv"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if defined UV (
  echo Запуск через uv (первый раз подготовка займёт минуту^)...
  "%UV%" run --frozen --no-dev python -m app
  goto done
)

rem --- 2) портативная версия со встроенным Python ---
if exist "python\python.exe" (
  set "PY=%~dp0python\python.exe"
  goto run
)

rem --- 3) установленный Python и своё окружение .venv ---
if not exist .venv (
  echo Создаю виртуальное окружение...
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
  if not exist .venv\Scripts\python.exe (
    echo Не найден ни uv, ни Python.
    echo Установите uv одной командой в PowerShell:
    echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    echo и запустите START.bat ещё раз.
    pause
    exit /b 1
  )
  .venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -r requirements.txt
)
set "PY=%~dp0.venv\Scripts\python.exe"
:run
"%PY%" -m app
:done
if errorlevel 1 (
  echo.
  echo Сервис завершился с ошибкой. Сделайте скриншот этого окна и отправьте его автору.
)
pause
