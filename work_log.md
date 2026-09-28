# Work log

## Fixed: fresh install produces a blank shortcut icon and an app that never opens

### Investigation

Found the actual broken install at `G:\BOOKS\ReCapper` (from the Start Menu
shortcut's real target). The shortcut's icon file (`assets\icon.ico`) was
present and byte-identical to source, well-formed (7 sizes up to 256×256) -
**the blank icon is a Windows icon-cache artifact from repeated
install/reinstall testing, not a real bug.**

The actual blocker: `app_log.txt` in that install showed
`ModuleNotFoundError: No module named 'flask'` - the app crashes on every
launch attempt, completely silently, because it's started via
`pythonw.exe` (no console) through `start_hidden.vbs`. `setup_log.txt`
showed `setup.bat` printed "[OK] All packages installed." and
"SETUP COMPLETE." anyway, despite `pip install -r requirements.txt` having
actually crashed.

**Root cause #1**: `setup.bat` wraps every `pip install` in
`powershell -Command "& { ... | Tee-Object ... }"` to log output. That
wrapper does **not** propagate the wrapped command's real exit code back to
cmd.exe's `errorlevel` - reproduced directly: a command that exits 7 inside
that exact wrapper pattern reports `errorlevel 0` to the caller. So every
`if errorlevel 1 (...)` check after a `pip install` in `setup.bat` was dead
code; a pip failure was always silently treated as success. **Fixed**: added
`; exit $LASTEXITCODE` to the end of all three PowerShell script blocks, and
added a real, previously-missing post-install check (`import flask`) that
hard-fails setup with a visible error if the app's own dependencies didn't
actually install - the one thing standing between a broken install and a
false "SETUP COMPLETE" for any future cause of this class.

**Root cause #2** (what pip was actually failing on, and why it kept
failing on retries too): `requirements.txt`'s dependency set had two
genuine, permanent version conflicts and multiple resolver-performance traps,
found and fixed by iteratively dry-running the real `pip` resolver against
live PyPI (not guessing):

1. `chatterbox-tts>=0.1` let pip's resolver backtrack into old chatterbox-tts
   releases (0.1.3/0.1.4) that depend on the abandoned `pkuseg==0.0.25`
   package, which has no Windows/Python-3.13 wheel and crashes building from
   source (`BackendUnavailable: Cannot import 'setuptools.build_meta'`) -
   this is the exact crash in the user's `setup_log.txt`. Current chatterbox-
   tts (0.1.5+) depends on `spacy-pkuseg` instead (has real wheels). Floor
   bumped to `>=0.1.7`.
2. **The permanent conflict, found via precise pip error output once the
   above was fixed**: `chatterbox-tts` pulls in `gradio==6.8.0` (for a demo
   UI - verified chatterbox-tts's own importable code contains zero
   references to `gradio` anywhere), and gradio requires `aiofiles<25.0` and
   pulls `rich` toward 15.x, while `webtoon-downloader` requires
   `aiofiles>=25.1.0` and `rich<14`. **No version of either package
   satisfies both simultaneously** - not a slow resolution, a genuinely
   empty solution space. Same story for `moviepy` (`pillow<12`) vs
   `webtoon-downloader` (`pillow>=12.0.0`, verified moviepy 2.2.1 works fine
   against the newer Pillow in practice despite its stale upper bound).
   **Fixed**: `chatterbox-tts` and `moviepy` are now installed separately in
   `setup.bat` with `--no-deps` (after confirming their own package code
   never imports the conflicting package), with each one's *actual* runtime
   dependencies added to `requirements.txt` directly so nothing is missing.
3. `google-genai>=1.0` (current latest: 2.25.0) forced pip to explore a huge
   pydantic/google-auth version range alongside chatterbox-tts's own huge
   tree, taking 10+ minutes or hitting pip's own `resolution-too-deep` limit.
   Pinned to `==2.25.0`. `torch`/`torchaudio` pinned to `==2.6.0` to match
   chatterbox-tts's own exact pin for the same reason (step 4b of setup.bat
   still overrides these with the CUDA build right after).

After all of the above, the real `requirements.txt` resolves in one clean
pass with zero backtracking - verified directly with
`pip install --dry-run -r requirements.txt` against live PyPI.

### Verified

- Reproduced the exact silent-failure bug: a command that fails inside
  `setup.bat`'s exact PowerShell+Tee-Object wrapper reports `errorlevel 0`.
- Reproduced the user's exact pip crash (`pkuseg` sdist build failure) with
  the original `chatterbox-tts>=0.1`, confirmed it's gone with `>=0.1.7`.
- Found and confirmed the `gradio` vs `webtoon-downloader` (`aiofiles`,
  `rich`) and `moviepy` vs `webtoon-downloader` (`pillow`) conflicts are
  unconditional (pip's own `ERROR: ... have conflicting dependencies`
  output, not just slow resolution) - fixed by excluding both packages'
  metadata via `--no-deps` + their real deps listed explicitly.
- Confirmed `chatterbox-tts`'s and `moviepy`'s own package code contain zero
  references to `gradio` / no runtime breakage against the newer Pillow,
  respectively, before relying on `--no-deps` for either.
- Final check: `pip install --dry-run -r requirements.txt` (the real file)
  against live PyPI in a fresh venv - clean, fast, zero conflicts, zero
  backtracking. All scratch venvs/logs cleaned up afterward.

### Files changed

- `setup.bat` - `; exit $LASTEXITCODE` on all 3 `pip install` PowerShell
  wrappers; new post-install `import flask` verification (hard-fails setup
  on failure); two new install steps (4c: `chatterbox-tts --no-deps`, 4d:
  `moviepy --no-deps`), each with a WARN-not-fail policy since both are
  optional/fallback features (draft narration and the ffmpeg primary path
  still work without them).
- `requirements.txt` - `chatterbox-tts`/`moviepy` moved out of the normal
  resolved set (installed separately, see above) with their real runtime
  deps added directly; `google-genai`/`torch`/`torchaudio` pinned exactly;
  each change commented with the specific conflict it avoids.

### Next steps for the user

Re-run `setup.bat` on the broken install (`G:\BOOKS\ReCapper` or wherever) -
safe to re-run, picks up from the fresh `requirements.txt`. The blank
shortcut icon should resolve itself on the next icon-cache refresh (log
off/on, or a normal Windows icon cache rebuild) - it's not code-related.
