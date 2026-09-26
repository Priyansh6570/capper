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
- **An internet connection** for the one-time setup — it downloads a few GB
  of software plus the AI voice model (also a few GB), all during setup, so
  the app never has to pause to download anything the first time you use it.
- A few GB of free disk space.

## One-time setup

1. Double-click **`setup.bat`** in this folder.
2. A black window will open and explain what it's doing at each step. This
   can take **10 to 30+ minutes** depending on your internet speed — most of
   that time is downloading PyTorch (the AI library) and the AI voice engine's
   model weights (step 5, several GB). It's normal for it to look "stuck" for
   a while during a download; as long as the window is still open, it's
   working.
   - Step 5 will ask if you have a free Hugging Face token. You can skip this
     (press Enter), but adding one makes the voice-model download much faster
     and less likely to get throttled — see "Speeding up / troubleshooting the
     voice model download" below.
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

1. Double-click **`start.bat`** (or your desktop shortcut, if you installed
   with the Windows installer).
2. No black window stays open — your web browser opens automatically to the
   tool after a few seconds. Look for the **ReCapper icon in your system
   tray** (bottom-right of the taskbar, near the clock; click the little "^"
   arrow there if you don't see it right away) — that means the app is
   running.
3. Use the app in your browser as normal.
4. **To stop the app**, right-click that tray icon and choose **Quit
   ReCapper**. There's no window to close and Ctrl+C doesn't apply anymore —
   the tray icon is the only control now that the app runs in the
   background.

Tip: right-click `start.bat` → **Send to → Desktop (create shortcut)** so you
can launch it from your desktop. You can also right-click the shortcut →
**Properties → Change Icon** to pick a nicer icon.

## Getting updates

The Projects screen (the first thing you see when the app opens) has a
**"Check for updates"** button. Click it any time — it tells you whether
you're already up to date, or that it updated some files. If it says
**"dependencies changed - please re-run setup.bat"**, do exactly that
(one-time, like the original setup) before you keep using the app. Either
way, **quit ReCapper from its tray icon and reopen `start.bat`** afterward so
the update actually takes effect — the app doesn't reload itself
automatically.

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

**Speeding up / troubleshooting the voice model download (step 5)**
Step 5 downloads the AI voice engine's (Chatterbox) model weights — several
GB — once, during setup, instead of on your first render. Hugging Face
(where the weights are hosted) heavily rate-limits anonymous downloads, so
without a token this step can be slow; it automatically retries on a
stalled/throttled connection (resuming, not restarting, each time), but a
token avoids the throttling in the first place:

1. Get a free account + a "Read" access token at
   <https://huggingface.co/settings/tokens>.
2. The next time you run `setup.bat`, paste it in when step 5 asks. It's
   saved to a `.env` file in this folder (never uploaded anywhere, never
   committed if this is a git checkout) so you're only asked once.
3. Already have a `.env` with a wrong/expired token? Edit that line by hand
   (`HF_TOKEN=...`) or delete it, then run `setup.bat` again.

If step 5 still can't finish after every retry (e.g. no usable internet
connection on this PC at all), setup still completes — the app just falls
back to downloading on first use, same as before this step existed. As a
last resort, you can sideload the model files yourself on a PC/USB stick
with a working connection:

1. On a machine with internet, install `huggingface_hub` (`pip install
   huggingface_hub`) and run:
   ```
   huggingface-cli download ResembleAI/chatterbox-turbo --local-dir chatterbox-turbo
   huggingface-cli download ResembleAI/chatterbox --local-dir chatterbox
   ```
2. Copy those two folders' contents into this PC's Hugging Face cache, under:
   ```
   %USERPROFILE%\.cache\huggingface\hub\models--ResembleAI--chatterbox-turbo\
   %USERPROFILE%\.cache\huggingface\hub\models--ResembleAI--chatterbox\
   ```
   matching the same internal structure the cache already uses (a
   `snapshots\<hash>\` folder of files, plus a `blobs\` folder they point
   to) — the simplest way to get that structure right is to run
   `huggingface-cli download` directly INTO a `.cache\huggingface\hub`
   folder with `--cache-dir` instead of `--local-dir`, then copy that whole
   `hub` folder over.
3. Run `setup.bat` again (or just `start.bat` and generate audio) — the app
   finds the cached model and never touches the network for it.

**Setup got partway through and failed**
Just double-click `setup.bat` again. It checks what's already done (Python,
the app's private environment, the AI packages, ffmpeg) and skips anything
already in place, so re-running only redoes the step that failed.

**The browser opened to a page that wouldn't load**
The app can take a few seconds to start. Just refresh the page (or reopen
<http://127.0.0.1:5005/>) — if the ReCapper icon is still in your system
tray, the app is running.

**Where does my work get saved?**
Everything you download, box, and render lives inside this same folder (under
`work/`, `output/`, and `projects/`) — nothing is uploaded anywhere except
what you explicitly choose to (downloading a chapter, or the one-time model
downloads described above).
