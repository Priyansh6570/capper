@echo off
setlocal EnableDelayedExpansion
title ReCapper - Setup
cd /d "%~dp0"

set "LOGFILE=%~dp0setup_log.txt"
set "VENV_DIR=%~dp0.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "FFMPEG_DIR=%~dp0vendor\ffmpeg"
set "FFMPEG_BIN=%FFMPEG_DIR%\bin"
set "MIN_VRAM_MB=6000"
set "PYEXE="
set "STEP_FAILED=0"
set "TMPFILE=%TEMP%\manhwa_setup_tmp.txt"

echo ===== ReCapper - setup started %DATE% %TIME% ===== > "%LOGFILE%"

REM Git refuses to run any command in a repo folder whose ownership looks
REM unexpected ("dubious ownership") - seen on some drives/install locations
REM and breaks the in-app "Check for updates" button. Mark this exact folder
REM trusted for this user; harmless/no-op if this isn't a git checkout or
REM it's already trusted. Covers manual `git clone` installs (the installer
REM does its own equivalent step right after cloning).
if exist "%~dp0.git" (
    where git >nul 2>&1
    if not errorlevel 1 (
        git config --global --add safe.directory "%~dp0" >nul 2>&1
    )
)

echo.
echo ============================================================
echo   ReCapper - one-time setup
echo ============================================================
echo   This will:
echo     1. Check for an NVIDIA GPU + driver
echo     2. Make sure Python 3.13 is installed
echo     3. Create a private Python environment for this app (.venv)
echo     4. Install the AI/video packages it needs (this is the slow
echo        part - it downloads several GB and can take 10-30+ minutes
echo        depending on your internet connection)
echo     5. Make sure ffmpeg (video encoder) is available
echo     6. Double-check everything actually works
echo.
echo   You need an internet connection for this. Everything it does
echo   is logged to setup_log.txt if you need to troubleshoot later.
echo ============================================================
echo.
call :maybe_pause

REM ==========================================================================
REM Step 1: NVIDIA GPU + driver
REM ==========================================================================
echo.
echo [1/6] Checking for an NVIDIA GPU + driver...
call :log "[1/6] Checking for an NVIDIA GPU + driver..."
where nvidia-smi >nul 2>&1
if errorlevel 1 (
    echo   [WARN] No NVIDIA GPU / driver detected ^(nvidia-smi not found^).
    echo          This app can still run, but voice generation will be
    echo          MUCH slower on CPU only ^(minutes instead of seconds
    echo          per line^).
    echo          If you DO have an NVIDIA GPU, install/update its driver
    echo          from: https://www.nvidia.com/Download/index.aspx
    call :log "  [WARN] nvidia-smi not found - no NVIDIA GPU/driver detected."
    if "%SETUP_UNATTENDED%"=="1" (
        echo          Continuing automatically in CPU-only mode ^(unattended install^).
        call :log "  Continuing automatically in CPU-only mode (unattended)."
    ) else (
        choice /c YN /n /m "  Continue anyway in slow CPU-only mode? [Y/N]: "
        if errorlevel 2 (
            echo   Stopping. Install/update your NVIDIA driver, then run setup.bat again.
            call :log "  User chose to stop - no NVIDIA driver."
            goto :fail
        )
    )
) else (
    set "GPU_NAME=unknown"
    set "GPU_VRAM="
    nvidia-smi --query-gpu=name --format=csv,noheader >"%TMPFILE%" 2>>"%LOGFILE%"
    set /p GPU_NAME=<"%TMPFILE%"
    nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits >"%TMPFILE%" 2>>"%LOGFILE%"
    set /p GPU_VRAM=<"%TMPFILE%"
    echo   [OK] Found GPU: !GPU_NAME! ^(!GPU_VRAM! MB VRAM^)
    call :log "  [OK] Found GPU: !GPU_NAME! (!GPU_VRAM! MB VRAM)"
    set "VRAM_NUM="
    set /a "VRAM_NUM=!GPU_VRAM!" 2>nul
    if defined VRAM_NUM (
        if !VRAM_NUM! LSS %MIN_VRAM_MB% (
            echo   [WARN] Your GPU has less than ~6GB VRAM. This app was built
            echo          and tested on a 4GB laptop GPU and DOES work on it
            echo          ^(it automatically falls back to CPU for anything
            echo          that runs out of GPU memory^), just a bit slower.
            call :log "  [WARN] VRAM below %MIN_VRAM_MB% MB - expect occasional CPU fallback."
        )
    )
)
del "%TMPFILE%" >nul 2>&1

REM ==========================================================================
REM Step 2: Python 3.13
REM ==========================================================================
echo.
echo [2/6] Checking for Python 3.13...
call :log "[2/6] Checking for Python 3.13..."
call :find_python
if not defined PYEXE (
    echo   Python 3.13 not found. Trying to install it automatically via winget...
    call :log "  Python 3.13 not found - attempting winget install."
    where winget >nul 2>&1
    if errorlevel 1 (
        echo   [FAIL] winget is not available on this PC, so it can't be installed
        echo          automatically.
        echo          Please install Python 3.13 yourself from:
        echo            https://www.python.org/downloads/
        echo          IMPORTANT: on the installer's first screen, tick
        echo          "Add python.exe to PATH", then run setup.bat again.
        call :log "  [FAIL] winget not available."
        goto :fail
    )
    winget install -e --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements >>"%LOGFILE%" 2>&1
    call :find_python
    if not defined PYEXE (
        echo   [FAIL] Automatic install did not finish where this script could
        echo          find it. Please install Python 3.13 yourself from:
        echo            https://www.python.org/downloads/
        echo          IMPORTANT: tick "Add python.exe to PATH" on install,
        echo          then run setup.bat again.
        call :log "  [FAIL] Python still not found after winget install."
        goto :fail
    )
    echo   [OK] Python 3.13 installed.
    call :log "  [OK] Python 3.13 installed via winget."
) else (
    echo   [OK] Found Python 3.13 ^(!PYEXE!^)
    call :log "  [OK] Found Python 3.13: !PYEXE!"
)

REM ==========================================================================
REM Step 3: virtual environment
REM ==========================================================================
echo.
echo [3/6] Setting up the app's private Python environment...
call :log "[3/6] Setting up .venv..."
if exist "%VENV_PY%" (
    echo   [OK] .venv already exists, reusing it.
    call :log "  [OK] .venv already exists."
) else (
    echo   Creating .venv ^(this is quick^)...
    !PYEXE! -m venv "%VENV_DIR%" >>"%LOGFILE%" 2>&1
    if not exist "%VENV_PY%" (
        echo   [FAIL] Could not create the virtual environment. See setup_log.txt.
        call :log "  [FAIL] venv creation failed."
        goto :fail
    )
    echo   [OK] Created .venv
    call :log "  [OK] Created .venv"
)

echo   Upgrading pip...
"%VENV_PY%" -m pip install --upgrade pip >>"%LOGFILE%" 2>&1

REM ==========================================================================
REM Step 4: install packages (CUDA torch FIRST, then the rest)
REM ==========================================================================
echo.
echo [4/6] Installing packages ^(this is the slow step - several GB,
echo       can take 10-30+ minutes depending on your internet^)...
call :log "[4/6] Installing packages..."

echo   4a. Installing the app's packages ^(this pulls in a generic,
echo       non-CUDA PyTorch as a side effect - step 4b fixes that^)...
powershell -NoProfile -Command "& { & '%VENV_PY%' -m pip install -r '%~dp0requirements.txt' 2>&1 | Tee-Object -FilePath '%LOGFILE%' -Append }"
if errorlevel 1 (
    echo   [FAIL] Package install failed. See setup_log.txt for the error.
    call :log "  [FAIL] requirements.txt install failed."
    goto :fail
)
echo   [OK] All packages installed.
call :log "  [OK] All packages installed."

echo   4b. Switching PyTorch to the CUDA build - watch this window for progress...
echo       ^(one of the AI packages above pins a specific plain-CPU
echo       PyTorch version as ITS OWN dependency; this step deliberately
echo       overrides that with the matching CUDA build instead - this is
echo       expected and the app is tested working this way^)...
powershell -NoProfile -Command "& { & '%VENV_PY%' -m pip install --upgrade --index-url https://download.pytorch.org/whl/cu128 torch torchaudio 2>&1 | Tee-Object -FilePath '%LOGFILE%' -Append }"
if errorlevel 1 (
    echo   [FAIL] Installing the PyTorch CUDA build failed. See setup_log.txt.
    call :log "  [FAIL] torch/torchaudio (cu128) install failed."
    goto :fail
)
echo   [OK] PyTorch CUDA build installed.
call :log "  [OK] PyTorch CUDA build installed (step 4b)."

REM ==========================================================================
REM Step 5: ffmpeg
REM ==========================================================================
echo.
echo [5/6] Checking for ffmpeg ^(video encoder^)...
call :log "[5/6] Checking for ffmpeg..."
where ffmpeg >nul 2>&1
if not errorlevel 1 (
    echo   [OK] ffmpeg is already on this PC's PATH.
    call :log "  [OK] ffmpeg found on PATH."
) else if exist "%FFMPEG_BIN%\ffmpeg.exe" (
    echo   [OK] ffmpeg already downloaded for this app.
    call :log "  [OK] ffmpeg already present in vendor\ffmpeg."
) else (
    echo   ffmpeg not found - downloading a copy just for this app
    echo   ^(this does NOT change your system - it's saved inside this
    echo   app's own folder^)...
    call :log "  ffmpeg not found - downloading."
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$ErrorActionPreference='Stop'; $zip = Join-Path $env:TEMP 'manhwa_ffmpeg.zip'; Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $zip; $extract = Join-Path $env:TEMP 'manhwa_ffmpeg_extract'; if (Test-Path $extract) { Remove-Item $extract -Recurse -Force }; Expand-Archive -Path $zip -DestinationPath $extract -Force; $exe = Get-ChildItem -Path $extract -Filter ffmpeg.exe -Recurse | Select-Object -First 1; if (-not $exe) { throw 'ffmpeg.exe not found in downloaded archive' }; $bin = $exe.DirectoryName; New-Item -ItemType Directory -Force -Path '%FFMPEG_BIN%' | Out-Null; Copy-Item (Join-Path $bin '*') '%FFMPEG_BIN%' -Force; Remove-Item $zip -Force; Remove-Item $extract -Recurse -Force" >>"%LOGFILE%" 2>&1
    if not exist "%FFMPEG_BIN%\ffmpeg.exe" (
        echo   [FAIL] Could not download/extract ffmpeg automatically.
        echo          Download a build yourself from https://www.gyan.dev/ffmpeg/builds/
        echo          ^(the "essentials" build^), then copy its bin\ folder
        echo          contents into: %FFMPEG_BIN%
        call :log "  [FAIL] ffmpeg auto-download failed."
        goto :fail
    )
    echo   [OK] ffmpeg downloaded to vendor\ffmpeg\bin
    call :log "  [OK] ffmpeg downloaded to vendor\ffmpeg\bin"
)

REM ==========================================================================
REM Step 6: final verification
REM ==========================================================================
echo.
echo [6/6] Verifying everything works...
call :log "[6/6] Final verification..."
set "PATH=%FFMPEG_BIN%;%PATH%"

set "CUDA_OK=0"
set "CUDA_RESULT="
"%VENV_PY%" -c "import torch; print('YES' if torch.cuda.is_available() else 'NO')" >"%TMPFILE%" 2>>"%LOGFILE%"
set /p CUDA_RESULT=<"%TMPFILE%"
del "%TMPFILE%" >nul 2>&1
if "!CUDA_RESULT!"=="YES" set "CUDA_OK=1"

set "FFMPEG_OK=0"
ffmpeg -version >>"%LOGFILE%" 2>&1
if not errorlevel 1 set "FFMPEG_OK=1"

echo.
echo ============================================================
if "!CUDA_OK!"=="1" (
    echo   [PASS] GPU acceleration ^(torch.cuda.is_available = True^)
) else (
    echo   [WARN] GPU acceleration is NOT active ^(will run on CPU - slow^).
    echo          Try re-running setup.bat. If it still fails, open a
    echo          Command Prompt in this folder and run:
    echo            .venv\Scripts\python -m pip install --upgrade --index-url https://download.pytorch.org/whl/cu128 torch torchaudio
    echo          See setup_log.txt for details.
)
if "!FFMPEG_OK!"=="1" (
    echo   [PASS] ffmpeg runs correctly
) else (
    echo   [FAIL] ffmpeg did not run correctly. See setup_log.txt.
)
echo ============================================================
echo.

if "!FFMPEG_OK!"=="0" goto :fail

echo   SETUP COMPLETE.
call :log "SETUP COMPLETE."
echo.
echo   Note: the FIRST time you click "Generate audio + video" in the
echo   app, it will download the AI voice model ^(a few GB, one time
echo   only^) - that part needs internet and can take several minutes.
echo   After that first time it's cached on your PC and stays fast.
echo.
echo   Next: double-click start.bat any time you want to use the app.
echo ============================================================
call :maybe_pause
exit /b 0

:fail
echo.
echo ============================================================
echo   SETUP DID NOT FINISH. See the messages above and
echo   setup_log.txt for details. Fix the issue and run
echo   setup.bat again - it's safe to re-run.
echo ============================================================
call :log "SETUP FAILED."
call :maybe_pause
exit /b 1

REM ==========================================================================
REM Subroutines
REM ==========================================================================
:log
>>"%LOGFILE%" echo %~1
exit /b 0

REM Skips the "Press any key..." prompt when launched unattended (e.g. by the
REM Windows installer, which sets SETUP_UNATTENDED=1) - a normal double-click
REM run still pauses so the console window doesn't vanish before it's read.
:maybe_pause
if not "%SETUP_UNATTENDED%"=="1" pause
exit /b 0

:find_python
set "PYEXE="
py -3.13 -c "" >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=py -3.13"
    exit /b 0
)
set "FOUND_VER="
for /f "tokens=2 delims= " %%V in ('python --version 2^>^&1') do set "FOUND_VER=%%V"
if "!FOUND_VER:~0,4!"=="3.13" (
    set "PYEXE=python"
    exit /b 0
)
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    exit /b 0
)
if exist "%ProgramFiles%\Python313\python.exe" (
    set "PYEXE=%ProgramFiles%\Python313\python.exe"
    exit /b 0
)
exit /b 1
