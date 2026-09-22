# Building the Windows installer

`installer.iss` is an [Inno Setup](https://jrsoftware.org/isinfo.php) script that
produces a single `ManhwaRecapStudio_Setup.exe`. This doc covers: installing
Inno Setup, preparing the one thing that must exist before you compile
(a bundled ffmpeg), compiling, and exactly what happens at **install time**
vs the app's **first run** afterward.

## 1. Install Inno Setup (one-time, on the machine that builds the installer)

Either:
```powershell
winget install -e --id JRSoftware.InnoSetup
```
or download the installer yourself from <https://jrsoftware.org/isdl.php> and
run it (defaults are fine). This installs the compiler at:
```
C:\Program Files (x86)\Inno Setup 6\ISCC.exe
```

## 2. Stage a static ffmpeg build (required before compiling)

The installer bundles ffmpeg so end users don't need it on PATH. `vendor/`
is gitignored (it's a local build artifact, same as `.venv`), so **you must
populate it yourself before compiling** - `installer.iss` will fail to
compile if `vendor\ffmpeg\bin\ffmpeg.exe` doesn't exist. Easiest way: just
run `setup.bat` once in the project root (it downloads exactly this) -

```powershell
cd D:\Work\manhwa-recap
.\setup.bat
```

then delete the unused `ffplay.exe` it also extracts (not needed, ~100MB):
```powershell
Remove-Item vendor\ffmpeg\bin\ffplay.exe -ErrorAction SilentlyContinue
```

(Or fetch/extract a build yourself - the "essentials" build from
<https://www.gyan.dev/ffmpeg/builds/> - into `vendor\ffmpeg\bin\` so
`ffmpeg.exe` and `ffprobe.exe` land directly in that folder.)

## 3. Compile

```powershell
cd D:\Work\manhwa-recap\installer
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
```
Output: `installer\output\ManhwaRecapStudio_Setup.exe` (~65-70MB, mostly the
bundled ffmpeg). You can also open `installer.iss` in the Inno Setup IDE
(`Compil32.exe`) and press F9/Ctrl+F9 instead of the command line.

Re-run step 3 any time you change `installer.iss` or the app code - no need
to redo step 2 unless you want to update the bundled ffmpeg itself.

## What happens when someone runs `ManhwaRecapStudio_Setup.exe`

### Install time (inside the installer wizard)
1. **Welcome / license-free intro**, then **Select Destination Location** -
   the user picks (or accepts the default per-user) install folder.
2. **Disk space check** (our own, on top of Inno's normal check): computes
   free space on whichever drive was picked and refuses to continue (with a
   plain-language explanation - the app itself, plus ~2.5GB for the Python/
   CUDA environment, plus ~4GB for the AI voice model) unless there's
   **~10GB free**. The user can Browse to a different drive and try again.
3. **File copy** - just the bundled ffmpeg extracts to the chosen folder
   (into `vendor\ffmpeg\bin`). Fast, this is just decompression.
4. **Download the app** - a console-hidden step: ensures Git is present
   (installs it silently via winget if missing), then `git clone`s
   `{#RepoURL}` (branch `{#RepoBranch}`) into a temp staging folder and
   merges it into the install folder with `robocopy` (git clone itself needs
   an empty target, but Inno has already placed its own uninstaller files
   there by this point - robocopy has no such requirement and leaves those,
   and the already-extracted ffmpeg, untouched). The result is a **real git
   working tree**, not a plain file copy - that's what lets the in-app
   "Check for updates" button `git pull` it later. **This is why the GitHub
   repo must be public**: no credentials are available to an anonymous
   install, so a private repo would make every clone/pull fail with an auth
   error.
5. **Post-install environment setup** - this is the slow part. A console
   window opens running the just-cloned `setup.bat` (unattended:
   `SETUP_UNATTENDED=1`, so it skips its own "press any key"/"continue
   without a GPU?" prompts) - GPU/driver check, Python 3.13 (installed via
   winget if missing), `.venv` creation, `pip install -r requirements.txt`,
   then a **deliberate override**: reinstalling `torch`/`torchaudio` from
   the PyTorch cu128 index, because `chatterbox-tts` pins an exact non-CUDA
   torch version as its own dependency and would otherwise silently
   downgrade it (this is a real bug we hit and fixed while building this -
   see `work_log.md`). Then an ffmpeg check, and finally the installer
   independently re-verifies `torch.cuda.is_available()` itself (not just
   trusting `setup.bat`'s own exit code) so it can never claim GPU
   acceleration works when it doesn't. Typically 10-30+ minutes depending on
   internet speed; the console window shows live plain-English progress
   throughout.
6. **Finished page** - never just says "Complete." It always states the
   real GPU-acceleration status, and if the download or environment setup
   genuinely failed, says so explicitly (with a pointer to `install_log.txt`
   when there's a log to point to) and that it's safe to run the installer
   (or `setup.bat`) again. An optional checkbox launches the app immediately
   (only offered if everything actually succeeded).

Also created: a Start Menu entry, an optional desktop shortcut (checkbox on
the "Select Additional Tasks" page, checked off by default), and an
uninstaller.

### First run (after the installer window has already closed)
The **Chatterbox AI voice model (~4GB) is NOT downloaded during install** -
Inno Setup has no way to show progress for it, because it doesn't happen
until later. It downloads automatically the first time the app actually
needs it: when someone clicks **"Generate audio + video"** in the Framer UI
for the first time ever. What the app itself needs changed so that moment
shows real progress instead of looking frozen is reported (by request) in
the chat/root `work_log.md`, rather than implemented here.

### Uninstalling
Since the app code is now a `git clone`, not an Inno-tracked file copy, Inno
itself has nothing to auto-remove - `[Code]`'s uninstall handler is
responsible for the whole install folder. It asks **once**: "Also remove
the downloaded AI voice model and your app data?"
- **Yes**: deletes `work/`, `output/`, `projects/`, `downloads/`, `inputs/`,
  `series/` inside the app folder, the exact Chatterbox model folders
  (`models--ResembleAI--chatterbox` and `-turbo`, by exact name only - never
  a wildcard/whole-cache wipe, so unrelated Hugging Face downloads from
  other tools are left alone) from the shared cache under the user's
  profile, then everything else (the app code, `.git`, `.venv`, `vendor`).
- **No**: removes everything *except* those six data folders (enumerated
  dynamically from what's actually in the install folder, not a hardcoded
  list, so it doesn't need updating when new top-level files are added to
  the repo) - the "program" goes, the "content" stays, e.g. if reinstalling
  later.

No is the safe default and is also what an unattended (`/VERYSILENT`)
uninstall does automatically, since there's no one to ask. A closing note
mentions that a winget-installed Python or Git were left in place (other
apps might use them) and how to remove them manually if wanted.

## In-app updates: your release workflow

The Framer UI has a **"Check for updates"** button (top of the Projects
screen) that runs `git pull --ff-only` in the install folder - it touches
**only** git-tracked source files; `.venv`, installed packages, and the
Hugging Face model cache are never touched (none of them are tracked by
git - see `.gitignore`). It reports one of: already up to date / "Updated N
file(s), restart to apply" / "Updated N file(s), dependencies changed -
please re-run setup.bat" / an error.

**To ship an update, from your own dev checkout (this repo):**
```powershell
git add -A
git commit -m "describe what changed"
git push origin main
```
That's it - the next time any user clicks "Check for updates," `git pull`
picks up exactly what you pushed. A few things worth knowing:
- If your commit changed `requirements.txt`, users will be told to re-run
  `setup.bat` before restarting - so mention that in how you tell people
  about the update, don't rely on them noticing the in-app message alone.
- The pull is `--ff-only` (fast-forward only, deliberately) - never rebase
  or force-push `main` after users have already pulled older commits from
  it; that diverges their history from yours and their next update will
  fail with a clear-but-unhelpful-to-them git error instead of just
  updating. Only ever add new commits on top.
- Users only get an update when they click the button - there's no
  auto-check on launch. If a fix is urgent, tell them directly to click it.

## A real incident from testing this (read before you trust silent installs)

While verifying this script, an installer run launched with `/VERYSILENT`
still popped a real, visible `MsgBox` confirmation dialog on screen (Pascal
Script's `MsgBox` is a modal Win32 dialog - it is **not** suppressed by
`/VERYSILENT` on its own; that only hides the wizard UI). Because the test
was running unattended, nobody was expecting a dialog, and it got clicked
through - which deleted a real ~4GB Hugging Face model cache. Recoverable
(it just re-downloads on next use), but avoidable, and a sharp reminder that
a MsgBox left unguarded is a real hazard, not a cosmetic one.

Fixed by adding `IsSilentMode()` (parses `ParamStr()` directly for `/SILENT`
or `/VERYSILENT`, rather than trusting `WizardSilent()`/`UninstallSilent()`
alone) and guarding **every** `MsgBox` call in the script with it. If you
ever add a new `MsgBox`, guard it the same way.

**Because of this, live end-to-end testing of the compiled installer for
this task relied on compiling successfully + careful code review from this
point on, not further automated/silent runs of the interactive dialogs** -
the installer's dialogs are meant to be seen and answered by a real person,
which is the normal way to test one. Do a real interactive install (and a
real interactive uninstall, saying Yes at least once on a throwaway test
install - not your real one) yourself before shipping this to anyone else.
