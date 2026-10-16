# CLAUDE.md - project context for Claude Code

You are helping build a **manga/manhwa recap video app**. Read this before
editing. The architecture is deliberate; keep to it.

## Architecture rule (do not break this)

The stages that turn an exported chapter into a video are **connected only by files +
`manifest.json`**. Never make one stage call another, never pass data in memory between
stages, never merge stages into one script. Each stage:

1. Takes a `Manifest` (see `manifest.py`), reads its inputs from disk.
2. Does its work, writing outputs to `work/<chapter>/<stage-folder>/`.
3. Fills its owned manifest fields and sets `m.stage` to its marker.
4. Returns the manifest. The orchestrator saves it - **stages never call save()**.

Stage signature is always: `def run(m: Manifest) -> Manifest`.

This isolation is the whole point: it makes each stage independently runnable
and lets us swap one implementation for another without touching anything else.

## The manifest (the contract)

`manifest.py` defines `Manifest -> Page -> Panel`. Field ownership:

- s1 fills `Page.image` (the stitched strip)
- the editor's export fills `Page.panels` and `Manifest.beats` (crop, script_line, emotion = the
  line's pace, duration_s) and leaves the manifest at the `frames` marker
- s6 fills `Panel.audio`, `Panel.duration_s`
- s7 reads the beats and renders the video

`STAGE_ORDER` in `manifest.py` and `STAGES` in `orchestrator.py` must stay in sync. If you add a
manifest field, keep `Manifest.load()` backward-compatible so old manifests still load: unknown
fields are ignored and the markers of the removed stages are mapped (`LEGACY_STAGES`).

### Document type (`doc_type`)

`Manifest.doc_type` is `"manga"` (page-based) or `"webtoon"` (vertical strip). The editor creates
every chapter as `"webtoon"`; `Manifest.load()` defaults it to `"manga"` for old manifests. s1
renders one `Page` per PDF page for `"manga"`, and for `"webtoon"` renders all pages and
vertically stitches them in order into a single continuous strip (`work/<ch>/pages/strip_001.png`
+ a ~1500px `strip_001_preview.png`), exposing one `Page` (index 1). Page breaks in webtoon
exports are arbitrary slices, so they are glued back together.

## How a chapter flows

download (a site adapter makes the PDF) -> stitch (s1) -> script (the Claude engine) -> export
(frames cut and the manifest written at `frames`) -> audio (s6) -> video (s7). The app runs the
first four itself; it starts `orchestrator.py` as a subprocess for audio and video
(`--from audio`, `--from video`, `--to audio`).

## Stages

| Stage | File | Implementation | Runs on |
|-------|------|----------------|---------|
| 1 | s1_pdf_to_pages.py | PyMuPDF; the editor calls it directly to stitch | laptop |
| 6 | s6_tts.py | Chatterbox TTS (Turbo, GPU with CPU fallback) or Kokoro; engine chosen per project | laptop (RTX 3050, CPU fallback) |
| 7 | s7_assemble.py | ffmpeg: timed stills joined with xfade, libx264 | laptop |

## Environments

### torch must stay the CUDA build (+cu128)

This project's torch must be the CUDA build (`+cu128`). Several packages
(easyocr, torchvision, chatterbox-tts) try to pull a CPU or mismatched torch on
install. After ANY pip install, re-check:

```bash
python -c "import torch; print(torch.cuda.is_available())"
```

It must print `True`. If it prints `False`, reinstall:

```bash
pip install --force-reinstall -r requirements-gpu.txt
```

torch/torchaudio are pinned (`+cu128`) in `requirements-gpu.txt`, NOT in
`requirements.txt` - PyPI's Windows wheels are CPU-only, so listing them in
`requirements.txt` made every `pip install -r` swap the CUDA build out.
`setup.bat` installs `requirements-gpu.txt` first. `gpu.py` is the shared
GPU/NVENC probe (stage 6, stage 7, setup, the framer's GPU status card).

### Dependencies: three requirement files, one installer

- `requirements-gpu.txt`: the CUDA torch/torchaudio. `requirements.txt`: everything else the
  code imports (including opencv-python-headless). `requirements-nodeps.txt`:
  packages installed with `--no-deps` because their metadata conflicts with the rest
  (chatterbox-tts, kokoro, misaki); their real runtime deps live in `requirements.txt`.
- `deps.py` is the only installer/checker (`python deps.py install|check`); `setup.bat`, the
  installer (which runs setup.bat) and the framer's in-app updater all call it. It freezes the
  installed torch with a constraints file, so a dependency can never swap the CUDA build, and
  verifies `torch.cuda.is_available()` afterwards.
- A new third-party import must be added to a requirements file in the same commit:
  `tools/check_requirements.py` (the `.githooks/pre-commit` hook; enable once per clone with
  `git config core.hooksPath .githooks`) fails otherwise. `tools/smoke_imports.py` imports the
  app, every stage and the engine libraries; setup.bat runs it as its final check.
- Startup failures (`tools/framer/tray_launcher.py`, `start.bat`) are logged to `app_log.txt`
  and shown in a Windows dialog, because the app runs windowless.
- The Hugging Face model cache can live on another drive: `hf_home` in `app_settings.json` (a
  machine setting, git-ignored) is handed to huggingface_hub as `HF_HOME` by `config.py` when it
  is imported, unless `HF_HOME` is already set. `setup.bat` picks it per PC with
  `tools/choose_model_cache.py` (a second fixed drive with room, else the Windows default), and
  the Projects screen can change it.

- **Laptop / this repo**: Python + `requirements.txt`. The stitch, narration and video stages and
  the orchestrator run here. Target Windows; use `pathlib`, never hardcode path separators.

## The framer's project system (`tools/framer/`)

`tools/framer/app.py` is a Flask tool (self-contained under `tools/framer/`,
see its README) that wraps the pipeline in a **project system**: one project
per manhwa, each with its own chapter list + status and its own folder,
`projects/<slug>/chapters/<n>/{work,output}/`.

This is a **path redirect implemented entirely in `config.py`**, not a change
to the pipeline architecture: `chapter_work_dir()`/`chapter_output_dir()`
resolve to a project's chapter folder whenever `config.chapter_scope(work,
output)` is active (a context manager backed by a `ContextVar`) or the
`MANHWA_WORK_ROOT`/`MANHWA_OUTPUT_ROOT` env vars are set (read once at
import - this is how the orchestrator subprocess the framer spawns for
"Generate" picks up the redirect). Every stage still just calls
`chapter_work_dir(m.chapter_id)` exactly as before; **no stage file is
touched** by this. With no active scope and no env vars, both functions
behave exactly as they did before the project system existed, so CLI runs
over the legacy global `work/<chapter>/` tree are unaffected.

A project also carries **per-project media settings** (background music, a
Chatterbox voice reference, a tiled watermark, a custom video background) in
`project.json`, resolved with absolute paths into
`<chapter work>/media_settings.json` by `tools/framer/projects.py`. Stage 6
(`s6_tts.py`, the `voice` field) and stage 7 (`s7_assemble.py`, the rest) each
read that file the same defensive way s7 already reads the framer's
`framer/mapping.json` (`_load_mapping`) - both stages' own
`_load_media_settings()` returns `{}` on any absence/failure, so a chapter
with no settings file runs exactly as before this feature existed. Do not
move these fields into the `Manifest` schema - they are project-level config,
not stage output, and this file-based snapshot keeps s6/s7 independently
runnable/testable per the architecture rule at the top of this document.

`orchestrator.py` also takes an optional `--to <stage>` (stop after that
stage) alongside `--from`, so `--from audio --to audio` regenerates ONLY the
narration - this is what the framer's **Re-render voice** button uses
(`/generate_stream?what=voice`) after a voice-sample or speed change, without
re-exporting the frames or touching an existing render.

The framer's **merge** feature (concatenating several complete chapters'
`recap.mp4` into one, `/api/projects/<slug>/merge_stream`) is entirely
self-contained in `tools/framer/app.py` + `projects.py`: it reads already-
rendered output with `ffprobe`/`ffmpeg` and writes to
`projects/<slug>/merged/`. It required **no** change to the manifest,
orchestrator, or any pipeline stage - keep it that way; it has nothing to do
with a single chapter's render.

The framer's **branding** (`branding.py`) joins `recapper_intro.mp4` /
`recapper_outro.mp4` (project root, or a per-project custom upload) onto MERGED
videos only, as a separate `<merge>_branded.mp4`, via the existing job queue
(`what="brand"`). A 1s black/silent gap separates each clip from the main video
and the main video's audio fades in over 2s. Chapter renders are never branded.
Only the short clips (and the main audio track) are re-encoded; the main picture
is stream-copied and the clips are cached under `cache/branding/`.
The framer's **media library** (`library.py`) lists/deletes only the regenerable
file types in `library.KINDS`; script, mapping, editor state, manifest, settings
and custom uploads are not reachable from it. Chapter status is re-derived from
disk (`derive_status`), so a deleted recap.mp4 drops the chapter to "needs
render", and `/api/generate` refuses renders whose frames/audio were deleted.
Neither feature touches the manifest, orchestrator, or any pipeline stage.

The framer's **export/import** feature (`/api/projects/<slug>/export`,
`/api/projects/import`, `projects.export_archive`/`import_archive`) zips a
project's whole folder and restores it as a new project, rewriting the absolute
paths stored in chapter JSON files. Like merge, it needs no change to the
manifest, orchestrator, or any pipeline stage.

**Narration pacing** (`pacing.py`) is shared by both TTS engines in stage 6 (Chatterbox and Kokoro).
Each line has a `pace` (calm, tense, action, reveal, sad, comic; an editor field per line, written by the
Claude engine per block, else inferred
from the text), stored as the beat's `Panel.emotion`. A line is voiced sentence by sentence, the engine's
own silence is trimmed and capped, and `pacing.voice_line` lays the pauses and applies the pace's speed, so
the clip written to disk already contains every pause and stage 7 adds no gap between lines.

The framer's **Claude script engine** (`claude_engine.py`, `panel_split.py`) splits the strip into
panels, sends them as images to the local `claude` CLI in batches (pass 1) and writes
the narration from the resulting text-only breakdown (pass 2), then pre-fills the framer
with narration boxed to full panels, bubble blur boxes included. It touches no pipeline stage; TTS/render still read
the framer's export.

## Conventions

- Make every stage write inspectable files; do not optimize them away into memory.
- For long runs, the orchestrator's resume is the recovery mechanism - keep
  stages idempotent (safe to re-run over the same chapter).
- `pacing.py` holds the shared pace vocabulary (calm, tense, action, reveal, sad, comic) that
  stage 6's TTS maps to.

## Run / test

```bash
.venv\Scripts\python.exe -m unittest discover -s tests -t .   # the tests, against a throwaway projects folder
.venv\Scripts\python.exe tools/framer/app.py                   # the app, http://127.0.0.1:5005/
```

## Work log (do this after every task)

After completing any task in this project, write a summary of what you did to
`work_log.md` in the project root - what was changed, which files, and any
commands the user should run next. OVERWRITE the file each time (not append), so
it always reflects only the most recent task. Keep it concise.
