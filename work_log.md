# Work log

Framer fixes: editor persistence, upload progress, video flicker, "no background audio".

## Changes
- `tools/framer/app.py`
  - `/editor_state/load` now resolves state from disk (`_resolve_editor_state`): newest *populated* of `editor_state.json` / `recovery.json`, else rebuilt from `framer/mapping.json`.
  - `_video_url` (mtime-versioned) used by render jobs and by `/chapter_source_status`, which now returns `video_url` if `recap.mp4` exists.
- `tools/framer/index.html`
  - Autosave only writes when there are real edits (`dirty`), flushes on `pagehide`, uses `keepalive`.
  - Removed the empty "silent checkpoint" saves after download/stitch; export now saves editor state.
  - `openChapterEditor` just restores from disk (strip, script + frames, rendered video).
  - `showGenResult` is idempotent -> no more video blinking (was rebuilt with a new `Date.now()` URL every 1.5s job poll).
  - Asset upload uses XHR with a progress bar + % label, disabled Browse button, `beforeunload` warning; file counts as set only after the server responds.
  - Music setting: "No background audio" option.
- `tools/framer/projects.py`: music mode `none` validated; snapshot emits `music_off`.
- `stages/s7_assemble.py`: `_resolve_music` returns None when `music_off`.

## Next
Restart the app, open a boxed chapter, close and reopen it; try a large background upload and "No background audio" + Re-render video.
