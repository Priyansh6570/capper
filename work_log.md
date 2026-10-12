# Work log - latest task: batch pipeline, usage limiter, faster TTS

## Narration (claude engine)
Already in place before this task (verified, not changed): every block keeps >=1 usable frame
(`_finish_blocks` drops blocks with none, junk never narrated), 10-minute default, speaking rate measured
from the project's generated audio (`_speech_rate`), one extension pass, then the shortfall is recorded.

## Faster audio (`stages/s6_tts.py`)
Chapter 1 of the-stellar-swordmaster (42 lines, ~201s audio), Turbo on RTX 3050, on battery:
- Before: 568s wall, RTF 2.83 (T3 token decode ~485s, s3gen ~55s, model load 28s, reference conditioning 25s
  (redone on every line), file writes 0.3s).
- After: 335s wall, RTF 1.66 (T3 258s, s3gen 55s, load 15s, conditioning 1.3s once).
- Changes: reference is conditioned once (`Engine.prepare_ref`) instead of per line; Turbo's T3 runs fp16
  weights (`_halve_t3`) because the decode step is GPU memory-bandwidth bound.
- Voice check: speaker-embedding cosine to the reference 0.9365 vs 0.9379 before; 18.2 vs 18.3 chars/s.
- Plug the laptop in: on battery the GPU was power-capped (~17 GB/s memory throughput).

## Batch automation
- New `tools/framer/batch.py` (engine: per-stage lanes, persistence in `projects/<slug>/batch.json`, retry,
  pause/resume/cancel, review gate, usage gate) and `tools/framer/claude_usage.py`.
- `app.py`: download/stitch/export/render/script routes now call shared functions the batch also uses;
  `_HARDWARE` locks keep Claude/audio/video to one job at a time even against the manual queue;
  routes under `/api/projects/<slug>/batch`; batches resume on startup.
- `claude_cli.call(before_call=...)`, `claude_engine.run_chapter(before_call=...)` gate every Claude call.
- Usage source: the `rate_limit_event` in `claude -p` stream-json (`unifiedWindows.five_hour|seven_day`
  `{utilization 0-1, resetsAt}`); the status line does not run under `-p`. Saved to `claude_usage.json`.
- Settings (Project settings > Batch automation): review before audio (off), 70% / 60% thresholds.
- UI: checkboxes with shift-range, "Run pipeline", queue card with usage, stage chips, errors, retry, continue.

Not verified: the UI in a real browser (extension unavailable); a full real chapter through the batch.
Restart the app to pick everything up.
