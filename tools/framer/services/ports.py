"""Finding the port to serve on and detecting an already running instance."""
from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.request

from config import NO_CONSOLE


def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    """True if something is already accepting connections on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _ping_existing(host: str, port: int, timeout: float = 1.0) -> dict | None:
    """If host:port is already serving THIS app (its /__app_ping__ marker),
    return that JSON body (app name + pid) - None if it's unreachable OR
    reachable but NOT ReCapper (some unrelated program happens to be on this
    port), so callers never assume they know what they'd be closing."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/__app_ping__", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data if data.get("app") == "recapper" else None
    except Exception:  # noqa: BLE001 - "can't confirm it's us" -> treat as unknown
        return None


def _find_free_port(host: str, start_port: int, tries: int = 50) -> int:
    for p in range(start_port, start_port + tries):
        if not _port_open(host, p, timeout=0.1):
            return p
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:  # last resort
        s.bind((host, 0))
        return s.getsockname()[1]


def resolve_port(host: str, port: int) -> int | None:
    """Never let a busy port 5005 fail silently with Werkzeug's raw "Address
    already in use" traceback (which looks like a crash to someone who just
    double-clicked start.bat) - AND never silently start a second, fully
    independent ReCapper server + open a second browser tab just because
    start.bat got launched again while one is already running (the actual
    cause of a real "it keeps opening new tabs on its own" bug report: every
    relaunch is a brand new process, so webbrowser.open() in main() - the
    ONLY place in this app or any of its dependencies that opens a browser,
    confirmed by grepping the whole venv - legitimately fires again each
    time, once per process, exactly as designed; the fix has to be "don't
    start a redundant process" rather than anything about the open() call
    itself). So: if the port is busy and it's confirmed to be OUR OWN
    instance (via /__app_ping__, not just an assumption), the default is to
    do NOTHING - no new server, no new tab, just point at the one already
    running - a relaunch can only ever open a SECOND tab if the person
    explicitly chooses to close the old instance first. Only when the port
    is occupied by some unrelated program does this fall back to the next
    free port (main() still opens exactly one browser tab in that case).
    """
    # A just-restarted instance (see /api/restart) may still be releasing the
    # port - give it a moment before treating it as genuinely occupied.
    for _ in range(20):
        if not _port_open(host, port):
            return port
        time.sleep(0.15)

    print()
    print("=" * 60)
    existing = _ping_existing(host, port)
    if existing:
        pid = existing.get("pid")
        print(f"  ReCapper is already running at http://{host}:{port}/"
              + (f" (PID {pid})" if pid else "") + ".")
        print("  [C] Close it and start fresh here")
        print("  [Enter] Do nothing - switch to that browser tab instead (default)")
        choice = ""
        try:
            choice = input("  Choice [C/Enter]: ").strip().lower()
        except (EOFError, OSError, AttributeError):
            # not an interactive console - either launched by a script, or
            # (since tray_launcher.py runs this under pythonw.exe with no
            # console at all) sys.stdin is None, whose .readline() raises
            # AttributeError rather than EOFError/OSError. Either way: default.
            pass
        if choice == "c" and pid:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                capture_output=True, timeout=10, **NO_CONSOLE)
                print(f"  Closed the previous instance (PID {pid}).")
                for _ in range(20):
                    if not _port_open(host, port):
                        print("=" * 60)
                        return port
                    time.sleep(0.15)
                print(f"  Port {port} is still busy - giving up on closing it.")
            except Exception as e:  # noqa: BLE001
                print(f"  Could not close it ({e}).")
        print(f"  Not starting a second copy. ReCapper is already open at "
              f"http://{host}:{port}/ - this window can be closed.")
        print("=" * 60)
        return None

    print(f"  Port {port} is already in use by another program (not ReCapper).")
    print("  Starting on the next free port instead.")
    new_port = _find_free_port(host, port + 1)
    print(f"  Using http://{host}:{new_port}/ instead.")
    print("=" * 60)
    return new_port
