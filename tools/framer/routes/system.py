"""Routes for GPU status, dependencies, updates and restart."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

from flask import Blueprint, Response, jsonify

import gpu
from config import NO_CONSOLE, ROOT
from services.common import sse
from services.system import _missing_packages, install_deps_stream, system_tool_problems

bp = Blueprint("system", __name__)


@bp.get("/api/gpu/status")
def api_gpu_status():
    return jsonify(gpu.status())


@bp.get("/api/deps/check")
def api_deps_check():
    return jsonify(missing=_missing_packages(), tools=system_tool_problems())


@bp.get("/api/deps/install_stream")
def api_deps_install_stream():
    def stream():
        try:
            for item in install_deps_stream():
                if item["type"] == "result":
                    yield sse({"type": "done", "ok": not item["missing"],
                                "missing": item["missing"], "cuda_ok": item["cuda_ok"]})
                else:
                    yield sse(item)
        except GeneratorExit:
            raise
        except Exception as e:  # noqa: BLE001 - last-resort: still report, don't crash the stream
            yield sse({"type": "error", "message": f"{type(e).__name__}: {e}"})

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- #
# in-app update - "Check for updates" pulls the app's own CODE from git.
# Deliberately just `git pull --ff-only` in ROOT: nothing here touches .venv,
# installed packages, or the Hugging Face model cache - those all live
# outside what git tracks (see .gitignore), so a pull can never reach them.
# GIT_TERMINAL_PROMPT=0 makes a missing/expired credential fail fast with a
# clear error instead of hanging the request on an invisible auth prompt.
# Every success path also reports currently-missing packages (see
# _missing_packages above) - a pull can leave the CODE ahead of the venv's
# installed packages, and this is the only place that would ever notice.
#
# Tracked code files are never meant to be user-edited, and nothing in this
# app writes to them - so a local modification to one is always checkout
# noise (most likely a previous pull that got interrupted mid-checkout, e.g.
# the PC slept/lost power/was force-closed while git was writing files - a
# `git pull --ff-only` can't tell that apart from a real edit and refuses
# with "local changes would be overwritten by merge", which then blocks
# EVERY future update on that machine forever until someone runs `git reset
# --hard` by hand. `git reset --hard HEAD` before every pull (below) makes
# this self-healing: it only touches TRACKED files, matching them back to
# HEAD - it can't reach projects/, work/, output/, .env, or anything else
# under this app's folder, all of which are untracked/gitignored.
# --------------------------------------------------------------------------- #
@bp.post("/api/update")
def api_update():
    if not (ROOT / ".git").exists():
        return jsonify(ok=False, not_git=True,
                        message="This install wasn't set up with git, so in-app "
                                "updates aren't available here. Reinstall with "
                                "the latest installer to get this feature.")

    def run_git(*args):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        return subprocess.run(["git", *args], cwd=str(ROOT), env=env,
                               capture_output=True, text=True, timeout=120, **NO_CONSOLE)

    try:
        old = run_git("rev-parse", "HEAD")
        if old.returncode != 0 and "dubious ownership" in (old.stderr or "").lower():
            # Windows: git refuses to operate on a repo whose folder ownership
            # looks unexpected (seen on some drives/installs, e.g. an install
            # under G:\...) - self-heal by trusting this exact folder for this
            # user, then retry once. Reactive (not run on every request) so it
            # never bloats the user's global .gitconfig on the happy path.
            subprocess.run(["git", "config", "--global", "--add", "safe.directory",
                             str(ROOT).replace("\\", "/")],
                            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
                            capture_output=True, text=True, timeout=30, **NO_CONSOLE)
            old = run_git("rev-parse", "HEAD")
        if old.returncode != 0:
            return jsonify(ok=False, message="Could not read the current version: "
                                              + (old.stderr or old.stdout).strip())
        old_hash = old.stdout.strip()

        reset = run_git("reset", "--hard", "HEAD")
        if reset.returncode != 0:
            return jsonify(ok=False, message="Could not prepare the update: "
                                              + (reset.stderr or reset.stdout).strip())

        pull = run_git("pull", "--ff-only")
        if pull.returncode != 0:
            return jsonify(ok=False, message="Update failed: "
                                              + (pull.stderr or pull.stdout).strip())

        new = run_git("rev-parse", "HEAD")
        new_hash = new.stdout.strip() if new.returncode == 0 else old_hash

        if new_hash == old_hash:
            return jsonify(ok=True, already_up_to_date=True, files_changed=0,
                            message="Already up to date.",
                            missing_packages=_missing_packages(), system_tools=system_tool_problems())

        diff = run_git("diff", "--name-only", old_hash, new_hash)
        changed = [ln for ln in (diff.stdout or "").splitlines() if ln.strip()]
        missing = _missing_packages()

        if missing:
            message = (f"Updated {len(changed)} file(s). {len(missing)} required "
                       f"package(s) aren't installed yet - see below.")
        else:
            message = f"Updated {len(changed)} file(s). Restart the app to apply."

        return jsonify(ok=True, already_up_to_date=False, files_changed=len(changed),
                        message=message, missing_packages=missing, system_tools=system_tool_problems())
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, message="Update timed out - check your internet "
                                          "connection and try again.")
    except FileNotFoundError:
        return jsonify(ok=False, message="git is not installed or not on PATH.")
    except Exception as e:  # noqa: BLE001 - report, don't crash the app
        return jsonify(ok=False, message=f"{type(e).__name__}: {e}")


@bp.post("/api/restart")
def api_restart():
    """Cleanly restart this server process - what the "Restart now" button
    (shown after a successful "Check for updates") uses instead of asking
    the user to close and reopen start.bat themselves.

    NOT os.execv(): tried it first, but on Windows it's emulated (there's no
    real exec()) via spawn-then-exit, and called from a background thread
    while the main thread sits in Werkzeug's blocking accept() loop, it left
    the OLD process alive-but-unresponsive (still holding the port, no
    longer answering requests) instead of actually being replaced -
    confirmed empirically, not theoretical. Spawning a genuinely separate
    process and then hard-exiting this one is the reliable version of the
    same idea: no special creationflags, so the new process inherits this
    console (same window, Ctrl+C/close still works) and keeps running once
    this one exits. The new process's own startup (resolve_port) waits out
    the brief gap until this one actually releases the port. The frontend
    polls /__app_ping__ until the new process answers, then reloads.
    """
    def _do_restart():
        time.sleep(0.3)  # let this response flush before we spawn + exit
        env = dict(os.environ, RECAPPER_SKIP_BROWSER_OPEN="1")
        subprocess.Popen([sys.executable] + sys.argv, cwd=str(ROOT), env=env)
        os._exit(0)  # hard exit: no WSGI response to finish, no cleanup needed
    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify(ok=True)
