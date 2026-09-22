# Work log

## Task: Security audit (report only) + in-app git-based update mechanism

## Part 1 - Security audit of git history (report only, nothing changed)

Requested before making the GitHub repo public. Findings:

1. **Commit count**: the entire history is **one commit** ("Initial commit:
   manhwa recap pipeline + framer tool"), 30 files total, single branch
   (`main`). This made a full-history audit fast and exhaustive rather than
   a sample.
2. **`.env` / credential files**: never committed. Checked via
   `git log --all --pretty=format: --name-only --diff-filter=A | sort -u`
   (every filename ever added, across all history) - no `.env`, and no
   filename matching `secret|credential|\.key$|\.pem$|password|token`.
3. **Hardcoded secrets in file content**: none found. Searched all commit
   content (`git log --all -p`) for: Groq key format (`gsk_...`), AWS key
   format (`AKIA...`), private-key PEM headers, generic
   `api_key=`/`password=` assignments, and a broad scan for any 40+
   character token-like string (only match: markdown/comment separator
   dashes, e.g. `------`, nothing secret).
4. **`.gitignore` - real gap found**: the **working tree** version (already
   edited earlier this session, uncommitted) correctly covers `.env`,
   `work/`, `output/`, `projects/`, `downloads/`, `series/`, `inputs/`,
   models (`*.pt`, `*.pth`, `*.safetensors`, `*.onnx`, `*.ckpt`, `*.bin`,
   `.cache/`, `huggingface/`), and generated media. But the **currently
   committed** `.gitignore` (`git show HEAD:.gitignore`) is missing
   `projects/` entirely (it has `work/downloads/output/series/inputs/` but
   not `projects/`). Nothing was actually leaked - `projects/` was simply
   never `git add`ed in that one commit - but **commit the updated
   `.gitignore` before pushing anything else**, or a future `git add -A`
   could commit the real `projects/the-stellar-swordmaster/` (697MB of
   actual chapter data) by accident.

**Bottom line: no secrets were ever committed, safe to make public once the
.gitignore fix above is committed.** No key rotation needed.

## Part 2 - In-app git-based update mechanism

**Approach chosen for non-git installs (you confirmed): make the installer
itself `git clone` the app** (over HTTPS, repo now public), rather than a
zip-download fallback. Reasoning: gives correct `git pull` semantics for
free afterward, "N files changed" reporting comes from git itself instead of
custom diffing, and it's one mechanism instead of two to maintain.

### `tools/framer/app.py` - `POST /api/update`
Runs `git rev-parse HEAD` (before) → `git pull --ff-only` → `git rev-parse
HEAD` (after) → if different, `git diff --name-only <old> <new>` for the
changed-file list and whether `requirements.txt` is in it, all in `ROOT`
with `GIT_TERMINAL_PROMPT=0` (so a missing/expired credential fails fast
with a clear error instead of hanging the request on an invisible prompt).
`--ff-only` deliberately never merges/rebases - if history has diverged it
fails cleanly with git's own message rather than risking a merge conflict
in a non-technical user's install. Returns `{ok, already_up_to_date,
files_changed, requirements_changed, message}`; a repo with no `.git` (an
old pre-this-feature install) gets a clear `not_git` response instead of a
crash.

### `tools/framer/index.html` - "Check for updates" button
Added to the Projects screen header (next to the theme toggle - an app-level
action, not a per-chapter one). Reuses the existing `.fetchresult`/`btnBusy`/
`showToast` patterns already in the codebase (same as "New project"'s fetch
feedback) rather than inventing a new UI pattern. Green (`.ok`) for already
up to date or a clean code-only update; red (`.warn`) when
`requirements_changed` or on any error, so the "go re-run setup.bat" message
doesn't just flash by in a toast.

### `installer/installer.iss` - restructured to `git clone`, not a file copy
This is a real architecture change from the previous version of this file
(which bundled app code via `[Files]`), needed so `git pull` has something
to pull *into*. Verified the core mechanism **in isolation before encoding
it in Pascal** (clone into a temp dir, `robocopy /E` merge into a
pre-populated target with unrelated files already in it - proved this
preserves everything and produces a fully working git tree, without going
through the installer or risking another MsgBox incident):
- `[Files]` now only bundles ffmpeg (straight into `{app}\vendor\ffmpeg\bin`
  as before - unaffected by the clone, since it isn't part of the git repo).
- New `CloneRepo()`: ensures git is present (`winget install Git.Git` if
  missing, same PATH-probe-with-fallback-paths pattern as `setup.bat`'s own
  Python detection), then `git clone --depth 1` into `{tmp}\repo_clone`
  (needs an empty target - `{app}` already has Inno's own `unins000.*` in it
  by this point, so can't clone directly into it), then `robocopy /E` merges
  the clone into `{app}` (no empty-target requirement, leaves the
  already-extracted ffmpeg and `unins000.*` alone).
- `CurStepChanged(ssPostInstall)` now calls `CloneRepo()` before
  `RunEnvSetup()`; `RunEnvSetup` exits immediately if the clone failed
  (no `setup.bat` to run yet).
- Finished-page text now distinguishes "couldn't download the app" from
  "downloaded fine but the Python/AI environment failed" - different causes,
  different fixes, shouldn't share one vague message.
- **Uninstaller rewritten**: app code is no longer Inno-tracked, so
  `[Code]`'s uninstall handler is now responsible for the whole install
  folder, not just the venv/ffmpeg extras it originally covered. Added
  `RemoveAppCodeKeepingData()`, which enumerates `{app}`'s actual top-level
  contents (`FindFirst`/`FindNext`) and removes everything **except** the
  six data folders - dynamic, not a hardcoded file list, so it doesn't need
  editing every time a file is added to the repo. "Yes, remove data" now
  also removes the app code afterward (nothing left to keep); "No" removes
  everything *except* the data folders.

### Verification performed
- `installer.iss` compiles cleanly (ISCC, no warnings) after every change.
- The clone+robocopy-merge mechanism was proven correct in an isolated Bash/
  PowerShell test *before* being written into Pascal Script: cloned a real
  repo into an empty temp dir, robocopy-merged it into a directory that
  already had stand-in `unins000.exe/.dat` + `vendor\ffmpeg\bin\ffmpeg.exe`
  in it, confirmed the result was a fully functional git working tree
  (`git status`/`git remote -v` both work) *and* that the pre-existing files
  were untouched.
- Given the previous task's incident (see below), **did not** run the
  restructured installer live/silently again - relied on compile success +
  code review for the Pascal Script changes, consistent with the standing
  decision to stop automating the interactive-dialog paths.
- `POST /api/update` tested for real against the actual project repo:
  returns `{"ok":true,"already_up_to_date":true,...}` correctly (nothing new
  upstream, as expected). The diff/`requirements_changed` logic was verified
  separately in an isolated throwaway git repo (bare repo + two clones,
  pushed a commit that touched `requirements.txt` and `app.py`, pulled it in
  the first clone): `git diff --name-only <old> <new>` correctly listed both
  changed files, confirming the requirements-changed detection is accurate.
- The frontend button was verified for real in a live browser against the
  running app (not just read): clicking it (called directly via
  `checkForUpdate()` in the page console to avoid flaky screenshot-based
  clicking) correctly showed "Already up to date." in green, matching the
  backend response, with the button correctly re-enabled afterward.
- `python -m py_compile` on `app.py` and `node --check` on the extracted
  inline JS both pass.
- All test artifacts (temp bare/clone repos, test Flask server) cleaned up;
  none of this touched the real project's git history or data.

### Files changed
- `tools/framer/app.py` - new `POST /api/update` route.
- `tools/framer/index.html` - "Check for updates" button + result display +
  `checkForUpdate()`.
- `installer/installer.iss` - restructured to git-clone based install (see
  above); `installer/INSTALLER_BUILD.md` updated to match, plus a new
  "In-app updates: your release workflow" section (what you personally do to
  ship an update: `git add && git commit && git push origin main` - that's
  the whole workflow, with notes on why `requirements.txt` changes need
  calling out separately and why `main` should never be rebased/force-pushed
  once users have pulled from it).
- `SETUP_GUIDE.md` - short "Getting updates" section for end users.

### Your release workflow (short version - full version in `installer/INSTALLER_BUILD.md`)
```powershell
git add -A
git commit -m "describe what changed"
git push origin main
```
Users click "Check for updates" whenever they want it (no auto-check on
launch) → restart the app, or re-run `setup.bat` first if you touched
`requirements.txt`. Never rebase or force-push `main` after users have
pulled from it - `git pull --ff-only` will fail cleanly (not corrupt
anything) but every user's next update breaks until they're told to fix it
manually.

### Next steps for you
1. Commit the updated `.gitignore` (adds `projects/` and a few build-output
   entries) **before** pushing anything else or making the repo public.
2. Make the GitHub repo public (this wasn't done for you - a repo-settings
   change on GitHub, not something in this codebase).
3. Do one real, interactive install + uninstall test of the restructured
   installer once the repo is public (this task relied on compile-testing +
   an isolated clone/merge proof, not another live run of the installer
   itself - see the incident note in `installer/INSTALLER_BUILD.md`).
