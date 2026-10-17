"""Project-level operations behind the project routes: streaming a task, chapter details, thumbnails."""
from __future__ import annotations

import io
import queue
import threading
from pathlib import Path
from typing import Callable

from PIL import Image
from flask import Response, send_file

import branding
import chapter_backup
import claude_engine
import projects
import totals
from services.batching import chapter_steps, thumb_source
from services.common import sse
from services.editor_state import narration_estimate


def stream_task(task: Callable[[Callable[[dict], None], threading.Event], dict]) -> Response:
    """Run `task(emit, stop)` on a thread and stream what it emits as SSE frames, ending with a `done` frame
    carrying its result or an `error` frame. `stop` is set when the client goes away."""
    events: queue.Queue = queue.Queue()
    stop = threading.Event()

    def work() -> None:
        try:
            events.put({"type": "done", **task(events.put, stop)})
        except Exception as e:  # noqa: BLE001 - the UI shows the message
            events.put({"type": "error", "message": str(e)})
        events.put(None)

    def stream():
        threading.Thread(target=work, daemon=True).start()
        try:
            while (event := events.get()) is not None:
                yield sse(event)
        finally:
            stop.set()

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def add_chapter_details(project: dict, chapters: list[dict]) -> None:
    slug = project["slug"]
    chars_per_sec = totals.speech_rate(slug) or claude_engine.DEFAULT_CHARS_PER_SEC
    for ch in chapters:
        ch["effective_tone"] = projects.chapter_tone(project, ch["n"])
        ch["narration"] = narration_estimate(slug, ch["n"], chars_per_sec)
        video = projects.chapter_video_path(slug, ch["n"])
        ch["duration"] = (branding.duration_of(video) if video.is_file()
                          else chapter_backup.cached_video_duration(slug, ch["n"]))
        ch["steps"] = chapter_steps(slug, ch["n"])
        ch["has_thumb"] = thumb_source(slug, ch["n"]) is not None


# --------------------------------------------------------------------------- #
# settings live previews - built with the SAME s7 compose/watermark helpers
# used by the real render, so the preview is WYSIWYG by construction. Settings
# are read from QUERY PARAMS (the in-progress form state), except an
# image file, which must already be uploaded (see /asset above) since a
# multi-megabyte file isn't practical to pass on every debounced preview tick.
# --------------------------------------------------------------------------- #
def placeholder_frame(slug: str) -> Path:
    """Any already-exported frame for this project to preview settings over,
    else a simple generated character silhouette, cached on first use."""
    chapters_dir = projects.local_dir(slug) / "chapters"
    if chapters_dir.is_dir():
        for frame in sorted(chapters_dir.glob("*/work/frames/frame_*.png")):
            return frame
    placeholder = projects.assets_dir(slug) / "_sample_placeholder.png"
    if not placeholder.exists():
        from PIL import ImageDraw
        img = Image.new("RGB", (700, 1000), (58, 66, 86))
        d = ImageDraw.Draw(img)
        d.ellipse((210, 110, 490, 410), fill=(214, 196, 180))
        d.rounded_rectangle((150, 420, 550, 900), radius=48, fill=(96, 108, 140))
        img.save(placeholder)
    return placeholder


def png_response(img) -> Response:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp
