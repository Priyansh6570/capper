# Work log - latest task: frames/layout, GPU memory check, Kokoro engine

## Layout (stages/s7_assemble.py)
Cause of the empty space (chapter 4: frames covered ~35% of the screen on average, 51 of 61 frames near-square or tall):
fixed 0.85H/0.92W margins, one frame per 16:9 still, and a 1.15x Ken Burns zoom that crops anything filling more than 87% of
the canvas. Now `_compose_still` packs all of a still's frames with `_arrange` (rows of one height, the split that gives the
largest frames), filling `FILL` 0.96 of the canvas, or `KEN_BURNS_FILL` 0.88 with Ken Burns on (peak zoom now 1.10, so the
frames stay inside the visible window at every moment). `_compose_grid_still` is gone.

## Frames (claude engine)
- `tools/framer/frame_crop.py` (new): snaps a model-chosen subject to an exact crop: keeps the subject, cuts detected bubbles
  out where possible, otherwise returns them to be blurred (heavy gaussian, style "g"), widens toward 16:9 inside the panel and
  never into a cut-out bubble, and grows to a minimum size (`MAX_UPSCALE` 1.6).
- `claude_engine.py`: pass 1 now lists `subjects` per panel (label, tight box as fractions, has_text, facing); pass 2 picks
  `frames: [{panel_id, subject, flop}]` and `mode` (sequence | together). Locally: flop is ignored for subjects with text and for
  sound_effect panels, a frame that mostly repeats an earlier one in the block is dropped, a one-frame "together" becomes sequence.
  Blocks now store ready frames (`bbox`, `flip`, `blur`); `app._state_from_narration` still reads old saved blocks.
- Verified with the real CLI (haiku) on four chapter 4 panels: schemas accepted, crops/blur/mode resolved. Not run: a full
  chapter with Opus/Sonnet.

## Audio
- GPU memory: the Chatterbox process peaked at 2.86 GB dedicated, 0.12 GB shared (fits). The whole GPU hit 3.89 of 4.0 GB
  because other apps hold ~1 GB. Chapter 4 script on AC power: 586s for 653s of audio (RTF 0.90); on battery the same code ran at 1.66.
- Kokoro (`stages/tts_kokoro.py`, chosen per project: Project settings -> Narration voice -> Kokoro; voice list, Preview button via
  `/api/tts/kokoro/preview`). Chapter 4: Kokoro af_heart 31s for 685s (RTF 0.05) vs Chatterbox 586s for 653s (RTF 0.90).
  Listen: `work/audio_compare/ch4_chatterbox.wav` and `ch4_kokoro.wav`.
- Install: setup.bat step 4e (kokoro/misaki with --no-deps) + requirements.txt additions; already installed in this venv.
  Torch is still +cu128 (cuda True). First Kokoro use downloads the model and spaCy's English model.

Restart the app to pick everything up.
