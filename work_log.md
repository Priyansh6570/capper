# Work log

Voice, script and layout round (all tests pass: 218).

- **Mixed narration** (`script_jobs.py`, `projects.py`, `index.html`): third option that uses "I" for the hero's own moments and "he/she" for everything else, switching only at scene boundaries.
- **Main video layout** (`projects.py`, `s7_assemble.py`, `index.html`, `app.js`): Project settings -> Video size -> Frame layout, Full frame or Full cover (`settings.video.layout`, passed to s7 as `cover`).
- **Emotion** (`pacing.py`, `claude_engine.py`, `app.js`): new `excited` and `angry` paces; each pace now also sets pitch, loudness and Chatterbox temperature. The Claude engine is told to pick the pace by feeling.
- **Gaps**: all pauses roughly halved (longest about 0.5s).
- **Speed / crash recovery** (`s6_tts.py`): voiced sentences are cached outside the project, so a re-run or crash retry skips them; no more CUDA cache flush per sentence.
- **C drive** (`config.py`): when `hf_home` is set, TEMP, torch, CUDA, Triton and other caches go to `<hf_home parent>\recapper_scratch` (here `D:\recapper_scratch`).
- **Comedic tone** (`tones.py`): rewritten to demand puns, masked swearing (spoken minced oaths), quick asides, never in serious scenes.
- `tests/test_tts_engine.py` updated for the temperature argument.

Next: restart the app, set the options in Project settings, re-run "script" for a chapter to see the new tone/POV, then re-render the voice.
