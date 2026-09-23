# Work log

## Task: fix ReCapper repeatedly opening a new browser tab on its own

### What was actually causing it

Not the reloader, not a watchdog/timer, not a crash-loop - all three of the
user's suggested culprits were checked and ruled out directly:

- **Flask reloader**: `app.run(..., debug=False)` (no `use_reloader` param,
  so it defaults to `debug`'s value = off). Verified empirically - started
  the real server, touched `tools/framer/app.py` on disk, no restart, no
  "Restarting with stat" message, log confirms "Debug mode: off" throughout.
- **A periodic timer**: grepped `tools/framer/` for every `setInterval`/
  `setTimeout` - the only ones are the 1.5s job-status poll, the 30s
  editor-state autosave, and a couple of debounces. None of them touch the
  browser or the server process.
- **Something else opening a browser**: grepped the ENTIRE `.venv` (every
  installed dependency - torch, chatterbox-tts, moviepy, edge-tts, gradio,
  click, typer, matplotlib, all of them) for `webbrowser.open`/`os.startfile`.
  A few packages (gradio, click, typer, matplotlib's webagg backend) HAVE
  browser-launch utilities available, but none of them are ever called -
  confirmed nothing in this project's own code (`app.py`, any `stages/*.py`,
  `orchestrator.py`) imports or invokes any of them.

That leaves exactly ONE line in the entire application + dependency tree
that can open a browser: `webbrowser.open(url)` inside `main()`
(`tools/framer/app.py`), called at most once per process
(`if __name__ == "__main__": main()`, no retry/relaunch wrapper anywhere).

**So the real question was "what's starting the process more than once,"**
not "what's reopening the tab." And the answer was a regression from this
session's own EARLIER port-conflict fix: `_resolve_port()` was written so
that if port 5005 is busy with ReCapper's own already-running instance, the
DEFAULT (including whenever start.bat's console isn't interactive) was to
"start this copy on a different port instead" - which silently launches a
second, fully independent server AND opens a second browser tab pointed at
it. Every time `start.bat`/the desktop shortcut gets launched again while
ReCapper is already running - a stray double-click, clicking the shortcut
again later without noticing the console window from before, anything -
that produces exactly the reported symptom: a new tab appearing "on its
own," with no crash, no error, nothing obviously wrong, because the old
fallback behavior was specifically designed to succeed quietly.

### The fix

`tools/framer/app.py`, `_resolve_port()`: when the port is confirmed busy
with ReCapper's OWN instance (via the existing `/__app_ping__` identity
check), the default is now to do **nothing** - print that it's already
running and exactly where, and return `None`. `main()` checks for `None`
and returns immediately, before starting Flask or scheduling the
`webbrowser.open()` call at all. A duplicate launch can now only ever
produce a second server+tab if the person explicitly answers `[C]` to close
the old instance first - never silently, never as a side effect of the port
merely being busy. The "occupied by some unrelated, non-ReCapper program"
fallback path (start on the next free port) is unchanged - that's a
genuinely different situation with no existing instance to point back to.

### Verified

- Started a real instance (instance A), then launched a second `app.py`
  process (instance B) exactly as a duplicate start.bat launch would:
  instance B correctly identified instance A by PID via `/__app_ping__`,
  printed "Not starting a second copy... this window can be closed.", and
  exited cleanly (code 0) WITHOUT calling `app.run()` or opening any
  browser tab. Confirmed instance A was completely unaffected afterward
  (same PID, still the sole owner of port 5005) - no interference, no
  duplicate server.
- The unrelated-program fallback branch's code was not touched by this
  change (only the "it's our own instance" branch changed).
- All test processes cleaned up; port 5005 confirmed free afterward.

Also fixed a stale comment in `index.html` left over from the port-conflict
task's OWN earlier iteration (said `/api/restart` "os.execv()s itself" -
that was the first attempt, replaced after it proved unreliable on Windows;
the comment never got updated to match).

### Files changed

- `tools/framer/app.py` (`_resolve_port`, `main()`)
- `tools/framer/index.html` (stale comment only, no behavior change)

### Aside (not part of this fix, flagging only)

Noticed a `.env` file at the project root with what look like live
`GEMINI_API_KEY`/`GROQ_API_KEY` values. It's correctly gitignored and has
never been committed (checked `git log` across all history) - no exposure,
just mentioning it exists in case it's not the key rotation the user
expects to still be using.

### Next steps for the user

- No new dependencies. Restart the app once to pick this up - after that,
  launching start.bat again while ReCapper is already running will no
  longer open a second tab; it'll just tell you it's already running and
  exit.
