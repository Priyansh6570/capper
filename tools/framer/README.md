# Framer — projects, manual frame selection, per-project media

Browser tool built around **projects** (one per manhwa/manhua). Each project
tracks its own chapter list + status, downloads/boxes/renders chapters into
its own folder, and carries its own background music, tiled watermark, and
video background — all fed into the pipeline's video stage (s7) at render
time.

## Run
```bash
pip install flask requests beautifulsoup4   # one-time (PyMuPDF + Pillow come from requirements.txt)
python tools/framer/app.py                  # prints http://127.0.0.1:5005/
```

## Layout

The chapter editor is a header + **left sidebar** (Chapter Source / Stitch
Strip / Draw Tool / Export & Render, each its own card) + **strip workspace**
(zoom/fit/fullscreen toolbar, the strip itself, a collapsible Activity log
docked underneath) + **script panel** on the right — every control is the
same element wired to the same backend endpoint as before, just regrouped.

**Mobile / touch** (≤900px, or any touch-primary device up to 1100px): the
sidebar becomes a slide-over drawer (the header's icon button opens it), and
the strip/script panels become two full-height views switched by a bottom tab
bar. On the strip, **one finger draws** a frame/blur box exactly like a mouse
drag, and **two fingers pinch-zoom** (and pan) — both route through the same
true-pixel coordinate math as desktop. Precise frame-boxing is still easiest
with a mouse; treat phones/tablets as a review-and-manage surface first.

## Projects

- **Projects landing screen** (shown first) — **New Project**: a name + a
  webtoons.com URL. On create, the backend tries to **auto-fetch** the series'
  title and chapter count by scraping the series' `/list?title_no=` page
  (`webtoon_meta.py`, best-effort, `requests`+`bs4`, never blocks/raises). If
  the fetch fails, a manual **"enter chapter range 1..N"** input appears
  instead. Existing projects are listed below to reopen.
- **Storage**: `projects/<slug>/project.json` (name, url, chapter list +
  status, media settings) plus `projects/<slug>/assets/` (uploaded music/
  watermark/background) and `projects/<slug>/chapters/<n>/{work,output}/` —
  everything for a chapter (download, stitched strip, framer state, frames,
  audio, `recap.mp4`) lives under its own project+chapter folder. This is a
  redirect of `config.chapter_work_dir()`/`chapter_output_dir()` via
  `config.chapter_scope()` — the pipeline stages themselves are untouched.
- **Chapter status**: `not_started → downloaded → boxed → rendered`, plus a
  manual `complete` ("Mark complete" on the chapter row — nothing on disk can
  infer this, it's your own bookkeeping). Statuses also get re-derived from
  what's actually on disk each time you open a project, so an interrupted run
  never leaves a stale status.
- **Project screen**: the chapter list, each row showing its status and the
  right action (**Download** for a fresh chapter — pulls the project's URL +
  that chapter number, then opens the editor; **Open editor** once a chapter
  has a PDF).
- **Chapter editor** — the workspace described below, scoped to whichever
  project+chapter you opened. Download/stitch/box/export/generate all read
  and write under that chapter's own folder.

## Merging complete chapters into one video

Every chapter row marked **complete** gets a checkbox; check two or more and
hit **Merge selected videos** (below the chapter list) to concatenate their
`recap.mp4` files, in chapter order, into one video under
`projects/<slug>/merged/`. This is a **framer-only feature** — it just reads
already-rendered output, so nothing about `manifest.py`, `orchestrator.py`, or
any pipeline stage needed to change.

- Progress streams live the same way Generate does (SSE), and the finished
  merge plays inline with a download link, exactly like a single chapter's
  render. Past merges for the project are listed below and stay clickable to
  replay.
- **Safety check**: every input is probed with `ffprobe` first. If all of them
  are 1920x1080 with the same video *and* audio codec (the normal case — every
  chapter came out of the same s7 render), the merge is a fast stream copy
  (`ffmpeg -f concat -c copy`, no re-encode, effectively instant). If anything
  differs — a stray non-standard render, a different resolution, whatever —
  every clip is scaled + letterboxed to 1920x1080@24fps and its audio
  resampled before concatenating, so a mismatch can't corrupt or desync the
  result; it just costs a slower re-encode.
- A chapter is only mergeable when its status re-derives as `complete` *and*
  it actually has a `recap.mp4` on disk — both are re-checked at merge time,
  not just trusted from a stale page load.

## Project settings (music / voice / watermark / background)

Open **Settings** on a project screen. Saved into that project's
`settings` (in `project.json`) and applied at render time via a resolved
snapshot the framer writes to `<chapter work>/media_settings.json` (the same
pattern s7 already uses for `framer/mapping.json`) — stages 6 and 7 read it
and fall back to today's defaults when it's absent.

- **Music** — default (`assets/music/`, the pipeline's global track) or a
  project-specific upload. Read by stage 7.
- **Narration voice** — default (`assets/voice_ref.wav`) or a project-specific
  `.wav` reference clip for Chatterbox cloning. Read by stage 6 (s6_tts); goes
  through the exact same float32/mono/24kHz preprocessing as the default clip
  before Chatterbox sees it. The Settings panel shows which one is active and
  lets you play the uploaded sample back. After changing it, use **Re-render
  voice** (below) to regenerate narration without re-boxing frames.
- **Watermark** — text or a logo image, **tiled** across the whole frame with
  **opacity**, **size**, **spacing**, and **angle** sliders. A live preview
  (the tiled watermark over a sample frame — the project's own first exported
  frame if one exists, else a generated placeholder) updates ~250ms after you
  stop adjusting a control. Read by stage 7.
- **Background** — the default blurred-crop-of-itself, a project-specific
  image (cover-scaled, **not** blurred — it's art you chose on purpose — just
  dimmed by the **dim** slider), or a project-specific **video**, played
  **looped** full-screen behind the frames for the whole render (same **dim**
  slider, applied as an approximate brightness offset). Image mode is
  live-previewed as a composited still; video mode previews as an actual
  looping, dimmed `<video>` element (there's no static image to render for a
  moving background). Read by stage 7 - see `_load_background` (image) /
  `_resolve_video_background` (video) in `stages/s7_assemble.py`. A video
  background works by precomposing each still with a TRANSPARENT background
  instead of baking one in with PIL, then `overlay`-ing that onto the looped
  background video in the ffmpeg filtergraph - fundamentally different from
  the image path, which stays exactly as it always was.
- **Frame Animation** → **Alternating Ken Burns** — a checkbox, independent of
  the transition style above: when on, each still slowly zooms while it's on
  screen (odd position zooms IN, even position zooms OUT, alternating), via a
  per-still `zoompan` in the ffmpeg filtergraph. Off by default because it
  measurably slows rendering (roughly 1.5x on this project's own dev machine,
  GPU-encoded via NVENC - zoompan's per-output-frame crop math runs on the
  CPU regardless of which encoder does the final encode).

Uploading a file saves it immediately; sliders/text/radio choices save on a
short debounce. A settings change rewrites the snapshot for every chapter the
project already has a work folder for, and generate re-writes it once more
right before rendering, so a last-minute tweak still lands.

## The chapter editor
0. **Download** (optional, top panel) — paste a **webtoon.com chapter URL** and the
   **chapter number**, click **Download PDF**. The backend runs
   [`webtoon-downloader`](https://pypi.org/project/webtoon-downloader/)
   (`--save-as pdf`) into the chapter's `download/` folder while a spinner shows. When the
   download finishes, the full-res PDF path is dropped into the Stitch-PDF field
   automatically (untouched — this is what **Stitch PDF** / the framer always use),
   and the tool **always** goes on to prepare Gemini-ready parts, live-streaming
   progress in the panel below:
   - the PDF is split into **≤100-page parts** (`PART_PAGES` in `app.py`), each
     overlapping the previous part by **5 pages** (`PART_OVERLAP`) so Gemini keeps
     continuity across the seam — e.g. `part1_p1-100`, `part2_p96-195`, …;
   - every page in every part is **downscaled to ≤1600px** on its longest side
     (`GEMINI_MAX_DIM`) and **re-encoded as JPEG at quality 75** (`GEMINI_JPEG_QUALITY`)
     inside the part PDF, so text stays legible but the upload is much smaller;
   - the compressed parts land in **`gemini_parts/`** under the chapter's folder
     (cleared on each re-download). The result panel lists each part's page range and file
     size, with **Copy folder path** / per-part copy buttons and an **Open folder**
     button so you can hand them to Gemini.

   The status panel lists each step (command, page count, per-part compress
   progress). (Requires `.venv\Scripts\pip install webtoon-downloader` — into
   this app's own venv, not a global/system Python, so the app can find it. We
   do NOT touch Gemini; you upload the files.)
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
  **Ctrl+Z / Ctrl+Shift+Z** undo / redo. (These only fire while the chapter
  editor screen is open.)
- **Save / Load editor state** (don't lose work): **💾 Save** writes the full
  working state (lines, per-frame true-px boxes + flip + blur, per-line mode +
  duration, strip/PDF reference) to `<chapter>/framer/editor_state.json`. Hit
  **📂 Load** to resume a chapter across sessions — the strip is restored from
  the saved tiles, no re-stitch. Edits **auto-save** (debounced + every 30s,
  only while the editor is the visible screen) to `framer/recovery.json`; if a
  newer recovery exists when you open a chapter, a **Restore** toast offers it
  back.
- **Light / dark theme** toggle (top-right, on every screen) and a **draggable
  split** between the strip and the script panel — drag the divider from
  roughly 240px up to nearly the full window width; both the theme and the
  split width are remembered for the session (held server-side, not in
  browser storage). The script list (`#lines`) scrolls on its own within
  whatever height remains, with a stable-gutter scrollbar so it never jitters
  the layout as lines are added.
- **Loading indicators**: a spinner + "Stitching PDF…" / "Building preview…"
  shows while a tall strip is built so the UI never looks frozen.
- **Fullscreen strip view** (`Fullscreen` button in the zoom bar, or `Esc` to
  exit) — hides everything but the strip itself so you can scroll through it
  like reading a webtoon. It's read-only (drawing a frame/blur box is
  disabled while fullscreen) — exit to go back to boxing. Zoom/scroll are the
  same `#stage`/`#world` underneath, just given the whole window; nothing
  about the zoom math changes.
- **Script-only view** (`Script only` toggle above the narration textarea) —
  hides the strip and widens/centers the script column for a clean read or
  edit pass over the narration alone. Lines stay fully editable; toggling
  back restores the strip and whatever split width you had before.

4. **Export** — writes, under the chapter's folder:
   - `frames/frame_<NNN><letter>.png` — each frame cropped from the full-res strip,
     flipped + blurred. The letter (`a`,`b`,…) preserves order WITHIN a line.
   - `framer/mapping.json` — the authoritative plan (see schema below).
   - `manifest.json` — a pipeline manifest, `beats` set, `stage="clean"`.

   This also flips the chapter's status to `boxed`.

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
   and each line is synthesized (minutes). **Re-render voice** re-runs ONLY s6
   (`--from audio --to audio`) — for after changing the project's voice sample or
   speed, without re-selecting frames; it never touches `recap.mp4` or the
   chapter's status, so a stale render stays valid until you also run **🎬
   Re-render Video**, which re-runs only s7 (fast) from the new audio. **■ Stop**
   kills whichever job is running. On success of a video-producing run, the
   finished `recap.mp4` plays inline with a download link and the chapter's
   status flips to `rendered`.

Or run the same stages from the terminal (for a chapter NOT inside a project,
i.e. the legacy global `work/<chapter>/` layout):
```bash
python orchestrator.py --chapter <chapter> --from audio
python orchestrator.py --chapter <chapter> --from audio --to audio   # voice only
```
`--from audio` (no `--to`) runs the audio (s6 TTS) and video (s7 assemble)
stages over your hand-picked frames; everything before is skipped. Add
`--to audio` to stop right after narration - handy after swapping the voice
reference or its speed, without touching frame selection or the existing
video. For a project chapter, set `MANHWA_WORK_ROOT`/`MANHWA_OUTPUT_ROOT` to
that chapter's `work/`/`output/` folders first (see `config.chapter_scope` —
this is what `/generate_stream` does for you automatically).

The exported **manifest has ONE beat per LINE** (so s6 makes one narration clip
per line — not one per frame). Each beat's visible image is the line's **first**
frame; s7 reads `framer/mapping.json` to render `sequence`/`together` multi-frame
lines (see `stages/s7_assemble.py`'s `_plan`/`_line_frames`) and falls back to
one still per beat when no mapping exists (legacy manifests).

Note: s6 (TTS) overwrites each beat's `duration_s` with the real audio length, so
the per-line duration set in the UI is a hint (it governs timing directly only
for lines with no audio).

## What s6/s7 do with project media settings
Both stages read `<chapter work>/media_settings.json` (written by this tool,
see "Project settings" above) each run:

`stages/s6_tts.py`:
- **Voice** — `_load_media_settings` resolves the project's `voice` field; if
  set and the file exists, `run()` uses it as the Chatterbox reference clip,
  else falls back to `assets/voice_ref.wav`, else no cloning. Either way it
  goes through `_preprocess_ref` (float32/mono/24kHz) exactly as before.

`stages/s7_assemble.py`:
- **Music** — the project's file if set, else the existing `assets/music/` scan.
- **Watermark** — built ONCE per render as a single 1920x1080 RGBA layer
  (`_build_watermark_layer`) and alpha-composited onto every precomposed still
  in `_precompose` — so both the ffmpeg path AND the MoviePy fallback get it
  for free, at a cost of one composite per beat (not per output frame).
- **Background (image)** — `_load_background` cover-crops + dims (not blurs)
  the project's image once per render; `_compose_still`/`_compose_grid_still`
  use it instead of the default blurred self-cover when set.
- **Background (video)** — `_resolve_video_background` just resolves the
  file path (there's nothing to bake into a static PIL still for a moving
  video); `_precompose(..., transparent=True)` then leaves each still's
  background area transparent instead, and `_build_filtergraph` `overlay`s
  the whole transition-chained foreground track onto the looped, scaled,
  dimmed background video as its last step. Mutually exclusive with the
  image background - see `run()`.
- **Ken Burns** — `_zoompan_filter` builds one `zoompan` filter per still,
  inserted into its per-input chain in `_build_filtergraph` BEFORE the
  transition chain; alternates zoom direction by each item's position in the
  render (not by anything about the source image itself).

All fields are optional; a project with no settings runs exactly as before
this feature existed.
