@echo off
setlocal
cd /d "%~dp0"

if not defined JAVA_HOME (
  if exist "%ProgramFiles%\Android\Android Studio\jbr\bin\java.exe" (
    set "JAVA_HOME=%ProgramFiles%\Android\Android Studio\jbr"
  ) else (
    echo Imposta JAVA_HOME su un JDK 17 o successivo.
    pause
    exit /b 1
  )
)

if not defined ANDROID_HOME set "ANDROID_HOME=%LOCALAPPDATA%\Android\Sdk"
if not defined ANDROID_SDK_ROOT set "ANDROID_SDK_ROOT=%ANDROID_HOME%"

where gradle >nul 2>nul
if not errorlevel 1 goto usePathGradle
if not exist "C:\gradle-9.6.1\bin\gradle.bat" goto noGradle

call "C:\gradle-9.6.1\bin\gradle.bat" --no-daemon assembleDebug
if errorlevel 1 goto buildFailed
goto buildSucceeded

:usePathGradle
gradle --no-daemon assembleDebug
if errorlevel 1 goto buildFailed
goto buildSucceeded

:noGradle
echo Gradle non trovato. Installa Gradle 9.6.1 o apri il progetto in Android Studio.
pause
exit /b 1

:buildFailed
echo Compilazione APK non riuscita. Controlla JDK, Android SDK e Python 3.11.
pause
exit /b 1

:buildSucceeded
copy /y "app\build\outputs\apk\debug\app-debug.apk" "Teenix-AndroidTV.apk" >nul
if errorlevel 1 (
  echo APK compilato ma non e' stato possibile copiarlo nella cartella del progetto.
  pause
  exit /b 1
)

echo.
echo APK creato: Teenix-AndroidTV.apk
pause
