# Work log

Pipeline card redesigned to match the "Pipeline Running" design (claude.ai/design), on `perf-refactor` (not pushed).

- `tools/framer/batch.py`: each chapter records `stage_spans` (`{stage: [start_epoch, end_epoch|null]}`) when a lane starts and ends a stage; old saved batches load without it.
- `tools/framer/index.html`, `static/css/app.css`, `static/js/app.js`: new card with status chip, stat blocks, stage rail, timeline (filters, legend, now marker, hatched estimates), activity log, Claude totals, usage meters with limit markers; when finished: video cards (tick to merge, tied to the chapter selection), "Where the time went", Run next N, Dismiss. Dim and light themes use the design's tokens. Hold banner, review Continue, Retry, Watch live/Open editor, per-chapter logs and Cancel/Pause are unchanged and live under "Chapter details" (it opens by itself when a chapter fails or waits for review).
- Not changed: the counts strip above the pipeline and the merge bar keep their old look.
- `tests/test_batch.py`: spans are recorded for every stage that ran.

**Verified:** 154 tests; both states rendered headlessly in Edge with sample data (browser extension was unavailable). Not yet seen against a live run.

**Run next:** restart the app from the tray if it is already running, then start a pipeline on one chapter and watch the card.
