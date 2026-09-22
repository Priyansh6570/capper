# Setup Guide (Windows)

This guide is for running the manhwa recap tool on a Windows PC. You do not
need to know how to code — just follow the steps below.

## What you need

- **Windows 10 or 11.**
- **An NVIDIA graphics card** (GeForce/RTX), ideally with **6GB of VRAM or
  more**. This is what makes voiceover generation fast. A 4GB card (e.g. an
  RTX 3050 laptop GPU) also works, just a bit slower — the app automatically
  falls back to your CPU for anything that doesn't fit in GPU memory. No
  NVIDIA card at all? The app still runs, but voice generation will be much
  slower (minutes instead of seconds per line).
- **An internet connection** for the one-time setup (it downloads a few GB of
  software) and for the first time you generate audio in the app (it
  downloads the AI voice model, also a few GB, one time only).
- A few GB of free disk space.

## One-time setup

1. Double-click **`setup.bat`** in this folder.
2. A black window will open and explain what it's doing at each step. This
   can take **10 to 30+ minutes** depending on your internet speed — most of
   that time is downloading PyTorch (the AI library) and the AI voice engine.
   It's normal for it to look "stuck" for a while during a download; as long
   as the window is still open, it's working.
3. When it finishes, it prints either:
   - **`SETUP COMPLETE`** — you're done, go to "Every time you want to use
     it" below.
   - **`SETUP DID NOT FINISH`** — something needs fixing. Read the message
     above it (it tells you exactly what to do), check the common problems
     below, fix it, then double-click `setup.bat` again. It's safe to run
     more than once — it picks up where it left off instead of starting
     over.
4. Everything the setup script does is written to **`setup_log.txt`** in this
   folder. If something goes wrong and you need help, that file has the
   details.

## Every time you want to use it

1. Double-click **`start.bat`**.
2. A black window opens (this is the app running) and your web browser opens
   automatically to the tool after a few seconds.
3. Use the app in your browser as normal.
4. **The black window is the app** — leave it open while you're working.
   When you're done, close that window (or press Ctrl+C inside it) to stop
   the app.

Tip: right-click `start.bat` → **Send to → Desktop (create shortcut)** so you
can launch it from your desktop. You can also right-click the shortcut →
**Properties → Change Icon** to pick a nicer icon.

## Getting updates

The Projects screen (the first thing you see when the app opens) has a
**"Check for updates"** button. Click it any time — it tells you whether
you're already up to date, or that it updated some files. If it says
**"dependencies changed - please re-run setup.bat"**, do exactly that
(one-time, like the original setup) before you keep using the app. Either
way, **close and reopen `start.bat`** afterward so the update actually takes
effect — the app doesn't reload itself automatically.

## Common problems

**"Setup hasn't been run yet on this PC" when I click start.bat**
Run `setup.bat` first — `start.bat` is only for daily use after setup has
finished successfully once.

**GPU acceleration is NOT active / voice generation is very slow**
This usually means one of:
- Your NVIDIA driver is missing or too old. Download the latest one from
  <https://www.nvidia.com/Download/index.aspx>, install it, restart your PC,
  then run `setup.bat` again.
- PyTorch installed the wrong (CPU-only) build. Open a Command Prompt in this
  folder and run:
  ```
  .venv\Scripts\python -m pip install --upgrade --index-url https://download.pytorch.org/whl/cu128 torch torchaudio
  ```
  then run `setup.bat` again to re-verify.

**My antivirus flagged or blocked setup.bat / start.bat / ffmpeg**
This is a false positive some antivirus software gives to batch scripts that
download files and install Python packages — that's normal behavior for a
setup script, not a sign of a virus. In your antivirus, allow this folder (or
restore the quarantined file) and try again. If you're not comfortable doing
that, ask whoever set this PC up to verify the script with you first.

**My GPU only has 4GB of VRAM — will this work?**
Yes. This app was originally built and tuned on exactly that (an RTX 3050
laptop GPU with 4GB). Setup will print a warning, not an error — it still
works, it just automatically drops to your CPU on any line that doesn't fit
in GPU memory.

**Setup got partway through and failed**
Just double-click `setup.bat` again. It checks what's already done (Python,
the app's private environment, the AI packages, ffmpeg) and skips anything
already in place, so re-running only redoes the step that failed.

**The browser opened to a page that wouldn't load**
The app can take a few seconds to start. Just refresh the page (or reopen
<http://127.0.0.1:5005/>) — if the black window from `start.bat` is still
open and hasn't shown an error, the app is running.

**Where does my work get saved?**
Everything you download, box, and render lives inside this same folder (under
`work/`, `output/`, and `projects/`) — nothing is uploaded anywhere except
what you explicitly choose to (downloading a chapter, or the one-time model
downloads described above).
