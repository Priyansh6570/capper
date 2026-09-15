# Chapter workflow — from raw chapter to finished recap video

The exact, repeatable steps for turning ONE chapter into a recap video, with the
exact commands. This is the **manual / framer-driven** path we actually use: the
recap script is written with Gemini, frames are hand-picked in the Framer tool,
and only the audio + video stages of the pipeline run at the end.

Pipeline order recap (story-first): `download → split (if >127pg) → Gemini script
→ framer box → export → audio → video`.

Use a short `<chapter>` id (folder-safe, e.g. `01`, `ch_007`) and keep it the same
across every step below — everything lands under `work/<chapter>/`.

---

## 0. One-time setup
```bash
python -m venv .venv && .venv\Scripts\activate     # Windows (PowerShell: .venv\Scripts\Activate.ps1)
pip install -r requirements.txt
pip install flask                                  # for the Framer tool
```
- Optional voice cloning: drop a clean reference clip at `assets/voice_ref.wav`.
- Optional music bed: drop a track in `assets/music/` (first file is used, ducked
  under narration).
- `torch` MUST be the CUDA build — verify any time after a pip install:
  ```bash
  python -c "import torch; print(torch.cuda.is_available())"   # must print True
  ```

---

## 1. Download
Get the raw chapter as a **PDF** (the Framer stitches every page into one tall
webtoon strip). Save it somewhere you can point at, e.g.
`inputs/<chapter>.pdf`.

## 2. Split if > 127 pages
Gemini / Google AI Studio accepts at most **127 pages per uploaded PDF**. If the
chapter PDF is longer, split it into parts of ≤127 pages so each part can be
uploaded for the script step. (The Framer can still stitch the WHOLE original PDF
later — the split is only to get the script out of Gemini.)

Check the page count and split with PyMuPDF (already installed):
```bash
# page count
python -c "import fitz; print(fitz.open(r'inputs/<chapter>.pdf').page_count)"

# split into 120-page parts -> <chapter>_part1.pdf, _part2.pdf, ...
python -c "import fitz,math; src=fitz.open(r'inputs/<chapter>.pdf'); STEP=120; \
[ (lambda d: (d.insert_pdf(src, from_page=i, to_page=min(i+STEP,src.page_count)-1), \
   d.save(rf'inputs/<chapter>_part{i//STEP+1}.pdf')))(fitz.open()) \
 for i in range(0, src.page_count, STEP) ]"
```
If it's ≤127 pages, skip this step.

## 3. Gemini script
Upload the chapter PDF (or each split part, in order) to **Google AI Studio
(Gemini 2.5)** and prompt it for a hero-POV recap **narration**, ONE line per
beat. Paste the result into a plain `.txt` file, one narration line per line of
text, in reading order (this is what the Framer's "Load script" consumes). If you
split the PDF, concatenate the parts' narration into a single ordered `.txt`.

Keep it to clean narration only — no panel numbers or notes — because each line
becomes one spoken clip and one matched frame.

## 4. Framer — box the frames
Launch the tool and open it in a browser:
```bash
python tools/framer/app.py        # prints http://127.0.0.1:5005/
```
In the UI (details in `tools/framer/README.md`):
1. **Stitch PDF** — give the path to the ORIGINAL chapter PDF (not the split
   parts) and type the `<chapter>` id. It builds `work/<chapter>/pages/strip_001.png`.
2. **Load script** — paste the narration or point at the `.txt` from step 3.
3. **Box frames** — for each line, drag one or more boxes on the strip to bind
   frames. Per line set the **play mode**:
   - **sequence** — the line's frames play one after another, splitting the line's
     audio evenly between them.
   - **together** — the line's frames show at once (side-by-side / grid) for the
     whole line.
   Per frame you can **flip** and draw **blur** regions (to hide text); both are
   baked into the exported PNG.
4. **Save project** often (writes `work/<chapter>/framer/project.json`; autosaves
   to `recovery.json`).

## 5. Export
In the Framer, click **Export**. It writes, under `work/<chapter>/`:
- `frames/frame_<NNN><letter>.png` — each frame, already flipped/blurred.
- `framer/mapping.json` — the authoritative per-line plan (text, duration, mode,
  frames). Stage 7 reads this to honour `sequence` / `together`.
- `manifest.json` — a pipeline manifest with `beats` filled and `stage="clean"`,
  so the orchestrator runs ONLY audio + video next.

## 6. Audio (stage 6 — TTS)
Generate one narration clip per line with Chatterbox TTS:
```bash
# fast, flat affect (Chatterbox-Turbo, default):
python orchestrator.py --chapter <chapter> --from audio

# emotional (slower; full Chatterbox honours per-emotion exaggeration/cfg_weight):
set MANHWA_TTS_EMOTION=1 && python orchestrator.py --chapter <chapter> --from audio   # cmd
$env:MANHWA_TTS_EMOTION=1;  python orchestrator.py --chapter <chapter> --from audio   # PowerShell
```
- The run prints which model is active and whether emotion is applied.
- Just iterating on timing/video? Use the instant edge-tts draft instead:
  `set MANHWA_TTS_DRAFT=1` (no GPU).
- Clips land in `work/<chapter>/audio/`; real durations drive the video timing.

`--from audio` runs audio THEN video in one go, so step 7 is already covered. Run
step 7 on its own only when re-rendering the video without re-voicing.

## 7. Video (stage 7 — assemble)
Render the final recap (only needed separately if audio already exists):
```bash
python orchestrator.py --chapter <chapter> --from video
```
Stage 7 reads `framer/mapping.json` and, per line:
- **sequence**: splits the line's audio evenly across its frames (back-to-back
  sub-stills under the one narration clip);
- **together**: lays the frames out in a grid for the whole line;
- single-frame lines render as before.
It logs each line's frame count + mode. Consecutive lines are separated by
`LINE_GAP` (~0.4s) of silence so narration never jump-cuts; a music bed is mixed
in and ducked under the voice.

**Output:** `output/<chapter>/recap.mp4` (+ `video_plan.json`).

---

## Quick command summary
```bash
# 0. setup (once)
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt && pip install flask

# 2. (only if >127 pages) check + split
python -c "import fitz; print(fitz.open(r'inputs/<chapter>.pdf').page_count)"

# 4-5. box frames + export
python tools/framer/app.py            # http://127.0.0.1:5005/  -> Export in the UI

# 6-7. audio + video (emotional voice)
$env:MANHWA_TTS_EMOTION=1; python orchestrator.py --chapter <chapter> --from audio

# re-render video only
python orchestrator.py --chapter <chapter> --from video
```

## Tunables worth knowing
- `stages/s7_assemble.py`: `LINE_GAP` (silence between lines), `CROSSFADE`,
  `MIN_BEAT`, `MUSIC_VOL`, `NARRATION_VOL`.
- `stages/s6_tts.py`: `_EMOTION` map (per-emotion exaggeration/cfg_weight),
  `MANHWA_TTS_EMOTION`, `MANHWA_TTS_DRAFT`, `TTS_VOICE` (in `config.py`).
- LLM backend for the in-pipeline reasoning stages: `MANHWA_LLM` (`groq` default /
  `ollama`). Not used by the manual Gemini path above.
