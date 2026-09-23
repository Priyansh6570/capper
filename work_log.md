# Work log

## Task: BUG - Gemini-parts SSE stream shows only "disconnected"

### Root cause

`/gemini_parts_stream` (and `/api/projects/<slug>/merge_stream`) format every
SSE frame with a helper `_sse(obj) -> str`. That helper was **deleted** in
commit `332ca2e` ("Rebrand to ReCapper...") - which, despite the label, also
contained an unrelated large refactor of `/generate_stream` from an
SSE-stream-shaped job to a polled job queue (`Job`/`_JOBS`). `_sse()` was
only needed by the OLD `/generate_stream` implementation, so it was removed
as part of that rewrite - but `gemini_parts_stream` and `merge_stream` still
call `_sse(...)` on every single event and were never updated. Confirmed via
`git show 332ca2e^:tools/framer/app.py` (had `def _sse` at line 1029) vs.
`332ca2e` (gone) and a repo-wide grep showing zero definitions left.

Net effect: every real call to `/gemini_parts_stream` hits
`NameError: name '_sse' is not defined` on its very first `yield`, before any
SSE bytes reach the browser. `EventSource` can't read a body off a
connection that dies before/while headers are being written, so all the
browser ever sees is a generic connection failure - hence "Gemini-parts
stream disconnected," which is genuinely a different, more specific bug than
a real network drop. Reproduced directly: called
`app._build_gemini_parts_stream(...)` and confirmed the pre-fix code path
would raise; after the fix, ran it end-to-end against a real 2-page test PDF
and it produced a correct part file.

Not a "missing tool/dependency" issue this time (unlike the webtoon-downloader
bug) - PyMuPDF/Pillow are both fine on a fresh install. It's a plain
regression: a shared helper got deleted by an unrelated refactor and two of
its three call sites were never updated to match. It only looked like a
"fresh install only" issue because the dev's own re-testing after that
refactor apparently never exercised Download -> Gemini-parts again.

### Fix

1. **Restored `_sse()`** (`tools/framer/app.py`, next to `_now_iso()`) so
   `/gemini_parts_stream` and `/api/projects/<slug>/merge_stream` work again.
2. **Made errors actually surface**: wrapped `gemini_parts_stream`'s entire
   `stream()` body (not just the two spots that already had narrower
   try/excepts) in an outer `try/except Exception` that yields a proper
   `{"type": "error", "message": ...}` SSE frame for ANY exception - including
   ones from `config.chapter_scope` or anywhere else not already anticipated
   - so a bug like this can never again present as an opaque "disconnected"
   with the real error only visible in the server's own console.
   `merge_stream` already had equivalent broad coverage for its main body.

### Files changed
- `tools/framer/app.py` - re-added `_sse()`; `gemini_parts_stream()` now has
  an outer catch-all around its generator body

### Next steps for the user
No install steps needed - this was a pure code fix. Retry the Download ->
Gemini-parts flow; it should now either succeed or show the real error
message instead of "stream disconnected."
