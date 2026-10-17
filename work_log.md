# Work log - cleared chapters count as rendered

- Cause: after "Clear from this PC", status, the step list ("needs render"), the chapter length and the merge check only looked at this PC's disk.
- `projects._apply_derived_status`, `services/batching.chapter_steps`, `services/project_ops.add_chapter_details` now treat cleared kinds as present; the video length is remembered when the video is cleared (looked up once from the Drive copy for chapters cleared earlier).
- Merge (`routes/merge.py`) copies just the chapter videos back from Drive before merging; frames/audio stay cleared. Re-rendering a cleared chapter restores everything it needs first (already so).
- Tests added in `tests/test_chapter_backup.py`. Restart the app.
