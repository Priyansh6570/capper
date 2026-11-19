"""Music analysis for AMV shorts: beats, downbeats, energy, sections and drops.

Everything is cached next to a hash of the file in `cache/amv_music/`, so a track is
analysed once. The result is plain JSON (the files-only contract of the pipeline):

    {"bpm", "duration", "beats": [s...], "downbeats": [s...],
     "energy": {"hop_s", "rms": [...], "low": [...]}, "drops": [s...]}

BPM from a tracker is often half/double the felt tempo (phonk especially), so `analyze`
takes `tempo_scale` (0.5 / 1 / 2) which the plan window exposes as a toggle.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache" / "amv_music"
SR = 22050
HOP = 512


def _file_key(path: Path) -> str:
    h = hashlib.sha1()
    h.update(str(path.stat().st_size).encode())
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
    return h.hexdigest()[:16]


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    return np.convolve(x, np.ones(n) / n, mode="same")


def _detect_drops(low: np.ndarray, hop_s: float, min_gap_s: float = 7.0) -> list[float]:
    """A drop = low-band (kick/808) energy that jumps well above the last ~2s, right after
    a quieter stretch. Returns times, strongest-first dedup'd by `min_gap_s`."""
    k = max(1, int(round(2.0 / hop_s)))
    lowm = _smooth(low, 9)
    scored: list[tuple[float, int]] = []
    for i in range(k, len(lowm) - k):
        before = lowm[i - k:i].mean() + 1e-9
        after = lowm[i:i + k].mean()
        ratio = after / before
        if ratio > 1.8:
            scored.append((float(ratio), i))
    scored.sort(reverse=True)
    picked: list[int] = []
    for _, i in scored:
        if all(abs(i - j) * hop_s >= min_gap_s for j in picked):
            picked.append(i)
    return sorted(round(i * hop_s, 3) for i in picked)


def _detect_breaks(low: np.ndarray, hop_s: float) -> list[list[float]]:
    """Stretches (>= 0.8s) where the kick/808 all but vanishes: the gap before a drop or
    between sections. Returns [[start, end], ...] in seconds."""
    lowm = _smooth(low, int(0.5 / hop_s) | 1)
    ref = float(np.percentile(lowm, 60)) + 1e-9
    quiet = lowm < ref * 0.3
    out, i, n = [], 0, len(quiet)
    while i < n:
        if quiet[i]:
            j = i
            while j < n and quiet[j]:
                j += 1
            if (j - i) * hop_s >= 0.8:
                out.append([round(i * hop_s, 3), round(j * hop_s, 3)])
            i = j
        else:
            i += 1
    return out


ONSET_HOP = 256


def _onsets(y: np.ndarray, sr: int) -> list[list[float]]:
    """The track's audible hits as [time, strength]: peaks of the spectral-flux envelope at a finer hop than the
    beat tracker, strength 0..1.5 relative to a loud hit. Cuts follow these where the track's rhythm is not the
    tracker's even grid (syncopation, a build with no kick yet)."""
    import librosa

    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=ONSET_HOP)
    frames = librosa.onset.onset_detect(onset_envelope=env, sr=sr, hop_length=ONSET_HOP, backtrack=False)
    ref = float(np.percentile(env, 95)) or 1.0
    times = librosa.frames_to_time(frames, sr=sr, hop_length=ONSET_HOP)
    return [[round(float(t), 4), round(float(min(1.5, env[f] / ref)), 3)] for t, f in zip(times, frames)]


def analyze(path: str | Path, tempo_scale: float = 1.0, use_cache: bool = True) -> dict:
    import librosa  # heavy import, only when analysing

    path = Path(path)
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE / f"{path.stem}_{_file_key(path)}_{tempo_scale:g}_v3.json"
    if use_cache and cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    y, sr = librosa.load(str(path), sr=SR, mono=True)
    duration = len(y) / sr
    hop_s = HOP / sr

    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr, hop_length=HOP, tightness=120)
    beats = librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP)
    bpm = float(np.atleast_1d(tempo)[0])

    if tempo_scale == 2.0 and len(beats) > 1:       # felt tempo is double: add the midpoints
        mids = (beats[:-1] + beats[1:]) / 2
        beats = np.sort(np.concatenate([beats, mids]))
        bpm *= 2
    elif tempo_scale == 0.5:                         # felt tempo is half: keep every other beat
        beats = beats[::2]
        bpm /= 2

    onsets = _onsets(y, sr)

    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    spec = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    low = spec[:12].sum(axis=0)                      # < ~250 Hz: kick / 808
    n = min(len(rms), len(low))
    rms, low = rms[:n], low[:n]

    drops = _detect_drops(low, hop_s)

    # downbeats: of the 4 possible phases pick the one whose beats carry the most low energy
    downbeats: list[float] = []
    if len(beats) >= 8:
        idx = np.clip((beats / hop_s).astype(int), 0, n - 1)
        phase_e = [low[idx[p::4]].mean() for p in range(4)]
        p = int(np.argmax(phase_e))
        downbeats = [float(b) for b in beats[p::4]]

    result = {
        "file": str(path), "bpm": round(bpm, 2), "duration": round(duration, 3),
        "beats": [round(float(b), 4) for b in beats],
        "downbeats": [round(b, 4) for b in downbeats],
        "drops": drops,
        "onsets": onsets,
        "breaks": _detect_breaks(low, hop_s),
        "energy": {"hop_s": round(hop_s, 5),
                   "rms": [round(float(v), 5) for v in rms[::4]],
                   "low": [round(float(v), 3) for v in low[::4]]},
    }
    try:
        cache_file.write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        pass
    return result


def snap(t: float, grid: list[float]) -> float:
    """Nearest grid time to t."""
    if not grid:
        return t
    a = np.asarray(grid)
    return float(a[np.argmin(np.abs(a - t))])


def hit_near(a: dict, t: float, reach: float = 0.2, floor: float = 0.6) -> float:
    """`t` moved onto the strongest audible hit within `reach` seconds (when there is one), so a clip starts
    on a sound and not on a grid line the ear does not hear."""
    near = [(s, o) for o, s in a.get("onsets", []) if abs(o - t) <= reach and s >= floor]
    return max(near)[1] if near else t


def pick_window(a: dict, length: float | None, drop: float | None = None, lead: float | None = None,
                min_len: float = 15.0, max_len: float = 58.0) -> tuple[float, float, float]:
    """Choose (start, end, drop_in_window). The window starts on a downbeat and puts the drop
    at about `lead` seconds in. With `length=None` it ends where the music's own section ends:
    at the first break (the dip after the drop section), plus a short tail, so the track is never
    cut in the middle of a phrase."""
    drops = a["drops"]
    want = length or 30.0
    if drop is None:
        need = max(4.0, want * 0.35)
        usable = [d for d in drops if d + want * 0.6 <= a["duration"] and d >= need] or drops or [a["duration"] * 0.4]
        drop = usable[0]
    drop = snap(drop, a["beats"])
    lead = want * 0.35 if lead is None else lead
    grid = a["downbeats"] or a["beats"]
    start = hit_near(a, snap(max(0.0, drop - lead), grid))
    if start > drop - 2.0:                      # never start at/after the drop
        earlier = [g for g in grid if g <= drop - 2.0]
        start = earlier[-1] if earlier else 0.0
    if length is None:
        brk = next((b for b in a.get("breaks", []) if drop + 8.0 < b[0] <= start + max_len - 1.0), None)
        end = (brk[0] + 0.9) if brk else start + want
        end = min(end, a["duration"], start + max_len)
        if end - start < min_len:
            end = min(a["duration"], start + min_len)
        return start, end, drop - start
    start = min(start, max(0.0, a["duration"] - length))
    return start, start + length, drop - start
