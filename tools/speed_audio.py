"""Speed up already-synthesized narration clips without changing pitch.

This is a post-processing utility, NOT a pipeline stage: it does not implement
`run(m) -> m` and the orchestrator never calls it. Run it by hand after stage 6
when the Chatterbox narration feels too slow for a recap, e.g.

    python tools/speed_audio.py --chapter ch_001 --factor 1.25

For every panel that has audio in `work/<chapter>/audio/`, the samples are
pushed through ffmpeg's `atempo` filter (time-stretch only, pitch preserved),
which is the simplest reliable route on Windows. ffmpeg writes a temp file that
then replaces the original in place (use `--suffix` to write a new file beside
it instead). After the stretch, each clip's REAL duration is re-measured with
soundfile and written back to `panel.duration_s`, because stage 7 derives slide
timing from it. Script text and `panel.script_line` are never touched - only the
audio bytes and the durations.

`atempo` only accepts a factor in [0.5, 2.0]; values outside that are realized
by chaining several `atempo` stages whose product equals `--factor`, so e.g.
3.0 becomes `atempo=1.732,atempo=1.732`.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Allow `python tools/speed_audio.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import chapter_work_dir          # noqa: E402
from manifest import Manifest                 # noqa: E402


def _atempo_chain(factor: float) -> str:
    """Build an `atempo` filter string for any positive factor.

    A single atempo stage is clamped to [0.5, 2.0]; we decompose `factor` into a
    product of in-range stages so arbitrarily large/small speedups still work.
    """
    if factor <= 0:
        raise SystemExit(f"--factor must be positive, got {factor}")
    stages: list[float] = []
    remaining = factor
    while remaining > 2.0:
        stages.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        stages.append(0.5)
        remaining /= 0.5
    stages.append(remaining)
    return ",".join(f"atempo={s:.6f}" for s in stages)


def _speed_one(src: Path, dst: Path, filt: str) -> None:
    """Run ffmpeg src -> dst with the given atempo filter, writing a temp file
    first so an in-place replace never corrupts the original on failure."""
    tmp = dst.with_name(dst.stem + ".speedtmp" + dst.suffix)
    cmd = ["ffmpeg", "-y", "-i", str(src), "-filter:a", filt, str(tmp)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise SystemExit(
            f"ffmpeg failed on {src.name} (exit {proc.returncode}):\n{proc.stderr.strip()}"
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp), str(dst))


def _real_duration(path: Path) -> float:
    """Measured length of a clip in seconds (frames / sample_rate)."""
    import soundfile as sf

    info = sf.info(str(path))
    return info.frames / float(info.samplerate)


def main() -> None:
    ap = argparse.ArgumentParser(description="Speed up narration clips (no pitch change)")
    ap.add_argument("--chapter", default="ch_001")
    ap.add_argument("--factor", type=float, default=1.25,
                    help="speedup factor; >1 is faster (default 1.25)")
    ap.add_argument("--suffix", default="",
                    help="if set, write to <name><suffix>.<ext> instead of in place")
    args = ap.parse_args()

    work = chapter_work_dir(args.chapter)
    if not (work / "manifest.json").exists():
        raise SystemExit(f"No manifest at {work / 'manifest.json'}; run the pipeline first.")
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg not found on PATH; install it and re-run.")

    m = Manifest.load(work)
    filt = _atempo_chain(args.factor)

    old_total = new_total = 0.0
    changed = 0
    for panel in m.ordered_panels():
        if not panel.audio:
            continue
        src = Path(panel.audio)
        if not src.exists():
            print(f"    [{panel.order:03d}] missing audio, skipping: {src}")
            continue

        old_dur = panel.duration_s or _real_duration(src)
        dst = src.with_name(src.stem + args.suffix + src.suffix) if args.suffix else src
        _speed_one(src, dst, filt)
        new_dur = _real_duration(dst)

        panel.audio = str(dst)
        panel.duration_s = round(new_dur, 2)
        old_total += old_dur
        new_total += new_dur
        changed += 1
        print(f"    [{panel.order:03d}] {old_dur:5.2f}s -> {new_dur:5.2f}s  {dst.name}")

    m.save()
    print(f"\nSped up {changed} clip(s) by {args.factor}x (atempo: {filt}).")
    print(f"Total narration: {old_total:.1f}s -> {new_total:.1f}s")


if __name__ == "__main__":
    main()
