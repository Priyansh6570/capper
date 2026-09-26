@echo off
setlocal
title ReCapper
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "VENV_PYW=%~dp0.venv\Scripts\pythonw.exe"
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

if not exist "%VENV_PYW%" (
    echo.
    echo ============================================================
    echo   pythonw.exe is missing from this app's Python environment
    echo   ^(.venv^). Delete the .venv folder in this app folder and run
    echo   setup.bat again to rebuild it.
    echo ============================================================
    pause
    exit /b 1
)

REM Use the app's own ffmpeg copy if we downloaded one during setup; harmless
REM if that folder doesn't exist (a system-installed ffmpeg is still found).
set "PATH=%FFMPEG_BIN%;%PATH%"

REM Pick up an HF_TOKEN saved by setup.bat's voice-model pre-download step
REM (see .env, gitignored) so the SAME faster/less-throttled Hugging Face
REM access applies if the app ever still has to touch the network for the
REM model (e.g. setup's pre-download didn't fully finish). No-op if .env
REM doesn't exist or has no HF_TOKEN line.
if exist "%~dp0.env" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0.env") do (
        if /i "%%A"=="HF_TOKEN" set "HF_TOKEN=%%B"
    )
)

REM Starts the server with pythonw.exe (no console subsystem at all - unlike
REM python.exe, it never has a window to show or close) via `start`, so this
REM script hands off and exits immediately instead of sitting there as "the
REM app's window". tools/framer/tray_launcher.py is what actually runs the
REM server + opens the browser + gives you a system tray icon ("ReCapper") -
REM that icon's "Quit ReCapper" is now how you stop the app, since there's no
REM window left to close. Launched from the desktop shortcut (start_hidden.vbs)
REM this whole script itself never shows a window either; double-clicking
REM start.bat directly still flashes one very briefly (unavoidable for any
REM .bat file) but it closes itself right away.
start "" "%VENV_PYW%" "%~dp0tools\framer\tray_launcher.py"
