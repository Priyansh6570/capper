"""Framer - browser-based manual frame-selection tool.

A small Flask app that lets you hand-bind each narration line of a recap script
to a frame you draw on the chapter strip, then export a mapping the EXISTING
pipeline turns into audio + video.

What it does:
  * /load_pdf   - stitch ALL PDF pages into one tall strip using the SAME code as
                  stages/s1_pdf_to_pages.py (we import and run stage 1's webtoon
                  path - PyMuPDF render + vertical concat + edge trim, with
                  Pillow's decompression-bomb guard lifted). Returns a downscaled
                  preview (canvas-safe) plus the scale factor so the browser can
                  map preview pixels back to TRUE strip pixels.
  * /load_script- split pasted text / a .txt file into ordered narration lines.
  * /export     - crop each hand-drawn bbox from the FULL-RES strip, horizontal-
                  flip + gaussian-blur as requested, and write:
                      work/<chapter>/frames/frame_NNN.png   (the rendered frames)
                      work/<chapter>/framer/mapping.json     (line/frame/duration)
                      work/<chapter>/manifest.json           (pipeline manifest)
                  The manifest's `beats` are populated and `stage` is set to
                  "clean" so the orchestrator runs ONLY audio + video next:
                      python orchestrator.py --chapter <chapter> --from audio

Run:  python tools/framer/app.py        (prints the localhost URL)

Self-contained under tools/framer/. The only reused project code is stage 1's
stitch (imported, not reimplemented) and the Manifest schema for the export.

Layout: this file builds the app and serves it; the HTTP routes are the blueprints in routes/
(one module per feature) and the logic behind them is in services/.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

# --- make the project root importable so we can REUSE stage 1 + the manifest --
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import waitress  # noqa: E402 - the server the app runs on, see make_server()
from flask import Flask, jsonify  # noqa: E402

import chapter_backup  # noqa: E402 - cleared chapters are restored on first use
import config  # noqa: E402 - module import so chapter_scope()/set_/clear_ are reachable
import projects  # noqa: E402 - the project system (this package: tools/framer/projects.py)
from routes import register_blueprints  # noqa: E402
from services.batching import resume_batches  # noqa: E402
from services.common import scope_params  # noqa: E402
from services.ports import resolve_port  # noqa: E402

app = Flask(__name__)
register_blueprints(app)


# Every route that touches chapter files redirects chapter_work_dir()/chapter_output_dir()
# (see config.py) to projects/<slug>/chapters/<n>/{work,output} for the duration of the request,
# by reading `project` + `ch_no` (see services.common.scope_params).
@app.before_request
def _apply_chapter_scope():
    slug, ch_no = scope_params()
    if slug and ch_no:
        try:
            config.set_chapter_scope(projects.chapter_work_dir(slug, ch_no),
                                      projects.chapter_output_dir(slug, ch_no))
        except chapter_backup.BackupUnavailable as e:
            return jsonify(error=str(e)), 503
    else:
        config.clear_chapter_scope()


@app.errorhandler(chapter_backup.BackupUnavailable)
def _backup_unavailable(e):
    """A chapter cleared to Drive that can't be read back: say so, instead of a bare server error."""
    return jsonify(error=str(e)), 503


@app.teardown_request
def _clear_chapter_scope(exc=None):
    config.clear_chapter_scope()


SERVER_THREADS = 32             # every open event stream (a render, a script, a download) holds one
SERVER_IDLE_TIMEOUT_S = 3600    # a stream may stay silent for a long time while Claude or the TTS works


def make_server(host: str, port: int):
    """The waitress server for the app; call .run() to serve. Port 0 picks a free port."""
    return waitress.create_server(app, host=host, port=port, threads=SERVER_THREADS,
                                  channel_timeout=SERVER_IDLE_TIMEOUT_S)


def main() -> None:
    host, port = "127.0.0.1", 5005
    port = resolve_port(host, port)
    if port is None:
        return   # a duplicate launch of an already-running instance - see resolve_port()
    url = f"http://{host}:{port}/"
    print("=" * 60)
    print("  ReCapper")
    print(f"  Open: {url}")
    print("=" * 60)
    # Python opens the browser (not start.bat) so it always points at the
    # PORT ACTUALLY USED, which resolve_port() may have changed. Delayed in
    # a background thread so it doesn't block app.run() below. Skipped after
    # a self-restart (see /api/restart, which sets this env var on the new
    # process) - the tab that triggered the restart already reloads itself
    # once this new instance answers, so opening another one would just be a
    # confusing duplicate. This is the ONLY webbrowser.open() call in the
    # entire app (or any dependency - checked), and it runs at most once per
    # process, so "opens a new tab on its own" can only mean a second
    # process started - which resolve_port() above now refuses to do.
    if not os.environ.pop("RECAPPER_SKIP_BROWSER_OPEN", None):
        threading.Thread(target=lambda: (time.sleep(1.0), webbrowser.open(url)),
                          daemon=True).start()
    resume_batches()
    # Threaded: a generate job holds an SSE connection open for minutes; other
    # requests (thumbnails, the result video, a second tab) must still be served.
    make_server(host, port).run()

if __name__ == "__main__":
    main()
