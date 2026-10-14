# Work log

## Step chips through the modal, clean redo, default settings, Claude check

- **Chips / Retry -> modal** (`tools/framer/index.html`, `batch.py`, `app.py`): every step chip and the Retry button open the pipeline step modal with that step pre-ticked; the separate confirm and `/rerun` route are gone. `POST /batch` now also adds plans to a running batch (`Batch.enqueue`), so a failed chapter can be redone while others run.
- **Redo cleans up**: export deletes old frames and the old narration audio before cutting new frames (and refuses an empty export before deleting anything); audio (`s6_tts.py`) clears `audio/` and its temp voice file first; video (`s7_assemble.py`) deletes the old `recap.mp4` + plan first; download removes other files in `download/` once the new PDF is in; script reuses panels only if newer than the strip. `atomic_io.clear_directory` is the shared helper. Media library gains a "Panels" type; audio/frames types now cover the temp voice file and `thumb.jpg`.
- **Default settings**: `tools/framer/project_defaults.py`, tracked `defaults/settings.json` + `defaults/assets/background.mp4` (taken from "100-Year-Old Top Chef"; series brief excluded). New projects start from them. Project Settings has "Make these my default settings", "Apply default settings" and "Apply to all projects" (shows the changes, applies after confirm).
- **Claude CLI check**: `claude_cli.cli_status` messages now give plain steps; the pre-run check shows them (multi-line) before any run that includes the script step.

Next: restart the app (`start.bat`). Commit and push `defaults/` to share it through Check for updates.
