# Work log - Back up to Drive

- New `tools/framer/chapter_backup.py`: per chapter, kinds = script & key mappings (always local), frames, audio, page images (off by default, large), chapter video. Copies into `<project folder>/chapters/<n>/{work,output}` (Drive) through `.part` files; backed up = every local file has a same-size copy; `backup.json` records when.
- Clear: deletes only verified, clearable kinds from this PC and writes `work/.cleared.json`. Restore: `projects.chapter_work_dir/chapter_output_dir` call `chapter_backup.ensure_local`, so any route/job that uses a cleared chapter copies it back first. If Drive isn't reachable the request answers 503 with a clear message (`app.py` before_request).
- `routes/backup.py`: GET `/api/projects/<slug>/backup`, GET `backup_stream` (SSE), POST `backup_clear`, POST `chapters/<n>/restore`. Chapters in a live batch are never cleared.
- UI: "Back up to Drive" button in the chapters toolbar -> dialog (kinds, chapters, merged-videos note, progress, Free up space). `index.html`, `static/js/app.js`, `static/css/app.css`.
- Only for projects stored in the Drive folder; local-only projects see an explanation.
- Tests: `tests/test_chapter_backup.py`. No Google web fallback (G: path only), as chosen.
- Not checked visually (browser extension was disconnected); restart the app and hard-refresh.
