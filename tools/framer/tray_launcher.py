"""Runs Framer with no console window, plus a system tray icon.

start.bat launches THIS (not app.py directly) with pythonw.exe, which has no
console subsystem at all - so double-clicking the desktop shortcut opens only
the browser, never a black window. That has one consequence this file exists
to handle: with no console to close or Ctrl+C, there needs to be some other
clean way to stop the app - the tray icon's "Quit ReCapper" is it.

It also has one non-obvious side effect: under pythonw.exe with no console and
no redirected stdio, `sys.stdout`/`sys.stderr` are `None` (documented Python
behavior), so ANY bare `print()` anywhere in the app (there are dozens, across
app.py and the stage modules) would crash with AttributeError the first time
it ran. Redirecting them to a log file below - BEFORE importing app.py or its
dependencies - fixes that at the source instead of hunting down every print().

Any failure to start - a missing package, a busy port, an unwritable folder - is written to
app_log.txt in full and shown in a Windows error dialog, because pythonw has no console to show it.

Run:  pythonw.exe tools/framer/tray_launcher.py   (see start.bat)
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = ROOT / "app_log.txt"
MB_ICONERROR, MB_TOPMOST = 0x10, 0x40000


def _explain(exc: BaseException) -> str:
    if isinstance(exc, ModuleNotFoundError):
        return (f"A component the app needs ('{exc.name}') is missing from this install.\n\n"
                "To fix it: open the ReCapper folder and double-click setup.bat. It is safe to run again "
                "and only installs what is missing. Then start ReCapper again.")
    if isinstance(exc, OSError):
        return ("ReCapper could not open its local web page, or could not read or write a file it needs.\n\n"
                "To fix it: close any other copy of ReCapper (check the system tray), make sure the "
                "ReCapper folder is not read-only, then start ReCapper again.")
    return ("ReCapper hit an unexpected error while starting.\n\n"
            "To fix it: start ReCapper again. If it keeps failing, run setup.bat in the ReCapper folder.")


def _write_failure_log(exc: BaseException) -> Path:
    """Appends the full traceback to app_log.txt, or to the temp folder when the app folder is not writable."""
    entry = (f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} ReCapper failed to start =====\n"
             + "".join(traceback.format_exception(exc)))
    for path in (LOG_PATH, Path(tempfile.gettempdir()) / "recapper_startup_error.txt"):
        try:
            with open(path, "a", encoding="utf-8") as log:
                log.write(entry)
            return path
        except OSError:
            continue
    return LOG_PATH


def _fail_startup(exc: BaseException) -> None:
    """Logs the failure, tells the user in plain language, and ends the process from any thread."""
    log_path = _write_failure_log(exc)
    ctypes.windll.user32.MessageBoxW(
        None,
        f"{_explain(exc)}\n\nWhat went wrong: {type(exc).__name__}: {exc}\n\nFull details are in:\n{log_path}",
        "ReCapper could not start", MB_ICONERROR | MB_TOPMOST)
    os._exit(1)


try:
    # Must happen before importing app.py (or Flask/Werkzeug, which log to
    # sys.stderr) - see the module docstring.
    if sys.stdout is None or sys.stderr is None:
        log = open(LOG_PATH, "a", buffering=1, encoding="utf-8", errors="replace")
        sys.stdout = log
        sys.stderr = log

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import app as framer_app  # noqa: E402 - see sys.path insert above
    import pystray  # noqa: E402
    from PIL import Image  # noqa: E402
except Exception as exc:  # noqa: BLE001 - any import failure must reach the user, not vanish under pythonw
    _fail_startup(exc)

HOST, PORT = "127.0.0.1", 5005


def _tray_image() -> Image.Image:
    icon_path = ROOT / "assets" / "icon.ico"
    if icon_path.exists():
        try:
            return Image.open(icon_path).convert("RGBA")
        except Exception:  # noqa: BLE001 - fall through to the plain fallback below
            pass
    return Image.new("RGBA", (64, 64), (40, 40, 40, 255))


SPLASH_MIN_S = 3.0   # keep the animated splash up long enough to play through, even on a fast/warm start
SPLASH_MAX_S = 8.0   # never block the browser opening on a stuck/slow server


def _show_splash_and_wait(host: str, port: int) -> None:
    """The animated ReCapper splash (see splash.py) - the gap between
    double-clicking the shortcut and the browser tab actually showing
    something used to be blank silence (no console, no window at all); this
    fills it, the way a desktop app's own splash screen would, instead of
    just opening the browser to a "can't connect" page for a moment.

    Runs splash.py as a SEPARATE PROCESS, not imported in-process - tkinter/
    Tcl can crash natively (observed: Windows Error Reporting APPCRASH in
    tcl86t.dll), which kills the whole process with no Python exception to
    catch, and this process is also running the Flask server (see main()
    below) - a splash crash used to take the server down with it. A
    subprocess crashing only kills that subprocess.

    Blocks until the child exits (it waits out the same
    SPLASH_MIN_S/SPLASH_MAX_S window internally - see splash.py) or
    SPLASH_MAX_S+2s at the latest, so a wedged/crashed child can never block
    the browser from opening. Never raises: splash.py being unavailable,
    crashing, or misbehaving must not block the app itself from starting,
    only skip the nicety.
    """
    try:
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "splash.py"),
             host, str(port), str(ROOT / "assets" / "ReCapper.png"),
             str(SPLASH_MIN_S), str(SPLASH_MAX_S)],
            timeout=SPLASH_MAX_S + 2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 - a broken/crashed/hung splash must never block startup
        pass


def _stop_active_processes() -> None:
    """Best-effort: terminate any render/merge subprocess still running so
    Quit actually stops the app's work, not just this process's own window
    (which never existed to begin with)."""
    try:
        with framer_app._JOBS_LOCK:
            jobs = list(framer_app._JOBS.values())
        for job in jobs:
            proc = job.proc
            if proc is not None and proc.poll() is None:
                proc.terminate()
    except Exception:  # noqa: BLE001 - quitting must never hang or crash
        pass
    try:
        for proc in list(framer_app._MERGE_JOBS.values()):
            if proc is not None and proc.poll() is None:
                proc.terminate()
    except Exception:  # noqa: BLE001
        pass


def _serve(port: int) -> None:
    try:
        framer_app.app.run(host=HOST, port=port, debug=False, threaded=True)
    except Exception as exc:  # noqa: BLE001
        _fail_startup(exc)


def main() -> None:
    port = framer_app._resolve_port(HOST, PORT)
    if port is None:
        return  # another instance is already running - see _resolve_port()
    url = f"http://{HOST}:{port}/"

    framer_app._resume_batches()
    server_thread = threading.Thread(target=_serve, args=(port,), daemon=True)
    server_thread.start()

    # Mirrors app.py's own main(): skipped after a self-restart (/api/restart
    # already opened the original tab; a second open() here would just be a
    # confusing duplicate) - so no splash on a restart either, it's a fast
    # in-place swap, not a fresh launch.
    if not os.environ.pop("RECAPPER_SKIP_BROWSER_OPEN", None):
        _show_splash_and_wait(HOST, port)  # blocks (main thread) until the
                                            # server's port is up, or times out
        webbrowser.open(url)

    def _open(icon, item):  # noqa: ANN001 - pystray callback signature
        webbrowser.open(url)

    def _quit(icon, item):  # noqa: ANN001 - pystray callback signature
        _stop_active_processes()
        icon.stop()
        os._exit(0)  # hard exit: no console/graceful-shutdown story needed here

    icon = pystray.Icon(
        "ReCapper",
        _tray_image(),
        "ReCapper",
        menu=pystray.Menu(
            pystray.MenuItem("Open ReCapper", _open, default=True),
            pystray.MenuItem("Quit ReCapper", _quit),
        ),
    )
    icon.run()  # blocks the main thread - required on Windows for the tray


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        _fail_startup(exc)
