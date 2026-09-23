# Work log

## Task: move stage 7's watermark from on-top-of-frames to background-only

### Root cause

`stages/s7_assemble.py`'s `_precompose()` built each beat's full still (crop
already pasted over its background via `_compose_still`/`_compose_grid_still`)
and only THEN alpha-composited the tiled watermark layer onto that finished
still:

```python
still = _compose_still(it["frames"][0], bg)
if wm is not None:
    still = Image.alpha_composite(still.convert("RGBA"), wm).convert("RGB")
```

Since the frame crop was already baked into `still`, the watermark landed on
top of everything, including the comic art. The settings panel's watermark
live preview (`tools/framer/app.py`, `/api/projects/<slug>/preview/watermark.png`)
had the exact same bug, compositing onto the finished preview still the same
way - so the preview matched the (wrong) render behavior.

### Fix

Moved the watermark composite to happen on the BACKGROUND layer, before the
frame crop is pasted on top, in both `_compose_still` and
`_compose_grid_still` (`stages/s7_assemble.py`):

- Added `_paste_background(base, bg, wm=None)`: composites `wm` onto `bg`
  first (if given), then pastes the result onto `base`. Both compose
  functions now call this instead of `base.paste(bg, (0, 0))` directly.
- Both functions take a new optional `wm` param; the frame crop(s) are
  pasted on top of `base` AFTER `_paste_background()`, same as before - so a
  crop now always covers/hides whatever watermark tiling sits under it.
- `_precompose()` passes `wm` straight into `_compose_still`/
  `_compose_grid_still` instead of compositing it onto the finished still
  afterward.
- Updated `tools/framer/app.py`'s watermark preview route the same way
  (`_compose_still(str(sample), wm=wm)` instead of a post-hoc composite), so
  the settings panel preview now accurately shows the new behavior.

Tiling/opacity/size/angle are untouched - `_build_watermark_layer()` (which
builds the single 1920x1080 tiled RGBA layer from those settings) wasn't
touched at all, only WHERE that layer gets composited changed.

### Verified

Rendered real test stills directly through `_compose_still`/
`_compose_grid_still` with a solid-color placeholder "frame" and a high-
opacity tiled text watermark (so it's unambiguous in a screenshot):
- Single-frame layout: watermark tiles fill the whole background, but the
  frame crop area is completely clean - no watermark text anywhere on it.
- Grid ("together") layout, 2 frames: watermark shows in the gaps
  around/between both frame crops, absent from both crops themselves.

Both confirm the compositing order is now background -> watermark -> frame
crops, per the request. Test images were deleted after inspection - no
leftover files.

### Files changed

- `stages/s7_assemble.py` (`_paste_background` new, `_compose_still`,
  `_compose_grid_still`, `_precompose`)
- `tools/framer/app.py` (`api_preview_watermark`)

### Next steps for the user

- No new dependencies. Next real chapter render (or "Re-render video") will
  use the fixed compositing order automatically.
- The settings panel's watermark preview also now reflects this - worth a
  quick look there before a full render if you want to sanity-check a
  specific watermark config.
