# Work log

## Three bug fixes: editor state persistence, Stitch button guard, webtoon-downloader missing on fresh install

### 1 & 2. Chapter Source / Stitch panel state was lost on reopen; Download and Stitch buttons stayed clickable after already being done

**Root cause**: the Download/Stitch UI state (downloaded PDF path, Gemini
parts, stitched strip, button enabled/disabled) was never actually persisted
anywhere - `editor_state.json` only ever held the framer's own boxing data
(`chapter_id`, `source_pdf`, `lines`). Reopening a chapter re-derived nothing
for the Chapter Source / Stitch Strip panels, so Download/Stitch always came
back in their default (enabled, empty) state regardless of what was already
on disk.

**Fix**: added `GET /chapter_source_status?chapter=<id>` in `tools/framer/app.py`,
which inspects disk directly - `download/*.pdf`, `gemini_parts/*.pdf`, and the
stitched strip (via the existing `_preview_payload()`) - and returns
`{pdf, gemini_parts, strip}` (each `null` if that step hasn't happened). New
helpers: `_downloaded_pdf()`, `_existing_gemini_parts()`.

Frontend (`tools/framer/index.html`): `refreshChapterSourceStatus()` calls
this endpoint and `applyChapterSourceStatus()` applies it - populates the PDF
path field, shows the Gemini-parts result panel, restores the stitched strip
preview (factored `applyStripPreview()` out of `applyProject()` so both the
disk-derived path and the `editor_state.json` restore path share it), and
**disables** the Download / Stitch buttons when that step is already done,
each with a small "Re-download" / "Re-stitch" link to force it again.

Wired in at every point state can go stale:
- `openChapterEditor()` - always, before checking `editor_state.json`/recovery.
- `doDownload()` / `loadPdf()` - in their `finally` blocks (after `btnBusy(..., false)`,
  which otherwise unconditionally re-enables the button).
- The standalone `loadProjBtn` ("Load") handler.
- `resetEditorState()` - clears the status divs/re-enables both buttons before
  the new chapter's real state is fetched, so a stale state never flashes.

This directly covers bug #2 (Stitch button guard) as well - same mechanism,
same endpoint.

### 3. `webtoon-downloader` missing on every fresh install

**Root cause found**: `requirements.txt` deliberately EXCLUDED
`webtoon-downloader`, with a comment saying it was optional and had to be
installed manually (`.venv\Scripts\pip install webtoon-downloader`).
`setup.bat` only ever runs `pip install -r requirements.txt` - so no fresh
install (via `setup.bat` or the Windows installer, which runs it unattended)
ever had it, no matter how many times this was "fixed" before, since the fix
never touched the one file that actually drives a fresh install.

**Fix**: added `webtoon-downloader>=2.4` to `requirements.txt` directly, and
removed the stale "install it yourself" comment. Verified before adding it
that this is safe: its own PyPI metadata has NO `torch` dependency (so it
can't fight step 4b's CUDA-torch override), and its `beautifulsoup4`/`pillow`/
`pymupdf` pins are compatible with what's already pinned here. Verified its
console-script entry point is named exactly `webtoon-downloader`, matching
what `tools/framer/sites/webtoons.py`'s `_cli()` looks for next to the venv's
`python.exe`.

Updated `tools/framer/README.md` to stop telling users to install it by hand.

### Verified

- `/chapter_source_status`: downloaded a real kingofshojo chapter, confirmed
  `pdf` populated / `strip` null; stitched it, confirmed `strip` now populated
  with the right tile count; ran the Gemini-parts split, confirmed
  `gemini_parts` reports the same part the live SSE stream produced.
- Confirmed `webtoon-downloader`'s wheel has no torch dependency and its
  entry-point script name matches what the app looks for.
- All test artifacts (`work/srctest/`, scratch scripts) cleaned up after.

### Files changed

- `tools/framer/app.py` - `/chapter_source_status` route + `_downloaded_pdf()`
  / `_existing_gemini_parts()` helpers.
- `tools/framer/index.html` - `refreshChapterSourceStatus()`,
  `applyChapterSourceStatus()`, `applyStripPreview()` (factored out), new
  `#dlStatus`/`#stitchStatus` hint divs, wiring in `openChapterEditor()`,
  `doDownload()`, `loadPdf()`, `loadProjBtn`, `resetEditorState()`.
- `requirements.txt` - added `webtoon-downloader>=2.4`.
- `tools/framer/README.md` - documented the disk-derived button state; dropped
  the manual-install instruction for `webtoon-downloader`.

### Next steps for the user

Run `setup.bat` again (or just `.venv\Scripts\pip install -r requirements.txt`)
to pick up `webtoon-downloader` on existing installs. New installs need no
manual step now.
