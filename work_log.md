# Work log - Claude Code script engine + automatic panel splitting

- New `tools/framer/panel_split.py`: cuts the stitched strip at near-uniform gutter rows into full-width
  `work/<ch>/panels/P001.png…`, drops blank/sliver panels, splits very tall ones at the most uniform row
  near the middle, records them as `Page.panels` in the chapter manifest.
- New `tools/framer/claude_engine.py`: drives the `claude` CLI (`-p --output-format json --json-schema
  --allowedTools Read`, cwd = panel folder, no `--bare`). Pass 1 = factual breakdown, pass 2 = narration
  blocks via `--resume`. State saved after each pass in `work/<ch>/claude/state.json`; story-so-far +
  character sheet in `projects/<slug>/story_memory.json`. Usage limit -> job pauses until reset, resumes itself.
- `app.py`: `script` job type on the existing queue, `POST /api/script/claude`, `GET /api/claude/status`,
  narration pre-fills the framer (one full-panel box per panel id; newest of saved state / pre-fill wins).
  Export keeps the panels in the manifest; `/api/generate` now requires exported narration lines.
  Gemini prompt helpers split into `_narration_voice` / `_style_reference_directive` (Gemini output unchanged).
- `projects.py` + `index.html`: settings `script_engine` (gemini|claude), `claude.{series_brief,
  comprehension_model, narration_model}`; the script button and Settings card follow the chosen engine.
- Tone / narration type / style reference still live under `settings.gemini` and apply to both engines.

Next: restart the app, pick "Claude Code" under Settings -> Script generation, then "Generate script with Claude"
in a chapter. Then `git add tools/framer work_log.md` and commit.
