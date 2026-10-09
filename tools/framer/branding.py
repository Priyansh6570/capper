"""Intro/outro branding for merged videos - no Flask here.

Branding applies to a MERGED video only; chapter renders stay unbranded. A raw
merge is joined to an intro and an outro into a separate `<merge>_branded.mp4`
and the raw file is never touched. The raw merge is stream-copied, so only the
short clips are ever re-encoded - to the merge's own resolution, fps, pixel
format and audio format - and the result is cached per (clip, target format).
Background music and watermark are baked into the chapter renders, so they can
never reach the clips.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import gpu
import projects
from config import NO_CONSOLE, ROOT

BRANDED_SUFFIX = "_branded"
GAP_SECONDS = 1.0          # black + silence between an intro/outro and the main video
FADE_SECONDS = 2.0         # the main video's audio rises over this long...
FADE_START_VOLUME = 0.15   # ...starting from this fraction of full volume
CLIP_KINDS = ("intro", "outro")
DEFAULT_CLIPS = {"intro": ROOT / "recapper_intro.mp4", "outro": ROOT / "recapper_outro.mp4"}

_CHAPTERS_IN_NAME = re.compile(r"_ch(\d+(?:-\d+)*)\.mp4$")
_X264_PROFILES = {"baseline": "baseline", "constrained baseline": "baseline",
                  "main": "main", "high": "high"}
_DURATIONS: dict[tuple, float] = {}

# (command, clip duration in seconds, on_fraction(0..1)) - runs one ffmpeg process.
Runner = Callable[[list, float, Callable[[float], None]], None]


def ffprobe_path() -> str:
    ffmpeg = Path(gpu.ffmpeg_path())
    sibling = ffmpeg.with_name("ffprobe" + ffmpeg.suffix)
    return str(sibling) if sibling.is_file() else (shutil.which("ffprobe") or "ffprobe")


def probe(path: Path) -> Optional[dict]:
    """{"video": stream, "audio": stream or {}, "duration": seconds (whole file),
    "video_duration": seconds (picture only)} for `path`, or None when it can't be
    read or has no video stream."""
    try:
        out = subprocess.run(
            [ffprobe_path(), "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,profile,width,height,r_frame_rate,pix_fmt,"
             "sample_rate,channels,duration:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, **NO_CONSOLE)
        data = json.loads(out.stdout or "{}")
    except Exception:  # noqa: BLE001 - an unreadable file is reported as None
        return None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return None
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    duration = float((data.get("format") or {}).get("duration") or 0)
    return {"video": video, "audio": audio, "duration": duration,
            "video_duration": float(video.get("duration") or duration)}


def duration_of(path: Path) -> float:
    """Length in seconds, cached per file version."""
    key = (str(path), path.stat().st_mtime_ns)
    if key not in _DURATIONS:
        info = probe(path)
        _DURATIONS[key] = info["duration"] if info else 0.0
    return _DURATIONS[key]


def target_format(info: dict) -> dict:
    """The format every clip must match so the raw merge can be stream-copied."""
    video, audio = info["video"], info["audio"]
    return {
        "width": int(video["width"]), "height": int(video["height"]),
        "fps": video.get("r_frame_rate") or "24/1",
        "pix_fmt": video.get("pix_fmt") or "yuv420p",
        "profile": _X264_PROFILES.get((video.get("profile") or "").lower()),
        "sample_rate": int(audio.get("sample_rate") or 48000),
        "channels": int(audio.get("channels") or 2),
    }


def clip_source(slug: str, kind: str) -> Path:
    """The custom upload for `kind` when the project has one, else the bundled default."""
    custom = projects.load(slug)["settings"]["branding"][kind]["file"]
    path = projects.resolve_asset_path(slug, custom) or DEFAULT_CLIPS[kind]
    if not path.is_file():
        raise FileNotFoundError(f"The {kind} clip is missing: {path.name}")
    return path


def _normalize_cmd(ffmpeg: str, src: Path, out: Path, target: dict, info: dict) -> list:
    w, h, rate = target["width"], target["height"], target["sample_rate"]
    layout = "stereo" if target["channels"] == 2 else "mono"
    video_filter = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                    f"fps={target['fps']},format={target['pix_fmt']}")
    audio_filter = f"aresample={rate},aformat=channel_layouts={layout},apad"
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats",
           "-progress", "pipe:1", "-i", str(src)]
    if info["audio"]:
        audio_map = "0:a:0"
    else:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={rate}:cl={layout}"]
        audio_map = "1:a"
    cmd += ["-map", "0:v:0", "-map", audio_map, "-vf", video_filter, "-af", audio_filter,
            *_encode_args(target), "-t", f"{info['video_duration']:.3f}", "-f", "mp4", str(out)]
    return cmd


def _encode_args(target: dict) -> list:
    args = ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
    if target["profile"]:
        args += ["-profile:v", target["profile"]]
    return args + ["-c:a", "aac", "-b:a", "192k", "-ar", str(target["sample_rate"]),
                   "-ac", str(target["channels"])]


def _gap_cmd(ffmpeg: str, out: Path, target: dict) -> list:
    rate = target["sample_rate"]
    layout = "stereo" if target["channels"] == 2 else "mono"
    return [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
            "-f", "lavfi", "-i", f"color=c=black:s={target['width']}x{target['height']}:r={target['fps']}",
            "-f", "lavfi", "-i", f"anullsrc=r={rate}:cl={layout}",
            "-vf", f"format={target['pix_fmt']}", *_encode_args(target),
            "-t", f"{GAP_SECONDS:.3f}", "-f", "mp4", str(out)]


def _cached(slug: str, key_parts: list, run: Runner, on_fraction: Callable[[float], None],
            cmd_for: Callable[[Path], list], duration: float) -> Path:
    key = hashlib.sha1(json.dumps(key_parts, sort_keys=True).encode()).hexdigest()[:16]
    cached = projects.branding_cache_dir(slug) / f"{key}.mp4"
    if cached.is_file():
        on_fraction(1.0)
        return cached
    part = cached.with_name(cached.name + ".part")
    try:
        run(cmd_for(part), duration, on_fraction)
        part.replace(cached)
    finally:
        part.unlink(missing_ok=True)
    return cached


def normalized_clip(slug: str, src: Path, target: dict, run: Runner,
                    on_fraction: Callable[[float], None]) -> Path:
    """`src` re-encoded to `target` (silent track added when it has no audio),
    cached so each clip is only ever encoded once per target format."""
    info = probe(src)
    if info is None:
        raise ValueError(f"{src.name} is not a readable video file.")
    stat = src.stat()
    return _cached(slug, [str(src.resolve()), stat.st_size, stat.st_mtime_ns, target], run, on_fraction,
                   lambda part: _normalize_cmd(gpu.ffmpeg_path(), src, part, target, info),
                   info["video_duration"])


def silent_gap(slug: str, target: dict, run: Runner, on_fraction: Callable[[float], None]) -> Path:
    """GAP_SECONDS of black and silence in `target`'s format, cached."""
    return _cached(slug, ["gap", GAP_SECONDS, target], run, on_fraction,
                   lambda part: _gap_cmd(gpu.ffmpeg_path(), part, target), GAP_SECONDS)


def _fade_in_cmd(ffmpeg: str, raw: Path, out: Path, target: dict) -> list:
    """The raw merge with its audio rising from FADE_START_VOLUME to full over
    FADE_SECONDS; the picture is copied untouched."""
    ramp = f"min(1,{FADE_START_VOLUME}+{1 - FADE_START_VOLUME}*t/{FADE_SECONDS})"
    layout = "stereo" if target["channels"] == 2 else "mono"
    return [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
            "-i", str(raw), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
            "-af", f"volume='{ramp}':eval=frame,aformat=channel_layouts={layout}",
            "-c:a", "aac", "-b:a", "192k", "-ar", str(target["sample_rate"]),
            "-ac", str(target["channels"]), "-f", "mp4", str(out)]


def render_final(slug: str, name: str, run: Runner, on_fraction: Callable[[float], None]) -> Path:
    """Write `<merge>_branded.mp4` next to the raw merge `name`: enabled intro,
    a silent gap, the merge (picture copied as-is, audio fading in), a gap, and
    the enabled outro."""
    raw = merge_path(slug, name)
    if not raw.is_file():
        raise FileNotFoundError(f"{name} no longer exists.")
    info = probe(raw)
    if info is None:
        raise ValueError(f"{name} is not a readable video file.")
    if not info["audio"]:
        raise ValueError(f"{name} has no audio track to fade in.")
    branding = projects.load(slug)["settings"]["branding"]
    kinds = [k for k in CLIP_KINDS if branding[k]["enabled"]]
    if not kinds:
        raise ValueError("Turn on the intro or the outro first.")

    target = target_format(info)
    steps = len(kinds) + 3          # clips, gap, fade-in, join
    step = 0

    def stage() -> Callable[[float], None]:
        nonlocal step
        step += 1
        return lambda f, base=step - 1: on_fraction((base + f) / steps)

    clips = {kind: normalized_clip(slug, clip_source(slug, kind), target, run, stage())
             for kind in kinds}
    gap = silent_gap(slug, target, run, stage())

    final = raw.with_name(f"{raw.stem}{BRANDED_SUFFIX}.mp4")
    part = final.with_name(final.name + ".part")
    faded = final.with_name(final.name + ".faded")
    concat_list = final.with_name(final.name + ".txt")
    try:
        run(_fade_in_cmd(gpu.ffmpeg_path(), raw, faded, target), info["duration"], stage())
        parts = ([clips["intro"], gap] if "intro" in clips else []) + [faded] + \
                ([gap, clips["outro"]] if "outro" in clips else [])
        concat_list.write_text("\n".join(f"file '{str(p).replace(chr(92), '/')}'" for p in parts),
                               encoding="utf-8")
        run([gpu.ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-nostats",
             "-progress", "pipe:1", "-f", "concat", "-safe", "0", "-i", str(concat_list),
             "-c", "copy", "-f", "mp4", str(part)],
            sum(probe(p)["duration"] for p in parts), stage())
        part.replace(final)
    finally:
        for leftover in (part, faded, concat_list):
            leftover.unlink(missing_ok=True)
    return final


def merge_path(slug: str, name: str) -> Path:
    """Path of a raw merge, rejecting anything that isn't a plain merge file name."""
    if Path(name).name != name or not name.startswith("merged_") or not name.endswith(".mp4") \
            or name.endswith(BRANDED_SUFFIX + ".mp4"):
        raise ValueError(f"Not a merged video: {name}")
    return projects.merged_dir(slug) / name


def final_path(raw: Path) -> Path:
    return raw.with_name(f"{raw.stem}{BRANDED_SUFFIX}.mp4")


def _describe(slug: str, path: Path) -> dict:
    stat = path.stat()
    return {"name": path.name, "url": f"/projects/{slug}/merged/{path.name}",
            "size": stat.st_size, "duration": duration_of(path),
            "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds")}


def list_merges(slug: str) -> list[dict]:
    """Every merged video on disk, newest first, each with its branded file if any."""
    merges = []
    for raw in projects.merged_dir(slug).glob("merged_*.mp4"):
        if raw.stem.endswith(BRANDED_SUFFIX):
            continue
        found = _CHAPTERS_IN_NAME.search(raw.name)
        final = final_path(raw)
        merges.append({
            **_describe(slug, raw),
            "chapters": [int(n) for n in found.group(1).split("-")] if found else [],
            "final": _describe(slug, final) if final.is_file() else None,
        })
    return sorted(merges, key=lambda m: m["created_at"], reverse=True)


def delete_merge(slug: str, name: str, *, final_only: bool = False) -> None:
    raw = merge_path(slug, name)
    final_path(raw).unlink(missing_ok=True)
    if not final_only:
        raw.unlink(missing_ok=True)
