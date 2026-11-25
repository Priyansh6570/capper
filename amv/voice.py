"""Deep narration of a quote with word times, for the quote short (style 6).

The voice is Kokoro `am_michael` (any `stages.tts_kokoro.VOICES` name works), spoken slowly, pitched down a little and
given weight in ffmpeg (bass, a barely audible echo for body, gentle compression). The sentences are voiced one by one, so the
pauses are exactly ours; each word gets a start/end inside its sentence in proportion to its length, which is what the
on-screen text follows.

`narrate(text)` returns a `Narration` (wav path + word list); nothing is kept in memory between calls and the wav is
cached by text + voice, so a re-render of the same quote does not run the model again.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from . import NO_CONSOLE

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache" / "amv_voice"
VOICE = "am_michael"
SPEED = 0.86            # slower than the narration of a recap: a quote is weighed, not told
PITCH = 0.94            # a touch lower
SENTENCE_PAUSE = 0.62   # between sentences (seconds)
VERSION = 5
POST = ("bass=g=4:f=150:w=0.8,equalizer=f=3200:t=q:w=1.2:g=-2,"
        "acompressor=threshold=-20dB:ratio=3:attack=15:release=180:makeup=3,"
        "aecho=0.92:0.7:26:0.07,loudnorm=I=-10:TP=-1.0:LRA=6")
# the closing message is spoken as it is: Kokoro's own voice at its normal speed, only levelled to match the quote
POST_PLAIN = "acompressor=threshold=-20dB:ratio=2.5:attack=15:release=180:makeup=2,loudnorm=I=-10:TP=-1.0:LRA=6"
PLAIN_VOICE = "am_adam"
PLAIN_PAUSE = 0.35      # one faint 26 ms tap: body, not a room


@dataclass
class Word:
    text: str
    t0: float          # seconds from the start of the narration wav
    t1: float
    sentence: int


@dataclass
class Narration:
    wav: Path
    duration: float
    words: list[Word]
    text: str


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", " ".join(text.split()))
    return [p for p in parts if p]


def _word_spans(sentence: str, t0: float, t1: float, sentence_no: int) -> list[Word]:
    """Split [t0, t1] over the words of `sentence`: longer words take longer, a comma or dash adds a breath."""
    words = sentence.split()
    weights = []
    for w in words:
        n = len(re.sub(r"\W", "", w)) + 1.5
        weights.append(n + (3.0 if re.search(r"[,;:—–-]$", w) else 0.0))
    total = sum(weights) or 1.0
    out, t = [], t0
    for w, wt in zip(words, weights):
        d = (t1 - t0) * wt / total
        out.append(Word(w, round(t, 3), round(t + d, 3), sentence_no))
        t += d
    return out


def _post(wav: np.ndarray, rate: int, post: str = POST) -> np.ndarray:
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    res = subprocess.run([ffmpeg, "-v", "error", "-f", "f32le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
                          "-af", f"{post},aresample={rate}", "-f", "f32le", "pipe:1"], input=wav.astype(np.float32).tobytes(),
                         capture_output=True, check=True, **NO_CONSOLE)
    return np.frombuffer(res.stdout, dtype=np.float32)


def narrate(text: str, voice: str = VOICE, speed: float = SPEED, pitch: float = PITCH,
            cache: Path = CACHE, plain: bool = False) -> Narration:
    """`plain`: the voice as Kokoro speaks it (no pitch change, no bass, no echo), for a spoken message rather than a quote."""
    key = hashlib.sha1(f"v{VERSION}|{voice}|{speed}|{pitch}|{plain}|{text}".encode()).hexdigest()[:16]
    cache.mkdir(parents=True, exist_ok=True)
    wav_path, meta_path = cache / f"{key}.wav", cache / f"{key}.json"
    if wav_path.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return Narration(wav_path, meta["duration"], [Word(**w) for w in meta["words"]], text)

    import pacing
    from stages import tts_kokoro
    rate = tts_kokoro.SAMPLE_RATE
    pipe = tts_kokoro.load_pipeline(voice, "cuda" if _cuda() else "cpu")
    pieces: list[np.ndarray] = []
    spans: list[tuple[float, float, str]] = []
    t = 0.0
    lead = np.zeros(round(0.05 * rate), np.float32)
    pieces.append(lead); t += len(lead) / rate
    sentences = _sentences(text) or [text]
    for i, s in enumerate(sentences):
        raw = tts_kokoro.synthesize(pipe, s, voice, speed)
        raw = pacing.cap_pauses(pacing.trim_silence(raw, rate), rate, 0.45)
        raw = pacing.stretch(raw, rate, 1.0, pitch) if pitch != 1.0 else raw
        spans.append((t, t + len(raw) / rate, s))
        pieces.append(raw); t += len(raw) / rate
        if i < len(sentences) - 1:
            gap = np.zeros(round((PLAIN_PAUSE if plain else SENTENCE_PAUSE) * rate), np.float32)
            pieces.append(gap); t += len(gap) / rate
    dry = np.concatenate(pieces)
    wav = _post(dry, rate, POST_PLAIN if plain else POST)
    # loudnorm works at a higher internal rate; the result must still be the same length (+ the echo tail)
    if not (len(dry) / rate - 0.2 <= len(wav) / rate <= len(dry) / rate + 1.5):
        raise RuntimeError(f"narration post-processing changed the length: {len(dry) / rate:.1f}s -> {len(wav) / rate:.1f}s")
    # echo tail: the clip ends when the last word does; the tail is allowed to ring on under the music
    sf.write(wav_path, wav, rate)
    words = [w for i, (a, b, s) in enumerate(spans) for w in _word_spans(s, a, b, i)]
    dur = round(len(wav) / rate, 3)
    meta_path.write_text(json.dumps({"duration": dur, "words": [w.__dict__ for w in words]}), encoding="utf-8")
    return Narration(wav_path, dur, words, text)


def spoken_end(n: Narration) -> float:
    """When the last word ends (the wav is a little longer: its echo)."""
    return n.words[-1].t1 if n.words else n.duration


def _cuda() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:  # noqa: BLE001
        return False
