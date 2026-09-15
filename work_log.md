# Work log

## Task: Framer "Generate Audio + Video" — run s6/s7 in-tool with live progress

Added in-tool generation to the Framer: after Export, run the pipeline's audio
(s6 TTS) and video (s7 assemble) stages for the chapter as a background job and
stream progress to the browser, then play/download the finished recap in-page —
no leaving the tool, no frozen request.

### Files changed
- `tools/framer/app.py` — SSE generate endpoint + output file server.
- `tools/framer/index.html` — generate buttons, progress bar, live log, result video.
- `tools/framer/README.md` — documented the new step 5.

### Backend (`app.py`)
- **`GET /generate_stream?chapter=<id>&what=both|video`** — runs
  `orchestrator.py --chapter <id> --from audio` (both: s6→s7) or `--from video`
  (video-only: s7) as a **subprocess** and **streams its stdout as Server-Sent
  Events**. Frames: `start` (carries `total_beats` for the bar), `log` (one per
  pipeline output line), `done` (`ok`, and `/output/<ch>/recap.mp4` when present),
  `error`. The child runs with `-u`/`PYTHONUNBUFFERED`/`PYTHONIOENCODING=utf-8` so
  lines stream immediately and never die on a unicode char; `stdin=DEVNULL` so it
  can't hang on a prompt.
  - One job per chapter (`_GEN_JOBS` + lock); a second request gets a clean
    "already running" error. Client disconnect / **Stop** raises `GeneratorExit`,
    which **terminates the subprocess**.
- **`GET /output/<chapter>/<name>`** — serves the finished artifact via
  `send_from_directory`, which honours **Range** requests so the inline `<video>`
  scrubs.
- `app.run(..., threaded=True)` so the long-lived SSE connection doesn't block
  thumbnails / the result video / a second tab.

### Frontend (`index.html`)
- New panel under Export: **🔊 Generate Audio + Video**, **🎬 Re-render Video**,
  **■ Stop**, a progress bar, a scrolling live log, and a result area.
- Opens an `EventSource`; appends every `log` line; drives the bar from the
  pipeline's own markers (`[NNN]` per voiced line for s6, `line N:` per rendered
  line for s7), weighted (audio ~70% / video ~22% for the combined run, all-video
  for re-render). The bar pulses **indeterminate** during the multi-minute TTS
  model load (before the first countable tick). On `done` it shows the recap
  inline (`<video controls>`) with a cache-busted **Download recap.mp4** link.
  Reconnect is suppressed after a normal end so the job doesn't restart.

### Verified (Flask test client, subprocess mocked — no heavy pipeline run)
- SSE emits start→logs→done; `start.total_beats` read from the manifest (53 for
  ch 01); `done.video` set when recap.mp4 exists.
- `what=video` invokes `--chapter <id> --from video`; `what=both` → `--from audio`.
- Concurrency guard returns "already running"; missing-manifest returns an error
  event.
- `/output/...recap.mp4` serves `video/mp4`, and a Range request returns **206**
  with `Accept-Ranges`/`Content-Range` (so in-page scrubbing works).

### Try it
```bash
python tools/framer/app.py        # http://127.0.0.1:5005/
# Stitch → script → box → Export, then click “Generate Audio + Video”.
# Watch the live log/bar; when done, play/download recap.mp4 inline.
```
