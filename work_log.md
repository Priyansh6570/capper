# Work log

## Fixed: "Stitch PDF" MemoryError on kingofshojo.com chapters (webtoons.com unaffected)

### Investigation

User's diagnosis was right on target ("i think it downloads images you need
to adjust support accordingly"). Downloaded a real chapter and inspected the
PDF `sites/kingofshojo.py` builds: its pages' MediaBox was the raw image's
**pixel** dimensions used directly as **point** dimensions (e.g. a page from
a 2560x1435px source image measured 2560x1435 **pt** - Pillow's PDF writer
does this by default when no `resolution=` is given, i.e. an implicit
72 DPI tag). `stages/s1_pdf_to_pages.py`'s webtoon path re-renders every PDF
page at `config.PAGE_DPI` (150), i.e. `zoom = 150/72 ≈ 2.08x` - so an
untagged page gets rendered at **~2x its own already-full-resolution pixel
size**. Across a 61-page chapter, that's every page held in memory at
roughly 4x its real pixel count before they're even composited into one
strip - confirmed exactly this: a real chapter's final strip measured
5334x130320px (695 megapixels) before the fix, for source images that were
never anywhere near that large. That's what was exhausting memory.
`webtoon-downloader` (the external CLI powering webtoons.com) apparently
tags its own PDFs with a sane DPI already, which is why that site's Stitch
never had this problem.

### Fix

`tools/framer/sites/kingofshojo.py` - `download_chapter()` now saves the PDF
with `resolution=PAGE_DPI` (imported from `config.py`, the same constant
`s1_pdf_to_pages.py` renders at). This makes the re-render at `PAGE_DPI`
reconstruct the original pixel dimensions exactly (1:1), instead of
upscaling them.

### Verified

- Re-downloaded the same real chapter: page 0's MediaBox went from
  2560x1435pt to 1228.8x688.8pt (`2560/150*72 = 1228.8`, exactly as
  intended); page 1 similarly 800x1067px -> 384x512.16pt.
- Ran the real Stitch step on the fixed PDF: strip size dropped from
  5334x130320px (695 Mpx) to **2560x62550px (160 Mpx)** - a 4.3x reduction -
  width now correctly matches the widest SOURCE image (2560px) instead of an
  inflated ~5334px. No MemoryError.
- Stitch time also dropped from 14.3s to 5.2s as a side effect of stitching
  1/4 as many pixels (on top of the earlier PNG `compress_level` fix, which
  is still in effect and compounds with this one).
- Verified directly on the actual affected install (`G:\BOOKS\ReCapper`),
  not just the dev checkout. Test artifacts cleaned up afterward.

### Files changed

- `tools/framer/sites/kingofshojo.py` - `pages[0].save(..., resolution=PAGE_DPI)`.

### Next steps for the user

None - re-download and re-stitch a kingofshojo chapter; it should now work
the same as a webtoons.com chapter.

---

## Fixed: chapter list only showing 4 of 388 chapters (CSS clipping bug)

### Investigation

Verified the data pipeline was entirely correct: `project.json` on disk had
all 388 chapters, `GET /api/projects/ultimate-son-in-law` returned all 388,
and the frontend's `renderChapterList()` had no row limit - so this was not
a fetch/pagination bug of any kind (no "load more as you go" feature exists
or is needed).

Used a forked browser-automation check to inspect the live DOM: all 388
`.chrow` elements WERE present and correctly rendered - the bug was purely
CSS. `.chaptercard` (wraps `#chList`) has `overflow:hidden` and sits inside
`.screenbody`, a `display:flex; flex-direction:column` container. Without
`flex-shrink:0`, flexbox shrank `.chaptercard` down to ~297px (enough for
~4 rows) to fit the available space, and `overflow:hidden` silently clipped
the rest instead of showing them - `.screenbody`'s own `overflow-y:auto`
(the intended page scrollbar) never got the chance to take over.

### Fix

`tools/framer/index.html` - added `flex-shrink:0` to `.chaptercard`, so it
renders at its natural (full-content) height and the page scrolls normally.
Verified via browser automation: `.chaptercard` height went from ~297px to
~50,937px (its true content height), all 388 rows visible/scrollable, screen
shot confirms normal list rendering.

## Investigated: slow chapter download / stitch / narration despite fast internet

Findings, in the order they actually run:

1. **Chapter download (kingofshojo.com) - real, fixed.**
   `sites/kingofshojo.py`'s `download_chapter()` fetched every page image
   with a bare sequential `requests.get()` call - no connection reuse (a
   fresh TCP+TLS handshake per image) and no concurrency. For a ~150-image
   chapter this is dominated by per-request round-trip latency, which
   bandwidth doesn't fix - 1 Gbps helps once bytes are flowing, not with
   how many separate handshakes have to happen first. Fixed: a shared
   `requests.Session` (connection keep-alive) + an 8-worker thread pool
   (the work is I/O-bound, so Python's GIL isn't a limiter). Verified live
   against the real site: a 61-image chapter completed in 3.7s end-to-end.

2. **Stitching - real, fixed, but NOT a network step at all.**
   `stages/s1_pdf_to_pages.py`'s webtoon path renders every page then
   composites them into ONE giant strip image (a real chapter measured
   5334x130320px = ~695 megapixels before the DPI fix above) and saves it
   as PNG. Pillow's default PNG `compress_level` (6) spends a lot of CPU
   squeezing a file that's only ever read back on the same machine, never
   distributed. Measured: saving a 96-megapixel test image took 13.5s at
   the default level vs 7.8s at `compress_level=1` - and real chapters here
   run several times larger than that test image, so the effect compounds.
   Changed the strip save to `compress_level=1` (larger file on disk,
   meaningfully faster to write). This step never touches the network
   either way - it was never going to speed up from faster internet.

3. **Text-to-speech - not a bug, explained.** Chatterbox TTS runs entirely
   locally (GPU with automatic CPU fallback) - it does NOT use the network
   at all except once, during `setup.bat`, to download the model weights.
   Confirmed this machine's GPU (RTX 3050 Laptop, 4GB VRAM) is detected and
   used (`torch.cuda.is_available() == True`), and the model is loaded ONCE
   per chapter render and reused for every line (not reloaded per line -
   checked `stages/s6_tts.py`, no inefficiency found there). The real
   narration wait is inherent to running a full neural TTS model on a
   modest 4GB laptop GPU - CLAUDE.md's own setup notes already say as much
   ("tested on a 4GB laptop GPU... works, just a bit slower"). Internet
   speed has no effect on this step.

### Secondary note (not fixed, flagged for later)

The browser-automation check that verified the `.chaptercard` fix also hit
a one-off screenshot timeout while the page was rendering the full
388-row, ~51,000px-tall unvirtualized chapter list - retried fine
immediately after. Not confirmed as a real user-facing slowdown (may have
just been the screenshot tool), but if a project's chapter list is ever
reported as sluggish to scroll/interact with, list virtualization would be
the fix.

Also (unrelated, found incidentally while testing launches): a pre-existing
race in `_resolve_port()` reproduced consistently in this session's testing
- two near-simultaneous launches can each decide the port is free before
either binds it, leaving one process's Flask thread dead (silent bind
failure) while its tray icon survives as a harmless but confusing orphan.
Not the cause of anything in this session's reports; noted in the previous
work-log entry and still not fixed (needs a proper lock file/mutex instead
of racing on the TCP port).

### Files changed

- `tools/framer/index.html` - `.chaptercard { flex-shrink:0 }`.
- `tools/framer/sites/kingofshojo.py` - concurrent (`ThreadPoolExecutor`,
  8 workers) + connection-reused (`requests.Session`) image downloads.
- `stages/s1_pdf_to_pages.py` - `compress_level=1` on the stitched strip's
  PNG save.

### Verified

- Live browser check (forked agent) confirmed the CSS fix: full 388-row
  height, scrollable, screenshot looks normal.
- Live download against the real kingofshojo.com site: 61 images in 3.7s.
- Live stitch of that same 61-page chapter on the actual affected machine:
  14.3s total (render + composite + save) for a 695-megapixel strip (before
  the DPI fix above further cut this to 5.2s / 160 megapixels).
- All fixes copied onto and verified against the actual affected install
  (`G:\BOOKS\ReCapper`), not just the dev checkout. Test artifacts (temp
  chapters, timing-test work folders) cleaned up afterward.

### Next steps for the user

None needed on your end beyond normal use.
