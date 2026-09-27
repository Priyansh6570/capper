# Work log

## Task: Gemini API integration for narration script generation

Replaced the manual "copy chapter pages to Gemini yourself, paste the answer
back" step with a real Gemini API integration in the Framer editor. Verified
model IDs live against Google's current docs and confirmed the actual API
calls, settings round-trip, and per-part streaming work end-to-end against
the running app and a real chapter's already-prepared Gemini parts.

### Model / endpoint used

- **SDK**: `google-genai` (the current unified SDK - was already installed in
  `.venv`, version 2.8.0, but missing from `requirements.txt`; added it there
  along with `python-dotenv`, since both are now used directly by the live
  Framer app, not just the dead auto-pipeline stages the old comment referred
  to).
- **Default model: `gemini-3.1-pro-preview`** - verified via Google's live
  models doc (`ai.google.dev/gemini-api/docs/models`) as the current flagship
  Pro-tier model, and confirmed working with a real API call against this
  project's actual `GEMINI_API_KEY`. It's labeled "Preview" (Google's 2.5 Pro
  is "limited access" for new API keys, i.e. not a real alternative here) -
  configurable in Settings if that changes.
- **Faster alternative: `gemini-3.8-flash`** - also verified live. Selectable
  per-project in Settings → Script generation, exactly as asked ("switch
  between a higher-quality and a faster model if I hit rate limits").
- **Call shape**: `client.models.generate_content(model=..., contents=[types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")], config=types.GenerateContentConfig(system_instruction=..., temperature=0.7, max_output_tokens=8192))`
  - sends each `gemini_parts/*.pdf` (already split/compressed by the existing
    manual-flow feature) as inline bytes - all three real parts of a test
    chapter were well under the ~50MB inline limit (5-10MB each).
  - `google.genai.errors.APIError` has real `.code`/`.message` attributes
    (confirmed by triggering one) - `code == 429` drives the rate-limit
    retry/backoff path.

### Where the key is stored

`.env` in the project root - **never** in `project.json`/settings (this app's
existing convention: `HF_TOKEN`/`GROQ_API_KEY` are `.env`-only too). New
`GEMINI_API_KEY`-only read/write helpers in `tools/framer/app.py`
(`_read_env_dict`/`_set_env_value`, a proper read-modify-write, unlike
setup.bat's one-shot append for `HF_TOKEN`) back three new routes:
`GET /api/gemini/key_status` (set?/last-4-chars hint, never the real key),
`POST /api/gemini/key` (save/replace), `DELETE /api/gemini/key` (clear) -
wired to a new "Script generation (Gemini)" card in the Settings screen. The
key is global (shared across every project, like the app itself); tone,
narration type, style reference, and model choice are per-project, in
`projects.py`'s existing settings schema (new `"gemini"` block in
`_default_settings()`, validated/clamped in `update_settings()` exactly like
`watermark`/`animation` already are - backfills onto every existing project
automatically via the existing deep-merge-on-load mechanism, confirmed live
against a project created before this feature existed).

### Generate flow

- New `GET /gemini_script_stream?chapter_id=...&project=...&ch_no=...`
  (EventSource, same SSE pattern as the existing `/gemini_parts_stream` and
  `/merge_stream`): reads that project's saved `gemini` settings, walks
  `gemini_parts/*.pdf` **in order** (never re-splits anything - that's still
  entirely the existing manual-flow feature's job), and for each part:
  - Paces requests (`GEMINI_MIN_INTERVAL_S`: 8s for Pro, 3s for Flash - Pro's
    free-tier limit is tighter) and emits `{"type":"progress","part":i,"of_parts":n}`
    + a `log` line ("Generating part i of n…") the UI shows live.
  - On a 429, retries with exponential backoff (5s/10s/20s/40s/60s, 5
    attempts) and streams a live "rate-limited - retrying in Ns…" log line
    instead of the connection going quiet.
  - On a non-recoverable error, stops with a clear `{"type":"error"}` message
    naming which part failed and why, and the editor shows a **Retry** button
    right in that status line (re-runs the whole generation - see Limitations
    below).
  - The system prompt (`_gemini_system_prompt`) tells Gemini the tone,
    narration type, and (if set) to imitate the style reference's language/
    tone/humor; extract every dialogue line and beat faithfully but narrate
    as flowing prose, not a transcript; and - for every part after the first
    - not to re-narrate the repeated overlap pages, and - for every part but
    the last - not to write an ending.
- **Overlap dedupe** (`_dedupe_overlap`, unit-tested with both a real-duplicate
  and a no-duplicate case): the prompt instruction above is the first line of
  defense, but isn't 100% reliable, so after generating consecutive parts'
  text, it takes the last ~700 chars of part N and the first ~700 of part
  N+1, and runs `difflib.SequenceMatcher.find_longest_match` between them. If
  the match is substantial (≥30 chars), everything in part N+1 up to the end
  of that match is dropped (so the duplicate lead-in disappears, the rest of
  part N+1 continues untouched). If no strong match is found - the common
  case, when Gemini followed the instruction - it's a no-op, so healthy
  non-duplicated content is never wrongly trimmed.
- The stitched full script is sent back as `{"type":"done","script":"..."}`.
  The client sets `#scriptText`'s value to it and calls the **existing**
  `loadScript()` function unchanged - the exact same `/load_script` paragraph
  → sentence-boundary line-splitting a manual paste already goes through, so
  the result is immediately ready to box, with zero new splitting logic.

### UI

- **Settings → Script generation (Gemini)**: API key field (password input +
  Save/Clear, status line, a link to `aistudio.google.com/apikey`), Tone
  (serious/comedy/dramatic/epic) and Narration type (first-person hero POV /
  third-person recap) dropdowns, Model dropdown (Pro/Flash), and a Style
  reference textarea - all autosave into the project's settings exactly like
  every other field on that screen.
- **Editor → narration script panel**: a "Generate script with Gemini" button
  above the script textarea, with a live status line (part N of M / rate-limit
  retries / errors+Retry) - mirrors the existing "Chapter Source" panel's
  Gemini-parts progress UI (same SSE-driven pattern, new element ids).

### Verification (real API calls made against the user's actual GEMINI_API_KEY)

- Confirmed both `gemini-3.1-pro-preview` and `gemini-3.8-flash` respond
  successfully to a live `generate_content` call.
- Ran a real chapter part (52 pages, ~5MB PDF) through the actual
  `_gemini_generate_part`/`_gemini_system_prompt` code: produced genuinely
  good first-person narration prose faithfully covering the dialogue/beats in
  ~37s on Flash.
- Unit-tested `_dedupe_overlap`/`_stitch_script_parts` with a synthetic
  real-duplicate case (correctly trimmed, new content preserved) and a
  no-duplicate case (correctly left untouched, no false-positive trimming).
- Started the real Flask app and, against a real project: verified
  `/api/gemini/key_status|key` set/replace/clear round-trip against the ACTUAL
  `.env` file (backed up first, restored after - every other `.env` line was
  preserved untouched at each step); verified the `gemini` settings block
  backfills onto a pre-existing project and round-trips save/validate/clamp
  correctly (including falling back to defaults on invalid `tone`/`model`
  values); streamed `/gemini_script_stream` against a real 3-part chapter and
  watched the correct `start`/`log`/`progress` events arrive live (stopped
  after part 1 of 3 completed successfully, to bound API cost/time for this
  verification pass - not a full run through `done`).
- `py_compile` on all touched `.py` files; extracted and `node --check`'d
  every inline `<script>` block in `index.html`; no duplicate DOM ids.

### Known limitation (by design, for now)

A failure partway through a multi-part chapter currently means clicking
"Generate script with Gemini" again restarts from part 1 (no per-chapter
checkpoint of already-completed parts). The automatic 429 retry/backoff
already covers the common transient case within one run; a full restart is
the fallback only for a harder failure (e.g. a bad key, or a part that fails
even after 5 retries).
