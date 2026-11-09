@echo off
setlocal
cd /d "%~dp0"

echo Avvio del server StreamScrap...
echo Apri questo indirizzo nel browser: http://127.0.0.1:8000
echo Per spegnerlo in modo pulito, premi CTRL+C.
echo.

where py >nul 2>nul
if not errorlevel 1 (
  py -3 server.py --host 127.0.0.1 --port 8000
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python non trovato. Installa Python 3 e riprova.
    pause
    exit /b 1
  )
  python server.py --host 127.0.0.1 --port 8000
)
