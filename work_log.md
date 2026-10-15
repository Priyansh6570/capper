# Work log - missing dependencies, update flow, startup failures

## What was wrong
`tools/framer/bubbles.py` imports `cv2` and `stages/tts_kokoro.py` imports `kokoro`, but neither
was in `requirements.txt` (kokoro was only in setup.bat, so the in-app "missing packages" check
never saw it). Your venv hid it.

## Missing from requirements (found by AST scan of every tracked .py, not your venv)
| Import | Package | Imported by |
|---|---|---|
| cv2 | opencv-python-headless | tools/framer/bubbles.py, stages/s2a_vision.py |
| kokoro | kokoro + misaki (now `requirements-nodeps.txt`, see below) | stages/tts_kokoro.py |
| groq | groq | llm.py, stages/s3_context.py |
| httpx | httpx | llm.py |
| huggingface_hub | huggingface_hub | stages/s6_tts.py, tools/predownload_tts_models.py |
| chatterbox | chatterbox-tts | stages/s6_tts.py (was setup.bat-only; now `requirements-nodeps.txt`) |
| (none) | moviepy | not imported anywhere (see below) |

Optional, deliberately NOT listed (every import is try/except guarded): ultralytics, easyocr,
pytesseract, paddleocr. `kaggle/` is skipped (runs in Kaggle's environment).

## Listed but not imported by any file
Direct: **moviepy** (+ its deps imageio, imageio-ffmpeg, proglog, decorator) - s7 says "MoviePy is not used";
**webtoon-downloader** (run as an executable via `shutil.which`, so it IS needed).
The rest (spacy, loguru, addict, num2words, misaki, librosa, s3tokenizer, transformers, diffusers,
resemble-perth, conformer, safetensors, spacy-pkuseg, pykakasi, pyloudnorm, omegaconf) are runtime
deps of kokoro/misaki/chatterbox. Nothing was removed; moviepy is a candidate if you want it gone.

## Non-pip dependencies
- **espeak-ng**: not needed - `espeakng-loader`'s wheel bundles it.
- **ffmpeg**: setup.bat downloads it to `vendor/ffmpeg`; in-app check reports it.
- **git**: needed by Check for updates; installer installs it; setup.bat + in-app check now report it.
- **Claude Code CLI**: only for the Claude script engine; setup.bat, the installer's finish page and
  the in-app check now report it (with the install + `claude auth login` steps).
- Tesseract binary: only for the optional OCR path.

## Changes
- `requirements.txt`: + opencv-python-headless, httpx, groq, huggingface_hub, and kokoro's runtime deps incl. scipy and regex (nothing imports them directly, but kokoro/misaki need them).
- `requirements-nodeps.txt` (new): chatterbox-tts, moviepy, kokoro, misaki (installed `--no-deps`). kokoro could NOT go in requirements.txt: it declares `misaki>=0.7.16` but the app runs misaki 0.7.4, so a normal install fails with ResolutionImpossible (found by the fresh-venv test). The hook, `deps.py check` and the in-app check all treat this file as part of the requirements.
- `deps.py` (new): single `check`/`install`. Installs torch CUDA first, then requirements under a
  constraints file freezing torch/torchaudio, then the no-deps file; verifies `torch.cuda.is_available()`
  and prints a plain `[WARN]` when False. Also flags exact `==` pin mismatches.
- `setup.bat`: steps 4a-4e replaced by `deps.py install`; final check is now `deps.py check` +
  `tools/smoke_imports.py`; notes for Claude Code / git. The installer runs setup.bat, so it inherits all this;
  `installer/installer.iss` finish page also mentions a missing Claude Code.
- `tools/framer/app.py`: dependency code now shells out to `deps.py` (never stale after a pull);
  `/api/update` and `/api/deps/check` also return `system_tools`. `preflight.system_tools()` added.
- `tools/framer/index.html`: after Check for updates, missing packages/programs are listed, install is
  offered, and "Restart now" appears after a successful install.
- `tools/check_requirements.py` + `.githooks/pre-commit`: fail a commit if any import is uncovered.
  `git config core.hooksPath .githooks` was run in this clone.
- `tools/framer/tray_launcher.py`, `start.bat`: startup failures are written to `app_log.txt` (or
  `%TEMP%\recapper_startup_error.txt`) and shown in a Windows dialog with cause, fix and log path.
- `tools/smoke_imports.py`: imports the app, every stage, and the lazily loaded TTS engines.
- `CLAUDE.md`: short "Dependencies" section.

## Run next
```
git config core.hooksPath .githooks          # once per clone (already done here)
.venv\Scripts\python deps.py check           # your venv: google-genai is 2.8.0 but pinned ==2.25.0
.venv\Scripts\python deps.py install         # fixes it, torch stays CUDA
.venv\Scripts\python tools\smoke_imports.py
```
Your `.venv` also has a broken leftover `~orch` folder in site-packages (pip warns about it); deleting
`.venv\Lib\site-packages\~orch*` is safe.
