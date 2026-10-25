"""A short sample of how a video's sound will mix: a few of the project's own narration lines over its music,
at the volumes given. It runs the same filter graph stage 7 renders with (`s7_assemble._audio_filters`), so
a change to the sliders can be heard at once instead of after a re-render.

No Flask in here: routes/projects.py serves it.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import gpu
import projects
from config import NO_CONSOLE, ROOT
from manifest import Manifest
from stages import s7_assemble as s7

SAMPLE_S = 14.0       # the sample ends here
MAX_LINES = 4
FALLBACK_LINE_S = 4.0
DEFAULT_VOICE = ROOT / "assets" / "voice_ref.wav"


class NoSample(Exception):
    """There is no narration to mix yet."""


def narration_lines(slug: str) -> list[tuple[Path, float]]:
    """A few voiced lines of the project's first chapter that has any: (audio file, seconds)."""
    for chapter in projects.load(slug)["chapters"]:
        try:
            beats = Manifest.load(projects.chapter_work_path(slug, chapter["n"])).kept_panels()
        except (OSError, ValueError):
            continue
        voiced = [(Path(b.audio), b.duration_s or FALLBACK_LINE_S) for b in beats
                  if (b.script_line or "").strip() and b.audio and Path(b.audio).is_file()]
        if voiced:
            return voiced[:MAX_LINES]
    if (voice := voice_reference(slug)) is not None:   # nothing rendered (or cleared after a backup): the voice sample
        return [(voice, SAMPLE_S)]
    raise NoSample("There is no narration to play yet: render a chapter, or add a voice sample in the voice settings.")


def voice_reference(slug: str) -> Path | None:
    """The clip Chatterbox clones: the project's own voice sample, else the app's default one."""
    voice = projects.load(slug)["settings"]["voice"]
    if voice.get("mode") == "file" and (own := projects.resolve_asset_path(slug, voice.get("file"))):
        return own
    return DEFAULT_VOICE if DEFAULT_VOICE.is_file() else None


def music_track(slug: str, mode: str, own: bool) -> Path | None:
    """The music the chosen mode plays: the uploaded file for `file` (the shorts' own or the main one), the
    default track for `default`, nothing for `none`."""
    if mode == "none":
        return None
    if mode == "file" and (uploaded := projects.find_asset(slug, "shorts_music" if own else "music")):
        return uploaded
    return s7._music_file()


def render(slug: str, *, mode: str, own: bool, narration_vol: float, music_vol: float) -> bytes:
    """The sample as WAV bytes."""
    lines = narration_lines(slug)
    music = music_track(slug, mode, own)
    cmd = [gpu.ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error"]
    narration, start = [], 0.0
    for index, (audio, seconds) in enumerate(lines):
        cmd += ["-i", str(audio)]
        narration.append((index, int(start * 1000)))
        start += seconds
    music_in = None
    if music:
        music_in = len(lines)
        cmd += ["-stream_loop", "-1", "-i", str(music)]
    filters, label = s7._audio_filters(narration, music_in, narration_vol, music_vol)
    with tempfile.TemporaryDirectory(prefix="audio-sample-") as tmp:
        out = Path(tmp) / "sample.wav"
        cmd += ["-filter_complex", ";".join(filters), "-map", label, "-t", f"{min(start + 1.0, SAMPLE_S):.2f}",
                "-c:a", "pcm_s16le", str(out)]
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **NO_CONSOLE)
        if done.returncode or not out.is_file():
            raise RuntimeError(f"ffmpeg could not mix the sample: {done.stderr.strip()[-300:]}")
        return out.read_bytes()
