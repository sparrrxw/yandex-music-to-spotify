# Обновление программы до последней версии с GitHub.
# Папки data (ваш прогресс и токены) и python (встроенный Python) не трогаются.
$ErrorActionPreference = 'Stop'
$Repo   = 'sparrrxw/yandex-music-to-spotify'
$Branch = 'main'
$Root   = Split-Path -Parent $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

try {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ((Test-Path (Join-Path $Root '.git')) -and $git) {
        Write-Host 'Обновляю через git pull...'
        & git -C $Root pull --ff-only
        if ($LASTEXITCODE -ne 0) { throw 'git pull не удался (есть локальные изменения?)' }
    } else {
        Write-Host "Скачиваю свежую версию с github.com/$Repo ..."
        $tmp = Join-Path $env:TEMP ('tm-update-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $tmp | Out-Null
        $zip = Join-Path $tmp 'src.zip'
        Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$Repo/archive/refs/heads/$Branch.zip" -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $src = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1
        if (-not $src -or -not (Test-Path (Join-Path $src.FullName 'app'))) { throw 'в скачанном архиве нет папки app' }
        # Старый код приложения удаляем целиком, чтобы не остались удалённые в новой версии файлы.
        foreach ($d in 'app', 'tests', 'tools') {
            $p = Join-Path $Root $d
            if ((Test-Path $p) -and ($d -ne 'tools')) { Remove-Item -Recurse -Force $p }
        }
        Get-ChildItem -Path $src.FullName -Force | ForEach-Object {
            if ($_.Name -in @('data', 'python', '.venv')) { return }
            Copy-Item -Path $_.FullName -Destination $Root -Recurse -Force
        }
        Remove-Item -Recurse -Force $tmp
    }

    $py = Join-Path $Root 'python\python.exe'
    if (-not (Test-Path $py)) { $py = Join-Path $Root '.venv\Scripts\python.exe' }
    if (Test-Path $py) {
        Write-Host 'Проверяю библиотеки...'
        & $py -m pip install -q --disable-pip-version-check -r (Join-Path $Root 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'не удалось обновить библиотеки' }
    }
    Write-Host ''
    Write-Host 'Готово! Запускайте START.bat. Ваш прогресс сохранён.' -ForegroundColor Green
} catch {
    Write-Host ''
    Write-Host ('Обновить не получилось: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Проверьте интернет и попробуйте ещё раз. Старая версия продолжает работать.'
    exit 1
}
