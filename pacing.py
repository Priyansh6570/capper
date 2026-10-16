"""How a narration line is paced: where its silences fall and how fast it is spoken, by mood.

A line is voiced one sentence at a time and joined here. The text-to-speech engines are never
left to decide a pause: each sentence is trimmed of the silence the engine puts around it, and
the gaps are laid down by this module, so they fall only at the end of a sentence or thought and
their length follows the line's mood. A line that stops mid-sentence (a long sentence the framer
cut at a comma) is followed by only a breath, never a full pause.
"""
from __future__ import annotations

import re
import subprocess
from typing import Callable, NamedTuple

import numpy as np

from config import NO_CONSOLE
from gpu import ffmpeg_path


class Pace(NamedTuple):
    """Seconds of silence before the line, between its sentences, before its last sentence (the
    reveal or the punchline) and after it; the longest pause left inside a sentence (at a comma);
    and the speaking rate (above 1 is faster)."""
    lead: float
    sentence: float
    last: float
    tail: float
    inner: float
    speed: float


PACES = {
    "calm":   Pace(0.00, 0.45, 0.45, 0.70, 0.28, 1.00),
    "action": Pace(0.00, 0.15, 0.15, 0.25, 0.14, 1.08),
    "tense":  Pace(0.00, 0.28, 0.28, 0.40, 0.20, 1.04),
    "reveal": Pace(0.70, 0.40, 0.65, 0.80, 0.30, 0.97),
    "sad":    Pace(0.15, 0.70, 0.70, 1.00, 0.42, 0.92),
    "comic":  Pace(0.00, 0.30, 0.65, 0.55, 0.24, 1.03),
}
DEFAULT_PACE = "calm"

# older emotion tags in pace terms
_ALIASES = {"neutral": "calm", "excited": "action", "angry": "tense", "surprised": "reveal"}

BREATH = 0.12             # after a line that stops mid-sentence
PUNCTUATION_WEIGHT = {"?": 1.15, "!": 0.85, "…": 1.5}   # scales the gap after a sentence by how it ends
MIN_SENTENCE_WORDS = 2    # shorter sentences are voiced with the next one (engines garble single words)
SPEED_RANGE = (0.85, 1.15)
SILENCE_DB = -42          # a stretch this far below the clip's peak loudness counts as silence
PAUSE_FRAME = 0.01        # resolution at which silences inside a sentence are measured
EDGE_KEEP = 0.04          # speech kept either side of the trim, so consonants are not clipped
FADE = 0.008              # edge fade on each sentence, so joins do not click

_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "vs", "jr", "sr", "no"}
_SENTENCE_END = re.compile(r"[.!?…]+[\"'”’)\]]*(?=\s)")
_CLOSERS = "\"'”’)]"


def pace_of(mood: str | None, text: str) -> str:
    """The pace id for a line: its own mood (or a stage 3 emotion), else inferred from the text."""
    mood = (mood or "").strip().lower()
    mood = _ALIASES.get(mood, mood)
    if mood in PACES:
        return mood
    sentences = split_sentences(text)
    shouted = sum(s.rstrip(_CLOSERS).endswith("!") for s in sentences)
    return "action" if sentences and shouted * 2 >= len(sentences) else DEFAULT_PACE


def split_sentences(text: str) -> list[str]:
    """The sentences of `text`. A sentence ends at . ! ? … (with its closing quote) before a space and
    a capital or digit, except after an abbreviation ('Dr.', 'No. 93'); one under MIN_SENTENCE_WORDS
    words stays with the sentence after it."""
    text = " ".join(text.split())
    parts, start = [], 0
    for end in _SENTENCE_END.finditer(text):
        word = text[start:end.start()].rsplit(" ", 1)[-1].lower()
        following = text[end.end():].lstrip()[:1]
        if (end.group().startswith(".") and word in _ABBREVIATIONS) or following.islower():
            continue
        parts.append(text[start:end.end()])
        start = end.end() + 1
    if text[start:].strip():
        parts.append(text[start:])
    merged: list[str] = []
    short = ""
    for part in parts:
        part = f"{short} {part}".strip()
        if len(part.split()) < MIN_SENTENCE_WORDS:
            short = part
        else:
            merged.append(part)
            short = ""
    if short:
        merged[-1:] = [f"{merged[-1]} {short}"] if merged else [short]
    return merged


def _weight(sentence: str) -> float:
    return PUNCTUATION_WEIGHT.get(sentence.rstrip(_CLOSERS)[-1:], 1.0)


def _ends_thought(text: str) -> bool:
    return text.rstrip(_CLOSERS)[-1:] in ".!?…"


def _quiet_frames(wav: np.ndarray, frame: int) -> np.ndarray:
    """Per frame of `frame` samples: is it silent, its loudness (RMS) being SILENCE_DB or more below the
    peak? Engines leave a noise floor, so a single loud sample does not make a frame speech."""
    rms = np.sqrt((wav[:len(wav) // frame * frame].reshape(-1, frame) ** 2).mean(axis=1))
    return rms < float(np.max(np.abs(wav))) * 10 ** (SILENCE_DB / 20)


def trim_silence(wav: np.ndarray, sample_rate: int) -> np.ndarray:
    """`wav` without the silence an engine leaves at either end."""
    frame = int(PAUSE_FRAME * sample_rate)
    voiced = np.flatnonzero(~_quiet_frames(wav, frame)) if len(wav) >= frame else []
    if len(voiced) == 0:
        return wav
    keep = int(EDGE_KEEP * sample_rate)
    return wav[max(0, voiced[0] * frame - keep):(voiced[-1] + 1) * frame + keep]


def cap_pauses(wav: np.ndarray, sample_rate: int, longest: float) -> np.ndarray:
    """`wav` with every silence inside it shortened to at most `longest` seconds. An engine decides
    the length of its pauses at commas itself and sometimes holds one for most of a second."""
    frame = int(PAUSE_FRAME * sample_rate)
    if len(wav) < frame or not np.any(wav):
        return wav
    quiet = _quiet_frames(wav, frame)
    keep = np.ones(len(wav), dtype=bool)
    start = None
    for number, is_quiet in enumerate(np.append(quiet, False)):
        if is_quiet and start is None:
            start = number
        elif not is_quiet and start is not None:
            excess = (number - start) * frame - int(longest * sample_rate)
            if excess > 0:
                middle = (start + number) * frame // 2
                keep[middle - excess // 2:middle - excess // 2 + excess] = False
            start = None
    return wav[keep]


def stretch(wav: np.ndarray, sample_rate: int, speed: float) -> np.ndarray:
    """`wav` spoken `speed` times as fast, pitch unchanged (ffmpeg's atempo)."""
    if abs(speed - 1.0) < 0.005:
        return wav
    result = subprocess.run(
        [ffmpeg_path(), "-loglevel", "error", "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
         "-filter:a", f"atempo={speed:.4f}", "-f", "f32le", "pipe:1"],
        input=wav.astype(np.float32).tobytes(), capture_output=True, check=True, **NO_CONSOLE)
    return np.frombuffer(result.stdout, dtype=np.float32)


def _faded(wav: np.ndarray, sample_rate: int) -> np.ndarray:
    n = min(int(FADE * sample_rate), len(wav) // 2)
    wav = wav.astype(np.float32, copy=True)
    if n:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        wav[:n] *= ramp
        wav[-n:] *= ramp[::-1]
    return wav


def voice_line(text: str, mood: str | None, speak: Callable[[str, float], np.ndarray], sample_rate: int,
               speed: float = 1.0) -> np.ndarray:
    """The narration of `text` as one mono waveform at `sample_rate`, pauses included.

    `speak(sentence, speed)` voices one sentence and returns its waveform at `sample_rate`; it applies
    `speed` (the line's pace times `speed`, the caller's own factor) itself, natively or with `stretch`.
    """
    pace = PACES[pace_of(mood, text)]
    rate = min(SPEED_RANGE[1], max(SPEED_RANGE[0], pace.speed * speed))
    sentences = split_sentences(text) or [text]

    def silence(seconds: float) -> np.ndarray:
        return np.zeros(round(seconds * sample_rate), dtype=np.float32)

    pieces = [silence(pace.lead)]
    for number, sentence in enumerate(sentences):
        spoken = cap_pauses(trim_silence(speak(sentence, rate), sample_rate), sample_rate, pace.inner)
        pieces.append(_faded(spoken, sample_rate))
        if number < len(sentences) - 1:
            before_last = number == len(sentences) - 2
            pieces.append(silence((pace.last if before_last else pace.sentence) * _weight(sentence)))
    pieces.append(silence(pace.tail * _weight(sentences[-1]) if _ends_thought(text) else BREATH))
    return np.concatenate(pieces)
