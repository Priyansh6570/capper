@echo off
setlocal
title ReCapper
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "FFMPEG_BIN=%~dp0vendor\ffmpeg\bin"

if not exist "%VENV_PY%" (
    echo.
    echo ============================================================
    echo   Setup hasn't been run yet on this PC.
    echo   Double-click setup.bat first ^(one-time, needs internet^),
    echo   then come back and use start.bat.
    echo ============================================================
    pause
    exit /b 1
)

REM Use the app's own ffmpeg copy if we downloaded one during setup; harmless
REM if that folder doesn't exist (a system-installed ffmpeg is still found).
set "PATH=%FFMPEG_BIN%;%PATH%"

echo.
echo ============================================================
echo   Starting ReCapper...
echo   Your browser will open automatically in a few seconds.
echo.
echo   Note: the FIRST time you click "Generate audio + video", the
echo   AI voice model downloads (a few GB, one time only, needs
echo   internet) - after that it's cached and stays fast.
echo.
echo   THIS WINDOW IS THE APP - keep it open while you work.
echo   Close this window (or press Ctrl+C) when you're done.
echo   If port 5005 is already taken, this window will say so and
echo   pick another port automatically - the browser still opens to
echo   the right address either way.
echo ============================================================
echo.

REM app.py opens the browser itself (see main() in tools/framer/app.py) -
REM it's the one that actually knows which port it ended up on, since a
REM busy 5005 makes it fall back to a different one. A blind `Start-Process`
REM to a hardcoded URL here would open the wrong page in that case.
"%VENV_PY%" "%~dp0tools\framer\app.py"

echo.
echo App stopped.
pause
