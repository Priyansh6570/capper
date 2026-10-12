# Work log - narration validation, 10-minute target, measured speaking rate

- Validation after pass 2 (`claude_engine._finish_blocks`): every block keeps >=1 usable frame or is dropped. A block pointing at junk
  or at nothing is dropped; one pointing only at bubble panels falls back to the usable panels of its scene. The prompt also hides
  junk-only scenes and forbids title-card / chapter-end narration.
- Length: target default 10 min (`claude.target_minutes`). Duration is estimated from the project's own generated audio
  (`app._speech_rate`: chars per second over beats with audio; ch4 = 18.0, not 15). Pass 1 now also records stakes and world info per
  scene; pass 2 reaches length through depth (beats, feelings, stakes, world/power system, links to story so far), never padding.
  One extension pass when under 90% of target; if still short, `narration.shortfall` is stored and shown in the job result.
- Frame rules apply to extension blocks (same `_finish_blocks`).
- ch4 re-run: 57 blocks, 10.2 min, every block 1-3 frames, no junk frames, min 3.03 s/frame, $5.72 estimated.
