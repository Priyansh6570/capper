# Framer — manual frame selection

Browser tool to hand-bind each narration line to a frame you draw on the
chapter strip, then export a mapping the existing pipeline turns into
audio + video.

## Run
```bash
pip install flask          # one-time (PyMuPDF + Pillow come from requirements.txt)
python tools/framer/app.py # prints http://127.0.0.1:5005/
```

## Use
0. **Download** (optional, top panel) — paste a **webtoon.com chapter URL** and the
   **chapter number**, click **Download PDF**. The backend runs
   [`webtoon-downloader`](https://pypi.org/project/webtoon-downloader/)
   (`--save-as pdf`) into `work/<chapter>/download/` while a spinner shows. When it
   finishes:
   - if the PDF has **>127 pages** (Gemini's per-upload page cap) it is split into
     ≤127-page parts at whole-page boundaries under `work/<chapter>/download/split/`,
     and **every part's path is printed** so you can upload them to Gemini yourself;
   - if ≤127 pages, the single PDF path is shown.

   The status panel lists each step (command, page count, split result). The
   downloaded **full** PDF path is dropped into the Stitch-PDF field automatically —
   just click **Stitch PDF** next. (Requires `pip install webtoon-downloader`. We do
   NOT touch Gemini; you upload the files.)
1. **Stitch PDF** — enter a PDF path (or choose a file) and optionally a chapter
   id. The strip is built with the SAME code as `stages/s1_pdf_to_pages.py`
   (every page rendered + vertically concatenated). The view is a downscaled
   preview (1000px wide) sliced into vertical **tiles** so a 600k-px-tall strip
   stays within browser limits. All boxes are stored in TRUE full-res strip px.
   - **Zoom**: the slider / `−` `+` buttons / **Ctrl(or ⌘)+mouse-wheel** (zooms
     toward the cursor). Range **5%–400%** — zoom out to 5% to see the whole strip
     for navigation, in to 400% to place boxes precisely. **Fit width** / **100%**
     buttons; current level shown.
   - **Scroll**: plain mouse-wheel scrolls vertically; a horizontal scrollbar
     appears when zoomed past the panel width.
   - Coordinates stay correct at any zoom/scroll: on-screen → TRUE px divides by
     `preview_scale × zoom` using live element geometry (which already folds in
     scroll), so a box drawn over a face at 2× exports that exact face at full res.
2. **Load script** — paste narration (one segment per line) or give a `.txt`
   path. Click a line to make it active.
3. **Add frames to a line** — a line can hold **multiple** frames. Drag a box on
   the strip to ADD a frame to the active line (each appears as a card under the
   line, in order). Per frame: **flip** toggle, **✕** remove, **◀ ▶** reorder
   within the line, and click a frame card to SELECT it (then **Blur mode** + drag
   draws blur regions on that selected frame). Reorder lines via the `⠿` grip.
   - **Blur strength / style**: blur is strong by default and scales with the
     region size (set by `BLUR_STRENGTH` in `app.py`), so big speech bubbles are
     destroyed too. The **Blur:** selector picks the style for new regions —
     **Mosaic** (pixelate; the most reliable text-killer, default) or **Heavy
     gaussian**. The style is stored per region and applied identically in the live
     preview and the exported frame (WYSIWYG).
   - **play mode** (per line): **sequence** = the frames play one after another,
     splitting the line's audio duration evenly between them; **together** = the
     frames are shown at once (side-by-side / grid) for the whole line duration.
### Workflow helpers
- **Insert / split / merge lines**: hover between any two lines (or above the
  first) for a **+ insert** bar that drops a blank line there; **split** a line in
  two at the text cursor (the first part keeps the frames); **merge ↑** folds a
  line into the one above (texts joined, both lines' frames kept in order). All are
  undoable.
- **Keyboard nav**: **↑/↓** or **j/k** move the active line (the strip scrolls to
  that line's first frame); **Enter** focuses its text; **Esc** leaves a field.
  **Ctrl+Z / Ctrl+Shift+Z** undo / redo.
- **Save / Load project** (don't lose work): **💾 Save project** writes the full
  working state (lines, per-frame true-px boxes + flip + blur, per-line mode +
  duration, strip/PDF reference) to `work/<chapter>/framer/project.json`. Type a
  chapter id and hit **📂 Load project** to resume a chapter across sessions —
  the strip is restored from the saved tiles, no re-stitch. Edits **auto-save**
  (debounced + every 30s) to `framer/recovery.json`; if a newer recovery exists
  when you open a chapter, a **Restore** toast offers it back.
- **Light / dark theme** toggle (top-right) and a **draggable split** between the
  strip and the script panel; both are remembered for the session (held
  server-side, not in browser storage).
- **Loading indicators**: a spinner + "Stitching PDF…" / "Building preview…"
  shows while a tall strip is built so the UI never looks frozen.

4. **Export** — writes, under `work/<chapter>/`:
   - `frames/frame_<NNN><letter>.png` — each frame cropped from the full-res strip,
     flipped + blurred. The letter (`a`,`b`,…) preserves order WITHIN a line.
   - `framer/mapping.json` — the authoritative plan (see schema below).
   - `manifest.json` — a pipeline manifest, `beats` set, `stage="clean"`.

### `mapping.json` schema
```json
{
  "chapter_id": "ch_001",
  "lines": [
    { "order": 1, "text": "...", "duration_s": 6.0, "mode": "sequence",
      "frames": [
        { "file": "frame_001a.png", "image": "<abs path>", "flip": false,
          "blur_regions": [[x,y,w,h]], "bbox": [x,y,w,h] },
        { "file": "frame_001b.png", "image": "<abs path>", "flip": true,
          "blur_regions": [], "bbox": [x,y,w,h] }
      ] }
  ]
}
```

## Then make the video
5. **Generate (in-tool)** — below Export, **🔊 Generate Audio + Video** runs the
   pipeline's audio (s6 TTS) then video (s7 assemble) for this chapter as a
   background job and **streams progress live** (a progress bar + scrolling log)
   over Server-Sent Events, so you never leave the page while the TTS model loads
   and each line is synthesized (minutes). **🎬 Re-render Video** re-runs only s7
   (fast) from the existing audio; **■ Stop** kills the running job. On success the
   finished `output/<chapter>/recap.mp4` plays inline with a download link.

Or run the same stages from the terminal:
```bash
python orchestrator.py --chapter <chapter> --from audio
```
This runs ONLY the audio (s6 TTS) and video (s7 assemble) stages over your
hand-picked frames; everything before is skipped.

The exported **manifest has ONE beat per LINE** (so s6 makes one narration clip
per line — not one per frame). Each beat's visible image is the line's **first**
frame, so the **current, unmodified s7 already renders a valid baseline video**
(first frame per line). To honour `sequence` / `together`, s7 must read
`mapping.json` — see below.

## What s7 (video) must change to honour `mode`
s7 today does, per beat: precompose ONE still and hold it for the beat's audio
length. To support multi-frame lines, change `s7_assemble.run` to, per beat,
look up that line in `work/<ch>/framer/mapping.json` by `order` and:

- **`together`**: precompose ONE still from the line's frames laid out in a grid
  (2 → side by side; 3–4 → 2×2; etc.), then proceed exactly as now (hold for the
  audio length). Only `_compose_still` needs a grid variant; timeline unchanged.
- **`sequence`**: split the beat's on-screen time (`max(audio_len, MIN_BEAT)`)
  EVENLY across the line's N frames — emit N sub-stills of `dur/N` each, all under
  the SINGLE narration clip for that line (delay the audio onto the first
  sub-still's start only; the others are silent holds). In `_plan`, expand one
  beat into N timeline items sharing the audio; in `_build_filtergraph`, the
  narration `adelay` targets the first sub-still's start.

s6 needs **no change** (it already voices one clip per beat = per line). The
frame files and per-frame flip/blur are already baked by the framer, so s7 only
arranges them — it never re-crops. If s7 is left unmodified, lines still render
as their first frame (a clean fallback), and `mapping.json` carries everything
needed when you do update it.

Note: s6 (TTS) overwrites each beat's `duration_s` with the real audio length, so
the per-line duration set in the UI is a hint (it governs timing directly only
for lines with no audio).
