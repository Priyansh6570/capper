"""Merging chapter videos into one: ffmpeg commands and the merge job registry."""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from services.jobs import ACTIVE_CHAPTERS, JOBS_LOCK


# --------------------------------------------------------------------------- #
# merge multiple chapters' rendered videos into one, in chapter order. Purely
# a framer-side feature over already-rendered recap.mp4 files - it reads
# finished output, nothing about the manifest/orchestrator/pipeline stages
# needs to change for this.
# --------------------------------------------------------------------------- #
MERGE_JOBS: dict[str, "subprocess.Popen | None"] = {}


MERGE_LOCK = threading.Lock()


def write_concat_list(paths: list[Path], dest: Path) -> None:
    """ffmpeg concat-demuxer list file. Forward slashes side-step that format's
    own backslash-escaping rules, which Windows paths would otherwise trip."""
    lines = [f"file '{str(p).replace(chr(92), '/')}'" for p in paths]
    dest.write_text("\n".join(lines), encoding="utf-8")


def build_merge_cmd_copy(ffmpeg: str, list_file: Path, out: Path) -> list[str]:
    """Fast path: every input already shares video+audio codec and resolution,
    so just concatenate the encoded bitstream - no re-encode, near-instant."""
    return [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stats",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", str(out)]


def build_merge_cmd_reencode(ffmpeg: str, size: tuple[int, int], paths: list[Path], out: Path) -> list[str]:
    """Safe path for mismatched inputs: scale/letterbox every video to
    the first video's size @24fps (never cropped or stretched) and resample audio to a
    common format before concatenating, so a stray non-standard render can't
    corrupt or desync the merge."""
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stats"]
    for p in paths:
        cmd += ["-i", str(p)]
    parts, vlabels, alabels = [], [], []
    for i in range(len(paths)):
        parts.append(f"[{i}:v]scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease,"
                     f"pad={size[0]}:{size[1]}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p[v{i}]")
        parts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=mono[a{i}]")
        vlabels.append(f"[v{i}]")
        alabels.append(f"[a{i}]")
    concat_inputs = "".join(v + a for v, a in zip(vlabels, alabels))
    parts.append(f"{concat_inputs}concat=n={len(paths)}:v=1:a=1[vout][aout]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[aout]"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "30", "-c:a", "aac", "-b:a", "64k",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    return cmd


# --------------------------------------------------------------------------- #
# merged videos: the on-disk list, intro/outro "final" renders (queued like any
# other job, see _run_brand_job) and deletion. Branding never touches chapter
# renders - see branding.py.
# --------------------------------------------------------------------------- #
def brand_key(slug: str, name: str) -> str:
    return f"brand:{slug}:{name}"


def _busy(key: str) -> bool:
    with JOBS_LOCK:
        return key in ACTIVE_CHAPTERS
