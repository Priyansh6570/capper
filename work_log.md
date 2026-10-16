# Work log

Slow project page, slow pipeline modal, refetch on returning from settings, and thousands of empty folders.

**Cause:** `GET /api/projects/<slug>` re-read and re-parsed the whole `project.json` once per chapter (564 times for the
mgeko project, 13 s), and every chapter got its detail computed and its folders created on each read.

**Backend (`projects.py`, `app.py`):**
- `projects.derive_all`: one load and at most one save for all chapters (was one per chapter).
- `GET /api/projects/<slug>?limit=N`: every chapter gets its stored fields and status, but the costly detail (tone,
  narration estimate, video length, steps, thumbnail) only for the first N. 13 s -> about 0.4 s.
- Path getters no longer create folders: `chapter_work_path` / `chapter_output_path` are pure, `chapter_work_dir` /
  `chapter_output_dir` create. `_framer_dir` and `_claude_state_path` are pure and the writers (upload, export, editor
  save, script job) create their folder with `_ensure_dir`. Reading the list now creates no folders; a chapter gets its
  folders only when something is written for it (download, editor save, pipeline).

**Frontend (`index.html`):**
- Chapter list shows 50 at a time with a "Show 50 more" button (`CHAPTER_PAGE`); select-all and range select cover the
  shown chapters.
- Pipeline modal opens at once with a spinner ("Checking each chapter's progress...") while the chapters load.
- Returning from Project settings reuses the loaded project (refreshes in the background only after 60 s). The batch
  poller no longer reloads the project on its first poll.

**Run next:** restart the app (done here). Empty folders from before can be removed with
`find projects/<slug>/chapters -depth -type d -empty -delete` (already done for the mgeko project).

**Ideas not done:** cache the per-project aggregates, store only touched chapters in `project.json`, pause polling on
hidden tabs, split `app.py`/`index.html`, production server, tests.
