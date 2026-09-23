# Work log

## Task: BUG - "Download PDF" fails on a fresh setup.bat install

### Root cause

`webtoon-downloader` is intentionally left out of `requirements.txt` (it's
optional, only needed for Framer's "Download PDF" button) - the README tells
users to `pip install webtoon-downloader` themselves. When installed, pip
creates ONLY a console-script entry point (`.venv\Scripts\webtoon-downloader.exe`)
- the package has no `__main__.py`, so `python -m webtoon_downloader` can
never work, regardless of how it's installed. Confirmed by actually installing
it into the project's `.venv` and inspecting site-packages.

`tools/framer/app.py`'s old `_webtoon_cmd()` tried `shutil.which("webtoon-downloader")`
first, and if that failed, fell back to `python -m webtoon_downloader`. That
`which()` call searches the app process's PATH - but `start.bat` launches
`app.py` with the bare venv python (`.venv\Scripts\python.exe "...\app.py"`),
never adding `.venv\Scripts` to PATH. So on a machine where the venv's
Scripts dir isn't separately on PATH, `which()` fails, the code falls back to
`-m webtoon_downloader`, and that always errors with exactly the reported
message ("'webtoon_downloader' is a package and cannot be directly executed").
It "worked" on the dev machine only because `webtoon-downloader` happened to
be resolvable via PATH there (e.g. a terminal with the venv activated).

### Fix

`tools/framer/app.py` (`_webtoon_cmd`): now looks for the console-script exe
next to the running interpreter first (`Path(sys.executable).parent /
"webtoon-downloader.exe"`) - this is exactly where `pip install` puts it when
installed into this app's own venv, and is correct regardless of PATH state.
Falls back to `shutil.which` for a global/other-env install. Dropped the
`python -m webtoon_downloader` fallback entirely since that invocation form
is never supported by this package.

Also updated the "not installed" error message, `requirements.txt`, and
`tools/framer/README.md` to tell users to install with
`.venv\Scripts\pip install webtoon-downloader` (into the app's own venv)
instead of a bare `pip install webtoon-downloader`, which could land in an
unrelated Python and still not be found.

Verified by actually `pip install`-ing `webtoon-downloader` into the repo's
`.venv` and confirming the new `_webtoon_cmd()` resolves
`.venv\Scripts\webtoon-downloader.exe` and that entry point runs (only
failure was a deliberately-omitted dependency from a `--no-deps` test
install). Uninstalled it afterward to leave the venv as found.

### Files changed
- `tools/framer/app.py` - `_webtoon_cmd()`, `_run_webtoon_downloader()` error message
- `requirements.txt` - comment
- `tools/framer/README.md` - install instructions

### Next steps for the user
No commands needed to pick up this fix. To actually use the Download button,
install the tool into the app's own venv:
```
.venv\Scripts\pip install webtoon-downloader
```
