# Work log

## Fixed: app randomly dies mid-use (native Tcl/Tk crash in the splash screen kills the whole process)

### Investigation

User reported: fetching a kingofshojo.com series got a "network error" toast,
and after that the whole app was unreachable ("refresh won't load"). Browser
console showed `NS_ERROR_NET_RESET` on the in-flight request, then
`NS_ERROR_CONNECTION_REFUSED` on every request after - meaning the Flask
server process itself died mid-request, not just one request failing.

This is the same physical machine as the earlier installer investigation, so
I inspected it directly instead of guessing:

- `tools/framer/sites/kingofshojo.py`'s `fetch_series()` for the exact
  reported URL ran fine standalone (under 1s, 388 chapters found) - not the
  cause.
- `app_log.txt` (tray_launcher.py's stdout/stderr redirect) showed the
  process restarting ("* Serving Flask app 'app'") repeatedly, roughly every
  1-2 minutes, with **zero Python tracebacks** for any of those restarts -
  ruling out an unhandled exception in the app's own code (Flask/Werkzeug
  with `debug=False, threaded=True` catches route exceptions and returns 500;
  it can't make the whole process vanish).
- Windows' own Application event log told the real story: repeated
  `APPCRASH` entries for `pythonw.exe`, faulting module **`tcl86t.dll`**
  (Tcl/Tk - the library behind Python's `tkinter`), exception code
  `0x80000003` (a native trap/abort, not a normal Python exception - nothing
  in Python can catch this).

Root cause: `tools/framer/tray_launcher.py` runs the animated startup splash
(`splash.py`, a Tkinter window) **in the same process, on the main thread**,
while the Flask server runs in a **daemon thread** of that same process. In
Python, an OS-level crash of the main thread takes the whole process down
immediately, killing all daemon threads with it - so a native Tcl/Tk crash in
the splash screen (which `show_and_wait()`'s `except Exception` can't catch,
since the crash happens below Python's exception machinery entirely) was
taking the running Flask server down with it. The user's kingofshojo fetch
was mid-flight when one of these happened, purely by timing - not caused by
the kingofshojo code itself.

### Fix

`tools/framer/splash.py` now also runs as a standalone script (`python
splash.py <host> <port> <logo_path> <min_s> <max_s>`). `tray_launcher.py`'s
`_show_splash_and_wait()` now spawns it as a **separate subprocess**
(`subprocess.run(..., timeout=SPLASH_MAX_S + 2)`) instead of importing and
running it in-process. If that subprocess crashes, hangs, or misbehaves in
any way, only the subprocess dies - the parent process (running the actual
Flask server) is a completely separate OS process and is unaffected. This
is what actually delivers on `splash.py`'s own pre-existing promise ("never
raises - a splash that fails must not block the app itself from starting"),
which a Python-level `try/except` alone can't guarantee against a native
crash.

### Verified

- Copied both changed files onto the actual affected install and ran the
  real launch path (`wscript.exe start_hidden.vbs`, exactly what the desktop
  shortcut invokes) several times.
- Confirmed the splash subprocess starts, runs its animation, and exits
  cleanly on its own (~8s) every time.
- Confirmed the Flask server (parent process) stays up and keeps responding
  to `/__app_ping__` throughout and after the splash subprocess's lifetime.
- All test processes cleaned up afterward.

### Secondary finding (not fixed, noted for later)

While testing repeated relaunches, hit a narrow, pre-existing race in
`app.py`'s `_resolve_port()`: two near-simultaneous launches can both pass
its "is the port open" check before either has actually bound it, so the
loser's Flask thread dies silently on a bind error while its tray icon
survives as a harmless but confusing orphan. Requires two launches within
the same ~150ms-3s window to trigger (e.g. rapid double-launch during
troubleshooting) - not the cause of this bug, but worth a proper fix (a lock
file/mutex instead of racing on the TCP port) if it comes up again.

### Files changed

- `tools/framer/splash.py` - added a `__main__` CLI entry point; updated
  module docstring to explain the subprocess-isolation design.
- `tools/framer/tray_launcher.py` - `_show_splash_and_wait()` now spawns
  `splash.py` as a subprocess instead of importing it in-process; added
  `import subprocess`.

### Next steps for the user

Re-run `setup.bat`? No - this doesn't need a package reinstall, just the
updated code (already deployed to the affected install; a normal "Check for
updates" picks it up elsewhere). Just use the app normally - if it was going
to hit the Tcl/Tk crash again, it now can't take the server down with it.
