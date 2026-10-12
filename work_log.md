# Work log - live Claude Code activity, token totals, stall fix

- Diagnosis (ch4): the claude call was not hung - `--output-format json` prints nothing until the end, so the UI sat on a
  fixed 10% / fixed-estimate ETA while Claude read one panel per turn (152 panels in 23 min, tens of millions of cache tokens).
- `claude_engine.py`: calls now use `--output-format stream-json --verbose --include-partial-messages` (+ `--json-schema`),
  parsed live into events: panel reads, streamed text, per-pass token usage (summed from `message_delta`, matches the final
  result), rate-limit and API-retry notices. Prompt asks for 8 Reads per turn. After a usage limit the interrupted session is
  resumed instead of restarted. Per-pass and chapter totals (+ `total_cost_usd`, seconds) saved in `claude/state.json`.
- `app.py`: script jobs expose `claude` stats (pass, panels read/total, tokens) in `/api/jobs`, progress/ETA from real work,
  SSE push at `/api/jobs/<id>/events`, `/api/claude/panels`, project `claude_usage` total.
- `index.html`: live feed under the script button (Reading Pxxx, streamed text), strip marks (read / reading / skipped, auto-scroll),
  stats in the editor, corner widget and project sidebar; "estimated API cost (not charged on subscription)".

Next: restart done; run "Generate script with Claude" on a chapter (ch4 pass 1 was never saved - the earlier run hit the
usage limit, then lost the network).
