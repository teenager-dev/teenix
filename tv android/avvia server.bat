@echo off
setlocal
cd /d "%~dp0.."

set "PYTHON=python"
where py >nul 2>nul
if not errorlevel 1 set "PYTHON=py -3"

set "TEENIX_LAN_IP="
for /f %%I in ('%PYTHON% "%~dp0lan_ip.py"') do set "TEENIX_LAN_IP=%%I"
if not defined TEENIX_LAN_IP (
  echo Impossibile individuare l'indirizzo LAN del PC.
  echo Collega il PC alla stessa rete della TV e riprova.
  pause
  exit /b 1
)

echo Avvio Teenix per la TV Android...
echo Il server ascoltera' solo sull'indirizzo LAN del PC: %TEENIX_LAN_IP%
echo Assicurati che TV e computer siano collegati alla stessa rete Wi-Fi.
echo.
echo Nella TV inserisci http://%TEENIX_LAN_IP%:8000
echo Se Windows chiede l'accesso, consenti Python solo sulle reti private.
echo Per fermare il server premi CTRL+C.
echo.

%PYTHON% server.py --host %TEENIX_LAN_IP% --port 8000
