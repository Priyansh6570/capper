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
(`--from audio`, `--from video`, `--to audio`). The Shorts add-on (below) runs after the video.

## Stages

| Stage | File | Implementation | Runs on |
|-------|------|----------------|---------|
| 1 | s1_pdf_to_pages.py | PyMuPDF; the editor calls it directly to stitch | laptop |
| 6 | s6_tts.py | Chatterbox TTS (Turbo, GPU with CPU fallback) or Kokoro; engine chosen per project | laptop (RTX 3050, CPU fallback) |
| 7 | s7_assemble.py | ffmpeg: timed stills joined with xfade, libx264 | laptop |
| 8 | s8_shorts.py | add-on, outside `STAGE_ORDER`: vertical shorts rendered with s7's renderer | laptop |

## Environments

### torch must stay the CUDA build (+cu128)

This project's torch must be the CUDA build (`+cu128`). Several packages
(easyocr, torchvision, chatterbox-tts; torchvision is pinned `+cu128` in `requirements-gpu.txt`) try to pull a CPU or mismatched torch on
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
`framer/mapping.json` (`load_mapping`) - both stages' own
media-settings loader returns `{}` on any absence/failure, so a chapter
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

Pass 1 has three **quality modes** (`quality.py`; `settings.claude.quality`, a per-chapter override in `chapter.json` as `quality`, and the
mode a script was last made with as `script_mode`): **Full** sends every panel as a 1024px image (the original behaviour), **Balanced** sends
`settings.claude.balanced_max_side` px images plus the text read from each panel, **Lite** sends only that text (with each bubble's place in the
panel) and no images, and its prompts say Claude has only dialogue and captions. Pass 2 and every style rule are the same in all modes. The text
comes from `ocr.py` (EasyOCR on torch: GPU when torch sees one, CPU otherwise or when the GPU runs out of memory; models in `models/easyocr`,
fetched by `deps.py install` or on first use). `panel_inputs.PanelCache` keeps the OCR text (`ocr.json`) and the downscaled copies
(`send_<side>/`) in the chapter's `claude/` folder, so another mode or a re-run reuses them; panel files are only read. A saved run in another
mode is discarded and the chapter starts over, without touching other chapters. No image sent is longer than `quality.MAX_IMAGE_SIDE` (2576px,
beyond which Claude would shrink it); panels are already cut at gutters by `panel_split`. Each finished run appends its tokens and mode to
`projects/<slug>/script_usage.jsonl` (`script_usage.py`).

The framer's **Shorts pipeline** (`shorts.py`, `routes/shorts.py`, `stages/s8_shorts.py`, `audio_sample.py`) is an add-on for chapters
that already have a script and a video, in a batch of its own (`batch.SHORTS_STAGES`, `shorts_batch.json`; each step
runs one chapter at a time, like the main batch). It works on a **range of chapters**, not per chapter: the batch is created
with only the range's first eligible chapter and `meta["find"]` = `{chapters, max_shorts}`. That chapter's `shorts_find`
step is one Claude job over the whole range (`shorts.find_range`: the scripts of all chapters in one call, or a few when
long; every moment is scored 1-10 for emotion / epic / mystery, only >= `MIN_SCORE` count, optionally cut to the best
`max_shorts`). It writes `work/<ch>/shorts/plan.json` only for chapters with a strong moment (saving a plan deletes that
chapter's old shorts; other chapters are left alone), records `meta["found"]` and `Batch.join`s the picked chapters, which
then run `shorts_render`: `python -m stages.s8_shorts`, which reads that plan and the chapter's
existing frames (each short's plan entry also carries a `script`, written right after the find by one more Claude call per picked
moment: a tight recap of the scene ending on a teaser made for it, each line naming the chapter lines whose panels to show;
s8 voices it with `s6_tts.voice` into `work/<ch>/shorts/audio`, never touching the chapter's narration; a plan without a
script replays the chapter's lines and audio) and writes `output/<ch>/shorts/short_NN.mp4` + `shorts.json`. It is not in
`manifest.STAGE_ORDER` or the orchestrator and leaves `m.stage` alone, so a normal run never makes shorts. It calls
s7's `prepare_render` / `execute_render` (not `run`) with `s7.PORTRAIT`, so the media settings apply as in the main
video; a project's `settings.shorts` (`layout` frame|cover, `own_look`, `watermark`, `background`, `own_audio`, `music`,
`audio`) is resolved into the snapshot's `shorts` key (`layout`, `look` that overrides the main watermark and background,
`sound` that overrides the main music and volumes); `cover` is passed to `prepare_render`; the stages stay connected only
through those files. The settings page plays a **sample** of the mix (`/api/projects/<slug>/preview/audio.wav`,
`audio_sample.py`): a few narration lines over the music built with `s7._audio_filters`, the same audio graph a render uses.
Shorts are paused by default: `settings.shorts.auto_make` is off (project schema 4 turned it off in existing projects), so they are made only
by the Shorts button or `/shorts`, and nothing in the long-video pipeline waits on them.

The framer's **publishing** (`tools/framer/publishing/`, `routes/publishing.py`, `services/remote.py`) uploads to YouTube and is
controlled from a Telegram bot. It touches no pipeline stage or manifest field: it reads finished merges and rendered shorts from
disk, and a project's channel, upload defaults and thumbnail are `settings.youtube`. Its state (OAuth tokens, bot token, queue,
quota) is kept in the git-ignored `publishing/` folder in the repo root, never in a project/Drive folder. Full videos wait for
approval and always upload private; shorts upload public on the channel's schedule (private when YouTube locks an unaudited API
project, switched to public once the lock lifts). Only `requests` is used for Google and Telegram, no Google client library.
Telegram is a planner as well as a remote. `/run` and `/shorts` start work; nothing is merged by itself. When a main batch
finishes (`batching._pipeline_finished`) shorts are made for the chapters it rendered if `settings.shorts.auto_make` is on, and a
**merge card** proposes the project's next finished, unmerged chapters up to `settings.plan.min_minutes`: Merge runs
`services/autopublish.py` (merge, intro/outro, the upload approval card), Change range asks for chapters and answers with their
length, Later drops it. A chapter that fails sends a **failure card** (`BatchEnv.failed`): `Batch.retry` runs it again from the failed
step or from the start, `Batch.skip` cancels it; the website has the same buttons (`/batch/chapters/<n>/retry|skip`, also under
`/shorts/batch/`). `services/planning.py` computes a project's plan from `settings.plan` (upload weekdays, minimum minutes): average
chapter length, chapters per upload, and the buffer (queued + merged + finished-unmerged video, in whole uploads, as days of the
schedule); `/plan` shows it. `services/advisor.py` suggests the neediest project (card with Run / Later) while no pipeline runs and
both Claude usage windows are under the project's batch limits. Card buttons act on `publishing/cards.json`
(`services/cards.py`), so they survive a restart. None of this touches the manifest, the orchestrator or any stage.

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
commands the user should run next. APPEND a new entry at the end each time; never
replace or rewrite earlier entries. Keep it concise.
