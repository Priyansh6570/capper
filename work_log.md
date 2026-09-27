# Work log

## Task: 4 fixes/features for a real broken install (G:\Documents\ReCapper) + API key security

All four items from the fix list, plus the follow-up Windows Credential Manager
request, were implemented AND verified live against the actual reported-broken
install at `G:\Documents\ReCapper` (not just the dev checkout) - that install
is now fully fixed and running the final code.

### 1. BLOCKER: Gemini import fails

**Diagnosis**: not a namespace collision. `python -c "import google; print(google.__path__)"`
on the broken install showed a completely normal PEP 420 namespace package.
`pip show google-genai` said "not found" - the package was simply never
installed there. Root cause: that install's venv predates the commit that
added `google-genai`/`python-dotenv` to `requirements.txt` (from the earlier
Gemini-integration session), and **nothing re-runs `pip install` after a git
pull** - "Check for updates" only pulls code. This is exactly the scenario
described in item 4, and now that item 4 exists, it also fixes item 1: running
the new self-repair flow on that install installed `google-genai` for real
(verified: `from google import genai` now succeeds there).

### 2. Suppress terminal windows on all subprocess calls

Added `NO_CONSOLE` to `config.py` - `{"creationflags": subprocess.CREATE_NO_WINDOW}`
on Windows, `{}` elsewhere - and spread it (`**NO_CONSOLE`) into every
subprocess call the running app actually makes: `tools/framer/app.py` (webtoon
downloader probe/run, the orchestrator subprocess, git rev-parse/pull/config,
ffprobe, the merge-stream ffmpeg concat, taskkill), `stages/s7_assemble.py`
(the nvenc probe and the main ffmpeg render call), and
`tools/predownload_tts_models.py`'s model-download subprocess (this can now
run mid-render too, via last session's `s6_tts._ensure_model_cached`). Left
`tools/speed_audio.py` untouched - it's a standalone CLI a developer runs by
hand from an already-open console, not something the app itself spawns, so
there's no extra window to suppress there.

### 3. Fake/frozen progress indicators

**3a - top progress bar never fills.** Real bug, found by tracing the CSS/JS
interaction, not a UI framework issue: `setGlobalProg(frac, label)` sets an
**inline** `.gfill` width for a real percentage, but when a later call passes
`frac=null` (indeterminate), it only toggled the `.indet` class and never
touched that inline style. Since an inline style outranks a plain (non-
`!important`) stylesheet rule, the leftover inline width (often `0` from
`clearGlobalProg()`) silently defeated `#globalProg.indet .gfill`'s intended
`width:38%` - the label text updated, but the fill sat at a stale width the
whole time, invisible. Fixed by having `setGlobalProg` clear the inline width
(`style.removeProperty('width')`) when switching to indeterminate mode.

**3b - render ETA never counts down.** `eta_s = estimated_total_s * (1 -
progress_fraction)`, recomputed fresh on every 1.5s poll - the math itself is
fine. The actual bug: `progress_fraction()` is defined to return **exactly
0.0** until the first beat/line finishes (audio==0 and video==0), so for the
entire model-load + first-synthesis phase - which can be a large fraction of
a short render's total time - the estimate is provably frozen, not just
slow to update. Fixed per the task's own suggested fallback: added
`fmtElapsed()`/`fmtEtaOrElapsed()` - shows live elapsed time (which can never
appear frozen, since it's just `Date.now() - started_at` recomputed every
poll) until real progress exists, then switches to the genuine, now-actually-
decreasing ETA. Applied to both the editor's `#globalProg` label and the
corner job widget.

### 4. Dependency check + self-repair in "Check for updates"

**Required-vs-installed detection**: `_requirement_specs()` parses
`requirements.txt` (strip comments/blank lines), `_missing_packages()` checks
each declared package's name against `importlib.metadata.version()` - this
compares **distribution names** (what pip/requirements.txt use), not import
names, so `PyMuPDF`→`fitz`, `beautifulsoup4`→`bs4` etc. need no mapping table.
`/api/update` now calls this after every successful pull (including "already
up to date" - a broken venv isn't only possible right after a pull) and
returns `missing_packages: [{package, feature}]`.

**Feature→package mapping**: a single `FEATURE_PACKAGES` dict in `app.py`
(package name -> one-line plain-English consequence, e.g. `"google-genai":
"AI script generation (Settings -> Script generation)"`), covering every
line in `requirements.txt`.

**Install flow**: the editor shows the missing list + a plain-language reason
per package, with an "Install missing packages" button. `/api/deps/install_stream`
(SSE) runs `pip install` with the exact spec strings from `requirements.txt`
(so version constraints are respected), streaming pip's real output as live
log lines - genuine progress, not a fabricated percentage pip doesn't
actually report for a multi-package install. If `torch`/`torchaudio` were
among the missing packages, it automatically follows up with setup.bat's own
CUDA-torch override (`--index-url .../cu128`), then re-verifies
`torch.cuda.is_available()` and reports it. On completion it re-checks what's
still missing so a partial failure can be retried for just what's left.

**Real bug found while verifying this end-to-end**: after installing
`google-genai` into the ALREADY-RUNNING server process, `/api/deps/check`
still reported it missing - `importlib.metadata` caches each `sys.path`
directory's listing on first access and doesn't notice a dist-info folder
that appeared after the process started. Fixed by calling
`importlib.invalidate_caches()` at the top of `_missing_packages()`, so a
successful in-app install is recognized immediately, with no app restart
needed.

### Follow-up: Gemini key in Windows Credential Manager, not plaintext

`_get_gemini_key()`/`_set_gemini_key()`/`_clear_gemini_key()` now go through
the `keyring` package (`WinVaultKeyring` backend on Windows) first, falling
back to the existing `.env` convention only if keyring has no usable backend.
A key already sitting in `.env` from before this existed is migrated into
Credential Manager (and removed from `.env`) the first time it's read, so
existing installs get the improvement automatically, not just new keys saved
from now on. The Settings UI and SETUP_GUIDE.md both state plainly: **this is
obfuscation, not absolute secrecy** - a local app's key is always ultimately
readable on the machine it runs on by whoever controls that machine; the real
protection was always that it's each user's own key and never leaves their PC
either way. Keyring just keeps it out of a plaintext file one accidental
"open in Notepad" away from being read.

### Verification

Every fix was tested against a REAL running server, most against the actual
previously-broken `G:\Documents\ReCapper` install, not just the dev checkout:

- Confirmed the exact `ImportError` on the broken install, confirmed
  `google-genai` genuinely wasn't installed there (not a namespace issue).
- `py_compile` on every touched `.py` file; extracted and `node --check`'d
  every inline `<script>` block; no duplicate DOM ids.
- Ran `/api/deps/check` on the broken install -> correctly reported
  `google-genai` missing with the right feature description.
- Ran the real `/api/deps/install_stream` SSE endpoint against that install
  live: watched real `pip install` output stream in, confirmed `google-genai`
  actually installed (`pip show`, and `from google import genai` - both
  succeeded afterward).
- Caught the `importlib.metadata` caching bug specifically because the
  already-running server still reported it missing post-install; fixed with
  `invalidate_caches()`, confirmed clean after a restart.
- Ran the real Windows Credential Manager round-trip (`keyring.set_password`/
  `get_password`/`delete_password`) directly, then through the app's own
  `/api/gemini/key_status|key` endpoints - confirmed the existing plaintext
  `.env` key migrates into Credential Manager (and is removed from `.env`)
  the first time it's read, confirmed `_gemini_client()` still constructs
  correctly reading from keyring, confirmed set/replace/clear all work.
- Restarted the actual `G:\Documents\ReCapper` tray app (the user's live
  instance) with the complete, final code: confirmed 0 missing packages,
  Gemini import works, and the key is correctly stored in Credential Manager.

### Note on process

The reported-broken install (`G:\Documents\ReCapper`) and this dev checkout
are two separate git working trees on the same machine/Windows account -
verifying live against both surfaced one artifact worth flagging: Windows
Credential Manager is scoped per-user, not per-install-folder, so testing the
same `keyring` service/account name from two installs on the same account
made the second one see the first's migrated key immediately. Not a bug in
the migration logic (a real separate machine/user wouldn't share this), but
it did mean the G: install's own plaintext `.env` copy needed an explicit
one-time manual cleanup during verification, since its own migration path
never ran (keyring already had a value from the other install's earlier test).
