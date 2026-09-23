# Work log

## Task 1: BUG - script text + boxed frames "gone" on reopen (possible data loss)

### Is the work still on disk? YES - confirmed, nothing was lost.

Checked `projects/the-stellar-swordmaster/chapters/1/work/framer/editor_state.json`
directly: `saved_at: 2026-09-15T17:40:19+00:00`, 43 lines, 68 boxed frames
across them - the user's real work, fully intact, untouched. This was a
**loading/restore bug, not data loss**, confirmed before touching anything.

### What broke (and what didn't)

Extensive live investigation (fresh server processes, a 300-request
concurrent stress test mixing scoped/unscoped routes, direct Python
request-context tests, a real browser round-trip) could NOT reproduce a
deterministic defect in the actual restore code path
(`/editor_state/status` -> `/editor_state/load` -> `applyProject()` in
`tools/framer/index.html`) - every clean test loaded the real 43 lines/68
frames correctly. The ONE failure I did observe turned out to be against a
long-running leftover server process from earlier testing in this same
session (confirmed via its own log: a later launch attempt correctly
detected and refused to duplicate it) - the same class of "stale process"
issue this session's port-conflict work was about, not a defect in the
current restore logic itself.

That said, this hunt surfaced a REAL, concrete gap while reading
`openChapterEditor()`: it only ever checked `/editor_state/status`'s
`project` field (the explicit-save slot). If that slot is ever empty for
ANY reason - a stale process, an interrupted save, anything - the function
fell straight through to "Opened X. Download or stitch a PDF to begin.",
completely ignoring `recovery.json` (the periodic autosave), even when it
holds real, substantial prior work. That's the exact failure MODE the user
described: strip/tiles restore fine (a separate, file-based mechanism), but
script+boxes look gone, with no indication that recoverable data exists.

### Fix

`tools/framer/index.html`, `openChapterEditor()`: when the explicit save is
missing but a recovery snapshot exists, it's now loaded automatically
(reusing the same `loadEditorState(true)` path the manual "Restore" toast
already used elsewhere) instead of silently declaring nothing found, with a
toast explaining it was restored from autosave so the user knows to click
Save to make it permanent.

**Verified**: renamed away `editor_state.json` for chapter 1 (recovery.json,
same 43-line content, still in place), started a truly fresh server, opened
the chapter - it correctly auto-restored all 43 lines from recovery instead
of showing "start from scratch", with the "Restored your last autosave..."
toast. Restored the original file afterward; confirmed byte-identical (43
lines, 68 frames).

### Files changed
- `tools/framer/index.html` (`openChapterEditor` recovery fallback)

---

## Task 2: per-project frame entry/exit animation (stage 7)

### What stage 7 needed to change

Today's crossfade (`xfade transition=fade duration=0.35s`) was hardcoded in
FOUR places that all have to agree: `_plan()`'s timing math (consecutive
items overlap on the timeline by exactly the crossfade length),
`_build_filtergraph()`'s ffmpeg filter chain, `_build_cmd()`, and the
MoviePy fallback. All four now take the transition/duration as parameters
instead of reading the `CROSSFADE` constant directly, resolved ONCE per
render by a new `_resolve_animation(settings)` from the project's
`animation` setting (falling back to exactly `fade`/`0.35s` - today's fixed
values - when unset).

- **Style -> settings**: cut, fade, slide (+ direction: left/right/up/down),
  zoom. ffmpeg's `xfade` filter natively supports `fade`, `slideleft/right/
  up/down`, and `zoomin` (there's no matching built-in `zoomout`, so "zoom"
  always maps to `zoomin` - avoided fragile custom `xfade` expressions for
  a "sensible set" instead). "Cut" uses ffmpeg's `concat` filter instead of
  `xfade` (a 0-duration crossfade isn't a reliable no-op) - `_plan()`'s
  existing overlap formula already produces correctly NON-overlapping
  timestamps automatically when crossfade=0, so no separate cut-specific
  timing path was needed.
- **Duration**: a float (server clamps to 0.05-1.0s; the UI slider defaults
  to the 0.3-0.6s range asked for, centered on today's 0.35s).
- MoviePy (last-resort fallback if ffmpeg itself fails) only distinguishes
  cut vs. a plain crossfade at the chosen duration - it has no built-in
  slide/zoom transitions to match ffmpeg's, and this path is rarely hit.

### Settings plumbing

- `tools/framer/projects.py`: added `"animation": {"style":"fade",
  "direction":"left","duration":0.35}` to `_default_settings()` - `load()`'s
  existing deep-merge automatically backfills this into every OLDER
  project.json that predates the setting, no migration needed (verified
  against the real "the-stellar-swordmaster" project.json). `update_settings()`
  validates/clamps style/direction/duration. `media_settings_snapshot()`
  passes it straight through (no file paths to resolve) into
  `media_settings.json`, which stage 7 already reads per chapter.
- `tools/framer/app.py`: no changes needed - `/api/projects/<slug>/settings`
  was already a generic deep-merge passthrough.
- `tools/framer/index.html`: new "Frame Animation" card in Project Settings
  (style radios, slide-direction select, duration slider showing e.g.
  "0.35s"), wired into the existing `fillSettingsForm`/
  `currentSettingsFromForm`/autosave-on-change machinery - no new save
  mechanism, reuses what watermark/background already use.

### Verified

- Unit-tested `_resolve_animation()`: empty settings -> exactly `("fade",
  0.35)`; explicit cut/slide/zoom map correctly; garbage input falls back
  safely (clamped duration, default style/direction).
- **Regression check**: `_plan()`/`_build_filtergraph()` called with NO
  animation args (today's call signature) produce byte-identical output to
  calling them with explicit `fade`/`CROSSFADE` - confirmed via direct
  equality assertion, not just eyeballing. Existing crossfade/timing
  behavior is provably unchanged for any project that hasn't touched this
  new setting.
- Confirmed "cut" produces non-overlapping start times and a `concat`
  filter (no `xfade` anywhere in that filtergraph); confirmed "slide"
  produces the correct `xfade=transition=slideleft:duration=...` filter.
- Full settings round-trip tested against a scratch project: default ->
  update -> garbage-input clamping -> `media_settings_snapshot()`
  passthrough, all correct.
- Live in the browser against the real project: the new settings card
  renders and behaves correctly (verified "Fade" pre-selected, duration
  slider showing "0.35s"); changed it to slide/right/0.55s, confirmed it
  saved to BOTH `project.json` and chapter 1's `media_settings.json`, then
  reverted it back to the default and confirmed both files show the
  original fade/left/0.35s again - no lasting change to the real project.
  No console errors throughout.

### Files changed
- `stages/s7_assemble.py` (`_resolve_animation`, `_plan`,
  `_build_filtergraph`, `_build_cmd`, `_assemble_ffmpeg`,
  `_assemble_moviepy`, `run()`, docstrings)
- `tools/framer/projects.py` (`_default_settings`, `update_settings`,
  `media_settings_snapshot`)
- `tools/framer/index.html` (new settings card + wiring)

### Next steps for the user
- No new dependencies for either task. Refresh the browser tab to pick up
  the recovery-fallback fix; the animation setting is available immediately
  in each project's Settings panel and applies on the next render.
