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

Run:  pythonw.exe tools/framer/tray_launcher.py   (see start.bat)
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Must happen before importing app.py (or Flask/Werkzeug, which log to
# sys.stderr) - see the module docstring.
if sys.stdout is None or sys.stderr is None:
    log = open(ROOT / "app_log.txt", "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = log
    sys.stderr = log

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app as framer_app  # noqa: E402 - see sys.path insert above

import pystray  # noqa: E402
from PIL import Image  # noqa: E402

HOST, PORT = "127.0.0.1", 5005


def _tray_image() -> Image.Image:
    icon_path = ROOT / "assets" / "icon.ico"
    if icon_path.exists():
        try:
            return Image.open(icon_path).convert("RGBA")
        except Exception:  # noqa: BLE001 - fall through to the plain fallback below
            pass
    return Image.new("RGBA", (64, 64), (40, 40, 40, 255))


SPLASH_MIN_S = 0.8   # never flash-and-vanish even on a fast machine/warm cache
SPLASH_MAX_S = 8.0   # never block the browser opening on a stuck/slow server


def _show_splash_and_wait(host: str, port: int) -> None:
    """A small borderless "app is loading" window (logo + status), centered
    on screen - the gap between double-clicking the shortcut and the browser
    tab actually showing something used to be blank silence (no console, no
    window at all); this fills it, the way a desktop app's own splash screen
    would, instead of just opening the browser to a "can't connect" page for
    a moment. Polls the server's own port (not an HTTP request - cheaper, and
    "the port is accepting connections" is all that matters here) and closes
    itself once it's up, with a floor (SPLASH_MIN_S, so it never just flashes
    on a warm/fast start) and a ceiling (SPLASH_MAX_S, so a slow or wedged
    server never leaves this on screen forever - the browser still opens
    either way once this returns).

    Runs entirely on the calling (main) thread and returns before anything
    else touches Tkinter - main() only starts pystray's OWN main-thread loop
    (icon.run()) after this function has returned, so the two never overlap.

    Never raises: tkinter/Tk being unavailable or misbehaving must not block
    the app itself from starting, only skip the nicety.
    """
    try:
        import tkinter as tk
        from PIL import Image, ImageTk
    except Exception:  # noqa: BLE001
        return

    try:
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        bg = "#0d0d0f"
        root.configure(bg=bg)
        w, h = 360, 220
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

        logo_path = ROOT / "assets" / "ReCapper.png"
        if logo_path.exists():
            img = Image.open(logo_path).convert("RGBA")
            img.thumbnail((96, 96), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            logo_label = tk.Label(root, image=photo, bg=bg)
            logo_label.image = photo  # keep a reference - Tk drops it otherwise
            logo_label.pack(pady=(26, 8))

        tk.Label(root, text="ReCapper", fg="#f5f5f5", bg=bg,
                 font=("Segoe UI", 15, "bold")).pack()
        status = tk.Label(root, text="Loading...", fg="#9a9a9a", bg=bg,
                           font=("Segoe UI", 10))
        status.pack(pady=(6, 0))
    except Exception:  # noqa: BLE001
        return

    start = time.monotonic()
    dots = ["Loading", "Loading.", "Loading..", "Loading..."]

    def _port_open() -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            return False

    def tick(i: int) -> None:
        elapsed = time.monotonic() - start
        try:
            status.configure(text=dots[i % len(dots)])
        except tk.TclError:
            return  # window already destroyed
        ready = _port_open()
        if (ready and elapsed >= SPLASH_MIN_S) or elapsed >= SPLASH_MAX_S:
            root.destroy()
            return
        root.after(180, tick, i + 1)

    root.after(180, tick, 0)
    try:
        root.mainloop()
    except Exception:  # noqa: BLE001
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


def main() -> None:
    port = framer_app._resolve_port(HOST, PORT)
    if port is None:
        return  # another instance is already running - see _resolve_port()
    url = f"http://{HOST}:{port}/"

    server_thread = threading.Thread(
        target=lambda: framer_app.app.run(host=HOST, port=port, debug=False, threaded=True),
        daemon=True,
    )
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
    main()
