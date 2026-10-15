@echo off
setlocal
title ReCapper
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "VENV_PYW=%~dp0.venv\Scripts\pythonw.exe"
set "FFMPEG_BIN=%~dp0vendor\ffmpeg\bin"

if not exist "%VENV_PY%" (
    call :startup_error "Setup has not been run on this PC yet.|To fix it: double-click setup.bat in the ReCapper folder (one-time, needs internet), then start ReCapper again."
    exit /b 1
)

if not exist "%VENV_PYW%" (
    call :startup_error "ReCapper's Python environment (.venv) is damaged: pythonw.exe is missing.|To fix it: delete the .venv folder in the ReCapper folder, then double-click setup.bat to rebuild it."
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
exit /b 0

REM This script usually runs hidden (start_hidden.vbs), where a console message or
REM `pause` would never be seen: log the failure and show a real dialog instead.
REM "|" in the message becomes a line break.
:startup_error
>>"%~dp0app_log.txt" echo [%DATE% %TIME%] start.bat: "%~1"
set "RECAPPER_MSG=%~1||Details are logged in:|%~dp0app_log.txt"
powershell -NoProfile -WindowStyle Hidden -Command "Add-Type -AssemblyName System.Windows.Forms; [void][System.Windows.Forms.MessageBox]::Show(($env:RECAPPER_MSG -replace '\|', [Environment]::NewLine), 'ReCapper could not start', 'OK', 'Error')"
exit /b 0
