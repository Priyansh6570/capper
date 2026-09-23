# Work log

## Task: restore intelligent script-splitting in the framer (regression)

### What was actually wrong

`tools/framer/app.py`'s `/load_script` route (used by the "Load script"
button next to the pasted-narration textarea) was doing exactly what the bug
report described - and had been since this repo's very first commit
(`git log` shows only 3 commits touch `app.py`; even the initial commit
already had this code, so there's no earlier "smart" version left to recover
via git):

```python
lines = [ln.strip() for ln in text.splitlines()]
lines = [ln for ln in lines if ln]
```

A pure newline split: one input line in, one segment out, full stop. A
pasted PARAGRAPH (no hard line breaks inside it) became exactly one
unusably-long segment - never split at sentence boundaries, never sized
toward a ~4s-per-frame target. (Blank/whitespace-only lines were already
filtered by this old code and did NOT produce empty segments in practice -
the one exception being a line containing ONLY non-breaking spaces or other
non-ASCII whitespace, which `.strip()` doesn't remove, so it would survive
the `if ln` filter as a "non-empty" empty-looking line. Handled in the fix
below too.)

The textarea's own placeholder text ("Paste the narration script, one
segment per line") documented this same wrong-for-prose expectation, which
is why it read as intentional rather than broken.

### The fix

`tools/framer/app.py`: replaced the two-line splitter with
`split_script_into_lines()` (+ two small helpers, `_normalize_ws` and
`_split_long_sentence`) that:

1. Splits the input into PARAGRAPHS on blank lines (`\n\s*\n`) - the unit
   the bug report calls out ("splitting long paragraphs...").
2. Within each paragraph, collapses all whitespace (including non-breaking
   spaces) and joins any hard-wrapped lines into one blob, then splits that
   at SENTENCE boundaries (after `.`/`!`/`?` + whitespace).
3. Greedily repacks consecutive sentences into segments targeting
   `_SCRIPT_TARGET_CHARS` (~60 chars, from a ~15 chars/sec narration-pace
   estimate * the requested 4.0s target) - a segment closes once it's
   already at/over the target OR the next sentence would push it past a
   hard cap (~90 chars, 1.5x target).
4. A single sentence longer than the hard cap (run-on prose, no punctuation)
   falls back to splitting at clause punctuation (commas/semicolons/colons),
   then a plain word-boundary wrap as a last resort.
5. A clause-split sentence's trailing fragment is flushed immediately
   rather than allowed to blend into the NEXT sentence's segment - avoids
   awkward joins like "...the gate. Finally," gluing a dangling fragment
   from the following sentence onto the one before it.
6. Empty paragraphs (blank lines, or lines of only whitespace/non-breaking
   spaces) never produce a segment.

Also updated the textarea's placeholder text in `tools/framer/index.html` to
describe the restored (correct) behavior instead of the old wrong one.

### Verified

- Direct unit-level test of `split_script_into_lines()` against several
  inputs (short lines, empty/whitespace-only input, a long multi-sentence
  paragraph, a paragraph with a very long run-on sentence, multi-paragraph
  input) - all split as intended, no empty segments.
- Killed a STALE leftover `app.py` process from an earlier task in this
  session that was still bound to port 5005 and silently serving pre-fix
  code (why the first couple of live re-tests looked unchanged) - restarted
  clean, then confirmed via a real `/load_script` POST and via the actual
  in-app "Load script" button (checked `S.lines` and the rendered `#lines`
  panel) that segments come out split correctly end-to-end.
- Tested against a real chapter's editor via `loadScript()` (which is
  undo-tracked) then called `undo()` to restore its original 43 lines
  afterward, so no real project data was disturbed by the test.
- Server stopped afterward; confirmed port 5005 has no listener.

### Files changed

- `tools/framer/app.py` (`/load_script` + new `split_script_into_lines`,
  `_split_long_sentence`, `_normalize_ws` helpers)
- `tools/framer/index.html` (`#scriptText` placeholder text)

### Next steps for the user

- No backend/pipeline changes, no new dependencies - refresh the browser tab
  (or restart `start.bat`) to pick this up.
- The ~4s/60-char target is a tunable constant
  (`_SCRIPT_TARGET_SEC`/`_SCRIPT_CHARS_PER_SEC` near the top of the
  `/load_script` section in `app.py`) if the narration pace assumption needs
  adjusting later.
