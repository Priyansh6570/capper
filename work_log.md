# Work log

## Task: fix 5 framer editor UI/bug reports

All changes in `tools/framer/index.html` unless noted.

### 1. "Re-render video" button overflowing its container

`#genVoiceBtn`/`#genVidBtn` were two `class="small"` buttons side by side in a
`.row2` flex row inside the 300px sidebar. `.sidecard .row2 > input` had
`flex:1`, but the matching rule for `> button` never existed, so both buttons
sized to their own text+icon content and the row's total width exceeded the
sidebar, overflowing it (their `white-space:nowrap` meant the text couldn't
wrap to absorb the overflow either).

Fix: dropped the `.row2` wrapper for just these two buttons and gave each
`class="small wide"` (the same full-width pattern already used by
`#genAVBtn`/`#genCancelBtn` right next to them) so they stack vertically at
100% width - can't overflow at any sidebar width.

### 2. Zoom sticks to the left instead of staying centered

`#world` (the strip element) was a plain block div inside `#stage`
(`overflow:auto`) with no centering rule. When zoomed below "fit width" (the
strip narrower than the viewport), a block child with no auto margins sits
flush against the container's left edge - that's what "sticks to the left"
was. The JS zoom math (`setZoom()`) already correctly keeps the viewport-
center point fixed while zooming; the bug was purely CSS layout.

Fix: added `margin:0 auto` to `#world`. This centers it whenever it's
narrower than `#stage` and has zero effect (auto margins resolve to 0) once
it's wider, so normal edge-to-edge scrolling at high zoom is unchanged.
Verified `screenToTrue()` (used for all box-drawing coordinate math) still
maps correctly since it reads `world.getBoundingClientRect()` directly, which
already reflects the margin offset - no JS changes needed.

### 3. Unstyled "Browse" buttons + messy settings panel layout

The 4 asset pickers (music/voice/watermark logo/background image) were raw
`<input type=file>` elements rendering the browser's native "Choose File"
button, unlike the one other file picker in the app (`pdfPath`/`pdfBrowseBtn`)
which already used the hidden-input + styled-button pattern.

Fix:
- Hid all 5 file inputs (the 4 above + `pdfFile`, unified for consistency)
  behind a new reusable `.filehidden` class, each paired with a themed
  `class="small ghost"` "Browse…" button that triggers `.click()` on the
  hidden input - wired in the same block as the existing `pdfBrowseBtn`.
- Empty filename `.swatch` spans now show a dim "No file chosen" via
  `:empty::before` instead of looking blank/broken.
- Redesigned `.setgrid` from a `flex-wrap` row (uneven column heights, no
  visual separation) to a CSS grid
  (`repeat(auto-fit, minmax(260px,1fr))`), and gave each `.setcol` a
  `.sidecard`-style sub-panel treatment (background/border/radius/padding)
  with an icon+label `<h3>` heading (music/audio/droplet/image icons) -
  each settings group (Music/Voice/Watermark/Background) now reads as a
  distinct, bounded card instead of floating text in a loose row.

### 4. Unclear save behavior in settings

Settings WERE already autosaving (400ms debounce, see `saveSettingsDebounced`)
- there was just no clear, persistent indicator, only a plain text line at
the bottom of the panel that stayed blank until you touched something.

Fix: moved the indicator into the "Project settings" header as a colored pill
badge (`.savebadge`, green/amber/red dot for saved/saving/failed, matching
the `.stpill` status-pill pattern used elsewhere in the app), and gave it an
idle state ("Autosaved") set as soon as the panel's data loads
(`fillSettingsForm()`), not just after the first edit - so it's obvious from
the moment you open Settings that changes save automatically, with no
separate "Save" button needed.

### 5. BUG: stitching a chapter with no script doesn't restore on reopen

Root cause: `editor_state.json` (the file `openChapterEditor()` checks for
and auto-restores from) was ONLY ever written by an explicit "Save" click or
by adding/editing script lines (`markDirty()` → debounced autosave). Neither
`doDownload()` nor `loadPdf()` (the "Stitch PDF" action) ever called it, so
downloading/stitching a chapter and leaving before touching the script left
NOTHING on disk for that chapter - reopening it correctly found no saved
state and reported exactly that ("No saved editor state for ...").

Fix (`tools/framer/index.html`):
- `saveEditorState(recovery, opts)` now takes an optional `{silent:true}` -
  writes the real `editor_state.json` (so `openChapterEditor()`'s existing
  restore check finds it) without the "Saved." toast/status line a
  deliberate user Save gets, so it can be called automatically.
- `doDownload()` now sets `S.chapter_id`/`S.source_pdf` from the response and
  fires a silent checkpoint save right after a successful download.
- `loadPdf()` fires a silent checkpoint save right after a successful stitch.

Verified end-to-end: stitched a real chapter with 0 script lines, confirmed
`editor_state.json` was written (`lines: 0`, full strip metadata present),
then closed and reopened the chapter - it restored the strip/tiles with no
error (`"Loaded saved state ... - 0 line(s)."`) instead of the old error.

### Verified in-browser (Chrome, local `tools/framer/app.py`)

- Settings panel: cards, icons, styled Browse buttons, autosave badge (idle
  "Autosaved" → "Saving…" → "Autosaved HH:MM:SS") all confirmed visually.
- Chapter editor sidebar: "Re-render voice"/"Re-render video" now full-width,
  no overflow.
- Zoomed a real stitched strip (1667×643945px) down to 10%: strip centered
  with equal left/right gaps (was flush-left before the fix). Confirmed
  `screenToTrue()` math still correct at non-1.0 zoom.
- Reproduced bug 5 exactly (stitch, 0 lines, leave, reopen) against a real
  project chapter, confirmed the fix, then deleted the ~440MB of test
  tiles/state this created under `projects/the-stellar-swordmaster/chapters/2/`
  so the user's actual project data is unaffected (chapter 2 is back to
  untouched `not_started`, no `work/` folder).
- No console errors at any point.

### Files changed

- `tools/framer/index.html` (all 5 fixes)

### Next steps for the user

- No backend/pipeline changes, no new dependencies - just refresh the
  browser tab (or restart `start.bat`) to pick this up.
