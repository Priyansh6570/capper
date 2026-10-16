"""Splitting a script into narration lines."""
from __future__ import annotations

import re

import pacing


# --------------------------------------------------------------------------- #
# script paste -> segments. NOT a line-per-line split: pasted narration is
# usually prose (one or more paragraphs, possibly hard-wrapped by whatever
# editor it was written in), and a whole paragraph makes an unusably long,
# unboxable segment. So this treats blank-line-separated PARAGRAPHS as the
# input unit, splits each at SENTENCE boundaries, then greedily repacks
# consecutive sentences into segments sized for roughly SCRIPT_TARGET_SEC of
# narration each (stage 6's real TTS clip length is what actually drives
# on-screen timing - this is only a same-ballpark starting point so segments
# come out boxable without manual re-splitting).
# --------------------------------------------------------------------------- #
_SCRIPT_CHARS_PER_SEC = 15          # rough narration pace: ~150wpm * ~6 chars/word / 60s


_SCRIPT_TARGET_SEC = 4.0


_SCRIPT_TARGET_CHARS = round(_SCRIPT_CHARS_PER_SEC * _SCRIPT_TARGET_SEC)   # ~60


_SCRIPT_MAX_CHARS = round(_SCRIPT_TARGET_CHARS * 1.5)                      # ~90 - hard cap


_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')


_CLAUSE_SPLIT_RE = re.compile(r'(?<=[,;:])\s+')


_WS_RE = re.compile(r'\s+')


_PACE_TAG_RE = re.compile(r'^\s*\[(' + "|".join(pacing.PACES) + r')\]\s*', re.IGNORECASE)


def _normalize_ws(s: str) -> str:
    # Collapses ANY whitespace run - including non-breaking spaces pasted
    # from word processors, which .strip() alone leaves behind, turning a
    # visually-blank line into a "non-empty" one that survives filtering.
    return _WS_RE.sub(' ', s).strip()


def _split_long_sentence(sentence: str) -> list[str]:
    """A sentence over the hard cap: break at clause punctuation first,
    falling back to a plain word-boundary wrap, so no one segment is
    absurdly long (run-on sentences, list-like prose, etc.)."""
    parts = [p for p in _CLAUSE_SPLIT_RE.split(sentence) if p]
    out = []
    for part in parts:
        while len(part) > _SCRIPT_MAX_CHARS:
            cut = part.rfind(' ', 0, _SCRIPT_MAX_CHARS)
            if cut <= 0:
                cut = _SCRIPT_MAX_CHARS
            out.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            out.append(part)
    return out or [sentence]


def split_script_into_lines(text: str) -> list[tuple[str, str | None]]:
    """Turn pasted narration prose into short, boxable segments (see module
    note above), each with the delivery tag (pacing.PACES) its paragraph started
    with, or None. Paragraph order and sentence order are both preserved."""
    lines: list[tuple[str, str | None]] = []
    for para in re.split(r'\n\s*\n', text):
        tag = _PACE_TAG_RE.match(para)
        pace = tag.group(1).lower() if tag else None
        blob = _normalize_ws(para[tag.end():] if tag else para)
        if not blob:
            continue
        sentences = [s for s in (x.strip() for x in _SENTENCE_SPLIT_RE.split(blob)) if s]
        cur = ""
        for sent in sentences:
            pieces = [sent] if len(sent) <= _SCRIPT_MAX_CHARS else _split_long_sentence(sent)
            for piece in pieces:
                if not cur:
                    cur = piece
                elif len(cur) >= _SCRIPT_TARGET_CHARS or len(cur) + 1 + len(piece) > _SCRIPT_MAX_CHARS:
                    lines.append((cur, pace))
                    cur = piece
                else:
                    cur = cur + " " + piece
            if len(pieces) > 1:
                # A sentence long enough to need clause-splitting: never let
                # its trailing clause fragment blend into the NEXT sentence's
                # segment (e.g. a short "Finally," clause gluing onto the
                # sentence before it) - flush now, keep fragments confined
                # to the sentence they came from.
                lines.append((cur, pace))
                cur = ""
        if cur:
            lines.append((cur, pace))
    return lines
