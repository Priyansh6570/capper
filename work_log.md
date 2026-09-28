# Work log

## Fixed: "Check for updates" failing with a raw git error on locally-modified tracked files

### Root cause investigation

Confirmed the app itself never writes to its own tracked source files - grepped
every write (`open(..., 'w')`/`atomic_write_json`) in the codebase; the only
code that touches `requirements.txt` (`_requirement_specs()`) opens it
read-only, and there's no other self-modifying pattern anywhere.

So a tracked file (`config.py`, `app.py`, `requirements.txt`, etc.) showing as
locally modified on an installed machine, when nobody ever edited it, can only
come from git's own checkout getting interrupted or made inconsistent -
**not** from anything the app does on purpose:

- **Most likely**: a previous "Check for updates" `git pull` got interrupted
  mid-checkout - the PC slept, lost power, or the app was force-closed while
  git was writing files. `git pull --ff-only` has no way to distinguish that
  from a real edit, so it refuses with exactly the error reported ("local
  changes would be overwritten by merge... commit or stash... Aborting") -
  and since this app **only** ever calls `git pull --ff-only` with no
  recovery, that failure is then PERMANENT: every subsequent "Check for
  updates" on that machine hits the same wall forever, which is exactly why
  previously pushed fixes were never reaching some machines.
- **Contributing factor**: this repo had no `.gitattributes`, so Windows
  checkouts followed whatever the user's own global `core.autocrlf` happened
  to be (commonly `true`). That doesn't by itself make a fresh checkout look
  modified, but it removes the one safety net (consistent line endings) that
  would otherwise mask checkout noise from an interrupted pull instead of
  turning it into a real diff.

Verified the fix and this diagnosis empirically with an isolated scratch git
repo (bare "origin" + a "clone" simulating an installed machine): reproduced
the exact reported error by locally editing a tracked file then pulling a
real incoming commit, then confirmed the fix resolves it.

### Fix

`tools/framer/app.py`, `/api/update`: runs `git reset --hard HEAD` right
after reading the current commit, unconditionally, before every
`git pull --ff-only`. Tracked code files are never meant to be user-edited,
so there's nothing to lose by always discarding local modifications to them
before updating - `reset --hard` only touches tracked files, so it can never
reach `projects/`, `work/`, `output/`, `.env`, or anything else under the
app's folder (all untracked/gitignored). Verified directly (see below) that a
local edit to a tracked file is discarded while an untracked file survives
untouched.

Added `.gitattributes` (`* text=auto eol=lf`) at the repo root so checkouts
use LF consistently regardless of a machine's global `core.autocrlf`,
removing line endings as a source of checkout drift going forward.

### Verified

- Reproduced the user's exact error message in an isolated scratch repo (dirty
  tracked file + a real incoming commit -> `git pull --ff-only` fails with
  "local changes would be overwritten by merge... Aborting").
- Confirmed `git reset --hard HEAD` discards ONLY the tracked-file
  modification; an untracked file in a gitignored dir (`work/`) and a random
  untracked file (`.env`-like) both survive untouched.
- Hit the real `/api/update` Flask route (via its test client, `ROOT`
  monkeypatched to the scratch "installed machine" clone) with a locally
  corrupted `app.py` and a real pushed update waiting on origin: got back a
  normal `{"ok": true, "message": "Updated 1 file(s)..."}` - never the raw git
  error - and the file on disk ended up exactly at the real update's content.
- All scratch repos/test files cleaned up afterward; nothing left in this
  repo's working tree beyond the intended fix.

### Files changed

- `tools/framer/app.py` - `git reset --hard HEAD` before every pull in
  `/api/update`, plus a comment explaining why.
- `.gitattributes` (new) - `* text=auto eol=lf`.

### Next steps for the user

None - existing installs self-heal on their next "Check for updates" (the
very next click discards whatever was locally wrong and pulls cleanly). No
user data is at risk.
