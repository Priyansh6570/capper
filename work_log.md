# Work log - Shorts: pick by merit across the range

- `tools/framer/shorts.py`: the find prompt (`_count_rule`) now asks for the best moments of the WHOLE range
  by score (skip weak chapters, several from one chapter allowed, none if nothing scores >= 6). `select()`
  ranks by score first; a chapter's best-before-second only breaks ties. Before, it forced one pick per
  chapter, which is why early chapters always won.
- `tools/framer/static/js/app.js`: while Claude chooses, the progress row reads "RANGE / Studying N chapters"
  instead of "CH <lowest>", and no chapter group is shown for it. After the find, only chapters it picked
  get rows, so a skipped first chapter no longer shows as if it got a short.
- `tests/test_shorts.py`: spread test replaced by merit tests (59 pass).

Cost note: the find is one `claude` call per ~150k chars of scripts (thinking already off, effort low),
then one small call per picked short. Fewer shorts requested = fewer calls.

Run: `.venv\Scripts\python.exe -m unittest discover -s tests -t .`
