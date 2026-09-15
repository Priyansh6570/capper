# CLAUDE.md - project context for Claude Code

You are helping build a **manga/manhwa recap video pipeline**. Read this before
editing. The architecture is deliberate; keep to it.

## Architecture rule (do not break this)

The pipeline is **stages connected only by files + `manifest.json`**. Never make
one stage call another, never pass data in memory between stages, never merge
stages into one script. Each stage:

1. Takes a `Manifest` (see `manifest.py`), reads its inputs from disk.
2. Does its work, writing outputs to `work/<chapter>/<stage-folder>/`.
3. Fills its owned manifest fields and sets `m.stage` to its marker.
4. Returns the manifest. The orchestrator saves it - **stages never call save()**.

Stage signature is always: `def run(m: Manifest) -> Manifest`.

This isolation is the whole point: it makes each stage independently runnable
and lets us swap a stub for a real model without touching anything else.

## The manifest (the contract)

`manifest.py` defines `Manifest -> Page -> Panel`. Field ownership:

- s1 fills `Page.image`
- s2 fills `Page.panels` (bbox, order, crop, speaker, dialogue)
- s3 fills `Panel.context`, `Panel.emotion`
- s4 fills `Page.clean_image`
- s5 fills `Panel.script_line`
- s6 fills `Panel.audio`, `Panel.duration_s`
- s7 reads everything and renders output

`STAGE_ORDER` in `manifest.py` and `STAGES` in `orchestrator.py` must stay in
sync. If you add a manifest field, keep `Manifest.load()` backward-compatible
(use `.get()` with defaults) so old manifests still load.

### Document type (`doc_type`)

`Manifest.doc_type` is `"manga"` (page-based) or `"webtoon"` (vertical strip).
It is decided **once per chapter** at creation: the orchestrator takes it from
`--type {manga,webtoon}`, else prompts interactively, then persists it in the
manifest (so it is never asked again on resume). `Manifest.load()` defaults it
to `"manga"` for old manifests.

Stages branch on it:
- **s1**: `"manga"` renders one `Page` per PDF page; `"webtoon"` renders all
  pages and vertically stitches them in order into a single continuous strip
  (`work/<ch>/pages/strip_001.png` + a ~1500px `strip_001_preview.png`),
  exposing one `Page` (index 1). Page breaks in webtoon exports are arbitrary
  slices, so they are glued back together.
- **s2**: a **local candidate cutter** for the stitched strip — runs on the
  laptop (CPU), no ML, no Kaggle, no network. It reads each `Page.image` strip,
  finds whitespace/gutter rows (low per-row pixel variance) and cuts there,
  deliberately OVER-cutting (better too many candidates than too few; a later
  selection stage trims). A min-height floor merges slivers upward; a max-height
  fallback splits long gutterless stretches into chunks. Each candidate crop is
  written to `work/<ch>/panels/panel_NNN.png` (top-to-bottom = reading order)
  with a `_debug_pNNN.png` overlay. OCR is best-effort/non-fatal (pytesseract →
  easyocr → paddleocr if installed, else skipped) and its text lands in
  `Panel.dialogue`. Cut aggressiveness lives in tunable constants at the top of
  the file. (The old Kaggle detection notebook + `_kaggle/` loader is retired.)

## Pipeline is STORY-FIRST (script before frames)

The pipeline writes the recap script from ALL the chapter's OCR FIRST, then
matches one character frame to each piece of script. Stage order is:
`init -> pages -> panels -> vision -> script -> match -> context -> clean ->
audio -> video` (see `manifest.STAGE_ORDER` / `orchestrator.STAGES`).

- **STAGE A = s5 (`script`)**: reads every candidate panel's OCR (full stage-2
  set, not a selection) + the series brief, makes ONE `llm.chat_json` call, and
  writes the whole hero-POV recap as ordered SEGMENTS to
  `work/<ch>/script/segments.json` (+ `script.txt`). Touches no panels.
- **STAGE B = s2b (`match`)**: reads `segments.json`, matches ONE eligible cropped
  frame (has_face/has_person, non-text-only) to each segment by its
  `refers_to_orders` + reading-order monotonicity (reuse allowed; text-only NEVER
  bound), and fills `Manifest.beats` - the ordered matched beats that
  `kept_panels()` returns. (This REPLACED the old frame-first selection.)

So `s5` runs before `s2b`, and `kept_panels()` returns `Manifest.beats`, not a
`selected` subset of candidates.

## Status: stages 1, 2, 5, 6, 7 are real; stages 3-4 are stubs

Each stub's module docstring names the exact model/library to replace it with
and where it runs. When implementing a stage, change ONLY that file (plus its
deps); do not touch the manifest schema or other stages unless asked.

| Stage | File | Status | Real implementation | Runs on |
|-------|------|--------|---------------------|---------|
| 1 | s1_pdf_to_pages.py | REAL | PyMuPDF | laptop |
| 2 | s2_detect_panels.py | REAL | local whitespace/gutter candidate cutter (over-cuts; later stage trims) + best-effort OCR | laptop (CPU) |
| 3 | s3_context.py | REAL | Groq Llama-4-Scout vision (context + emotion on beats) | API |
| 4 | s4_inpaint.py | stub | IOPaint + LaMa | Kaggle GPU |
| 5 | s5_script.py | REAL (STAGE A) | story-first script -> segments, via `llm.chat_json` (Groq/Ollama) | API/laptop |
| 2b | s2b_select.py | REAL (STAGE B) | frame matching to script segments (no model) | laptop |
| 6 | s6_tts.py | REAL | Chatterbox TTS (Turbo→full→CPU fallback); edge-tts `--draft` | laptop (RTX 3050, CPU fallback) |
| 7 | s7_assemble.py | REAL | MoviePy single-frame recap, h264_nvenc→libx264 | laptop (NVENC) |

Remaining stub is stage 4 (inpaint, Kaggle GPU). Everything else runs locally /
via API.

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
pip uninstall torch torchvision torchaudio -y
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
```

- **Laptop / this repo**: Python + `requirements.txt` (PyMuPDF, Pillow). Stages
  1, 2, 5, 3, 7 and the orchestrator run here. Target Windows; use `pathlib`,
  never hardcode path separators. Stage 2's OCR is optional/best-effort: install
  pytesseract (+ the Tesseract binary), easyocr, or paddleocr to fill dialogue;
  without one, cutting still runs and dialogue is left empty.
- **Kaggle (GPU stages 4, 6)**: these need heavy ML deps (torch, transformers,
  iopaint, a TTS package) that do NOT belong in this repo's requirements.
  Develop each GPU stage so it can run as a standalone Kaggle script over a
  `work/<chapter>/` folder synced up, then sync results back. Keep
  `run(m) -> m` identical so the orchestrator is unchanged.
- **APIs (stages 3, 5)**: read keys from environment variables
  (`GEMINI_API_KEY`, `GROQ_API_KEY`); never hardcode secrets. Respect free-tier
  rate limits (Gemini ~1,500/day) - batch and add simple retry/backoff.

### Swappable LLM backend (reasoning stages 2b + 5)

The text-reasoning stages (`s2b_select`, `s5_script`) both call `llm.chat_json`,
which picks a backend from `MANHWA_LLM` (default `"groq"`) so you can escape
Groq's free-tier rate limits by running locally:

- `MANHWA_LLM=groq` (default): Groq `llama-3.3-70b-versatile`, JSON mode, with
  the existing retry/backoff. Needs `GROQ_API_KEY`.
- `MANHWA_LLM=ollama`: a LOCAL Ollama model, no API key, no rate limits. To use
  it: install Ollama, run `ollama pull qwen2.5:3b`, then set `MANHWA_LLM=ollama`.
  Override the model with `MANHWA_OLLAMA_MODEL` and the host with
  `MANHWA_OLLAMA_HOST` (default `http://localhost:11434`). Ollama talks plain
  HTTP via `httpx` (already a dep) - no extra pip package.

Both backends return the same parsed-dict contract, so the stages don't change.

## Conventions

- Make every stage write inspectable files; do not optimize them away into memory.
- For long runs, the orchestrator's resume is the recovery mechanism - keep
  stages idempotent (safe to re-run over the same chapter).
- Use the EMOTIONS vocabulary in `s3_context.py` as the shared emotion set that
  stage 6's TTS maps to.
- Add a debug overlay (boxes + reading-order numbers drawn on the page) when you
  implement stage 2 - reading-order bugs are easiest to catch visually.

## Run / test

```bash
python make_fixture.py
python orchestrator.py
# inspect: work/ch_001/manifest.json, output/ch_001/storyboard.html
```

## Work log (do this after every task)

After completing any task in this project, write a summary of what you did to
`work_log.md` in the project root - what was changed, which files, and any
commands the user should run next. OVERWRITE the file each time (not append), so
it always reflects only the most recent task. Keep it concise.
