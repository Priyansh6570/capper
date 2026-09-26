# Work log

## Task: "Open website" button, crash-safe storage, video background + Ken Burns

Four asks (two small, two large), all implemented and tested against the real
app/ffmpeg on this machine - not just read through.

### 1. "Open website" button

Added next to BOTH webtoon URL fields (the exact "Webtoon URL" field in the
chapter editor's Chapter Source panel, and the "Manhwa URL" field on New
Project) - `tools/framer/index.html`: a small icon-only button
(`openUrlField(id)`) that `window.open()`s the field's current value in a new
tab, prepending `https://` if the pasted value has no scheme. Widened the
existing `.row2` CSS rule (was scoped to `.sidecard`) so the same layout works
on the New Project screen too. Verified live in a real browser: empty field
shows a "Paste a URL first." toast; a filled field opens correctly
(`https://www.webtoons.com/...`) in a new tab.

### 2. Storage robustness audit (atomic writes + backups)

**Audited every write to a state file the user's boxing/script work lives
in.** Found NOT atomic: `Manifest.save()` (manifest.json - beats/panels, the
pipeline's whole state), `/editor_state/save` (editor_state.json AND
recovery.json - the framer's live session, autosaved every ~1.5s), `/export`'s
mapping.json write (the finished line/frame plan), and preview.json. Already
atomic: `projects.py`'s `_atomic_write` (project.json, media_settings.json) -
temp file + `os.replace`, but with no backup and (found while stress-testing
the new shared version) a real Windows concurrency bug - see below.

**Fix**: new `atomic_io.py` (project root, next to `manifest.py`) -
`atomic_write_text`/`atomic_write_json` (temp file in the same directory,
`flush()`+`fsync()`, `os.replace()` - never a half-written file even on a
crash mid-save) plus a rolling `<path>.bak` of whatever the save is about to
overwrite (itself written the same crash-safe way, and never overwritten with
empty/corrupt content). `read_json_with_backup_fallback` is the read side:
falls back to `.bak` transparently if the primary is missing/empty/corrupt.
Wired into: `Manifest.save()`/`load()`, `/editor_state/save`/`load`, the
`/export` mapping.json write, `preview.json`, and `projects.py`'s
`save()`/`load()`/`write_media_snapshot()` (replacing its own duplicate
`_atomic_write`). Also hardened `s7_assemble._load_mapping()` to try
`mapping.json.bak` before silently degrading to "one frame per line," and
`app.py`'s `_preview_payload()` to regenerate (not crash) on a corrupt
`preview.json`, since that function gates getting back to the actual saved
session via `/editor_state/load`.

**Bug found and fixed while stress-testing** (not by inspection - a
concurrent-writers unit test raised a real `PermissionError`): on Windows,
two threads calling `os.replace()` on the SAME destination path at nearly the
same moment can transiently fail with "Access is denied," even though each
individual replace is atomic - the race is between them, not within one.
Fixed with a per-path `threading.Lock` registry inside `atomic_io.py` (the
Flask app runs `threaded=True`), verified with an 8-thread x 20-write stress
test after the fix: zero errors, no leftover temp files, correct final
content.

**Verified**, not just written: a standalone script exercising
`atomic_write_json`/`read_json_with_backup_fallback` directly (first save, no
stray `.bak`; second save, `.bak` holds the first version; corrupted primary
transparently recovers from `.bak`; concurrent-writer stress test), plus a
real `Manifest.save()`/`load()` round-trip including simulated corruption,
plus a real `projects.save()`/`load()` round-trip - all passing.

### 3. Report: any remaining path where boxing/script could be lost?

One real, small, already-partially-mitigated residual risk, and it's a
tradeoff rather than a bug: the editor already autosaves 1.5s after the last
edit (debounced) plus every 30s regardless (`index.html`'s `markDirty`/
`saveEditorState`) - so the ONLY window where work could be lost to a crash
is that ≤1.5s (or up to 30s if edits stop but the tab is left mid-typing in a
text field that hasn't blurred) between an edit and its next autosave. This
is a UX tradeoff (saving on literally every keystroke would be excessive
traffic), not a storage-hardening gap - the file WRITE itself, whenever it
happens, is now crash-safe per #2 above. Two much smaller, accepted edge
cases: (a) two browser tabs open to the same chapter is a last-write-wins
race, not corruption (nothing rolls back a still-atomic write); (b) a save
that fails outright (e.g. disk full) surfaces as "autosave failed" in the UI
and leaves the PREVIOUS good file untouched (the new content only replaces it
via the final atomic `os.replace`, which never runs on a failed write) -
loud failure, never silent corruption. Everything else (manifest, mapping,
project settings) is now atomic + backed up per #2.

### 4. Video background (image or VIDEO, looped) + alternating Ken Burns

**Video background** - `projects.py`: `background.mode` gains a third value
`"video"` (alongside `"blur"`/`"image"`); the shared upload slot now accepts
video extensions too (`save_asset` also fixed to delete the OTHER kind's
stale file on upload - needed once one slot can hold either an image OR a
video, otherwise `find_asset`'s glob could resolve to the wrong leftover
file). `media_settings_snapshot()` adds a `"kind"` field stage 7 branches on.

`stages/s7_assemble.py` - a video background is architecturally different
from an image one: an image gets baked into each precomposed PNG with PIL
(unchanged); a video can't be, so instead each still is precomposed
TRANSPARENT (`_precompose(..., transparent=True)` - new RGBA path through
`_compose_still`/`_compose_grid_still`/`_paste_background`) and the whole
transition-chained foreground track is `overlay`'d onto the looped, scaled,
dimmed background video (`-stream_loop -1` input) as the last step of
`_build_filtergraph`. Frontend preview (`index.html`) is a real looping,
dimmed `<video>` element pointing at the existing asset-download route
(rather than a rendered still - there's no single static frame that shows a
moving background is actually moving).

**Alternating Ken Burns** - a checkbox in Project Settings
(`animation.ken_burns`), independent of the transition style. Each still gets
its own `zoompan` filter (`_zoompan_filter`) inserted before the transition
chain: even position (1st, 3rd, ...) zooms 1.0 -> 1.15 over its on-screen
time, odd position (2nd, 4th, ...) starts at 1.15 and eases back to 1.0 -
always centered, always >= 1.0 zoom so it never samples outside the still's
own canvas.

**Bug found and fixed while testing the combination** (not by inspection): a
real ffmpeg render with a video background + a watermark showed the
watermark's color darkened and its opacity under-reported. Root cause:
`_paste_background`'s transparent-mode path used `base.paste(wm, box, wm)`
(an RGBA image pasted using its OWN alpha as the mask) - that double-applies
the alpha (once as the blended color, again as the blend weight), which is
wrong specifically when the destination starts fully transparent. Fixed with
`Image.alpha_composite(base, wm)` (real "over" compositing); verified with a
targeted pixel-level check (a 50%-opacity white watermark over transparent
now correctly comes out `(255,255,255,128)`, not the darkened/under-opaque
result from before) AND a full ffmpeg render + extracted frame, confirmed
visually.

**Everything below was verified against the real bundled ffmpeg
(`vendor/ffmpeg`, NVENC available on this machine) with synthetic test
assets, not just read through:**
- A full render with a video background (no watermark, no Ken Burns):
  ffmpeg succeeded, output frame extracted and inspected - background test
  pattern visible and dimmed, opaque frame art correctly on top, no bleed.
- The SAME plus a semi-opaque watermark: confirmed correct color/opacity
  after the fix above (see bug report), both at the PIL layer and in an
  extracted rendered frame.
- Ken Burns alternation: measured the rendered rectangle's pixel width at the
  start vs. end of two adjacent stills' own on-screen spans - item 1 (zoom
  IN) grew 611px -> 687px, item 2 (zoom OUT) shrank 690px -> 629px, i.e. the
  alternation is real and visible in the actual encoded output, not just in
  the filter string.
- Confirmed the DEFAULT render path (no video background, no Ken Burns)
  produces a byte-for-byte identical filter_complex string to before this
  change - existing projects are unaffected.
- The settings API round-trip end to end through the real Flask app: upload
  a video as the background asset (`save_asset` correctly deleted the
  project's old `background.png` when replaced with `background.mp4`), set
  mode to "video" + ken_burns true, and read back `media_settings.json` -
  resolved to the right absolute path, `kind: "video"`, `ken_burns: true`.
- Settings UI in a real browser: the "Custom video (looped)" radio and
  Ken Burns checkbox render and wire up correctly; the live `<video>` preview
  element is correctly created/wired (couldn't confirm actual pixel playback
  visually - this sandboxed test browser appears unable to decode ANY test
  video, H.264 or VP9, even via a plain page navigation with no custom JS
  involved at all, which points to an environment/codec limitation of this
  specific browser automation tool rather than a bug in the feature; the
  server-side serving of the video - headers, HTTP range requests for
  seeking - was independently verified correct with curl).
- All test assets/uploads/settings changes were cleaned up / reverted
  afterward - the real project used for testing (the-stellar-swordmaster)
  is back to its original state.

**Render-time impact of Ken Burns (measured on this machine, RTX-class GPU,
h264_nvenc)**, a 12-beat / ~44s timeline: without video background, plain
7.11s vs. Ken Burns 10.80s (**+52%**); with a video background, plain 12.24s
vs. Ken Burns 19.08s (**+56%**). So roughly **1.5x slower**, fairly
consistently, regardless of video background. This lines up with the reason
it's off by default: `zoompan`'s per-output-frame crop math runs on the CPU
regardless of which encoder does the final encode, so GPU encoding doesn't
hide the cost the way it does for a plain looped still.

### Files changed
- `atomic_io.py` - new (see #2)
- `manifest.py` - `Manifest.save()`/`load()` use atomic_io; dropped the now-
  unused `json` import
- `tools/framer/projects.py` - `_atomic_write` now delegates to atomic_io;
  `load()` uses backup-fallback; `background.mode` gains `"video"`;
  `animation.ken_burns`; `_ASSET_EXTS["background"]` allows video
  extensions; `save_asset` cleans up the other kind's stale file;
  `media_settings_snapshot()` adds `background.kind`
- `tools/framer/app.py` - `/editor_state/save`+`load`, `/export`'s
  mapping.json write, and `preview.json` use atomic_io;
  `_preview_payload()` regenerates instead of raising on a corrupt
  preview.json; `_resolve_port()`'s one `except` broadened (unrelated,
  left over from the previous task - see git blame if this looks odd)
- `stages/s7_assemble.py` - `_load_mapping()` uses backup-fallback;
  `_resolve_animation()` returns `ken_burns` too; new
  `_resolve_video_background()`, `_zoompan_filter()`; `_paste_background`/
  `_compose_still`/`_compose_grid_still`/`_precompose` gain `transparent=`;
  `_build_filtergraph`/`_build_cmd`/`_assemble_ffmpeg`/`run()` thread
  `ken_burns`/`video_bg`/`bg_dim` through; `_assemble_moviepy` docstring
  notes it doesn't replicate either (rare fallback path)
- `tools/framer/index.html` - "Open website" buttons (#1); `link` icon;
  "Custom video (looped)" radio + live `<video>` preview; "Alternating Ken
  Burns" checkbox
- `tools/framer/README.md` - documents the video background architecture
  and Ken Burns in both the user-facing and technical sections

### Next steps for the user
Nothing required to pick this up - no new dependency, no migration (existing
`project.json`/`manifest.json` files load unchanged via the deep-merge
defaults). Try it: Project Settings -> Video background -> "Custom video
(looped)" -> upload an mp4/mov/webm/mkv; Frame Animation -> "Alternating Ken
Burns" checkbox. Expect renders to take noticeably longer with Ken Burns on
(see measured numbers above) - it's off by default for exactly that reason.
