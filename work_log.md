# Work log

## Chapter screen, semi-dark theme, pipeline step modal, step tracker

**Backend**
- `tools/framer/app.py`: `_stage_times` / `_chapter_steps` read each of the 7 steps from disk (write times, so a step is "out of date" when an earlier step was written after it). `/api/projects/<slug>` now returns per chapter `steps {done, stale, next}`, `has_thumb`, and `duration` for any chapter with a video. New `/chapters/<n>/thumb` (first exported frame, else a video frame). `POST /batch` takes `{plans: {chapter: [steps]}}`. Merge now only needs a video on disk (not "complete"). Removed the view-only chapter pipeline route.
- `tools/framer/batch.py`: each chapter has a `plan`; only planned steps run, in order. `rerun` uses the same plan mechanism. Old `batch.json` files load unchanged.
- `tools/framer/preflight.py`: checks depend on the steps being run.

**Frontend** (`tools/framer/index.html`)
- Semi-dark theme tokens (page < panel < row/card < input < modal, header strips, selection tint, light frames on thumbnails/videos); `--accent-fg` for red text (AA).
- Chapter list: one selection checkbox, list/card toggle (remembered per project), container-query layout so status and actions never overlap, 7-step tracker + "Next:" label + video length.
- Pipeline modal for every start (Run pipeline, per-chapter Pipeline button): prerequisite ticking, reuse/redo, out-of-date pre-ticks, groups by state, footer summary, remembered choices.

**Not changed:** the step chips in the Pipeline card still re-run a single step directly (they have their own confirm).

**Run next:** restart the app (`start.bat`) to load the backend changes.
