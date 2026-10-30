"""Merging chapter videos into one: ffmpeg commands and the merge job registry."""
from __future__ import annotations

import contextlib
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import branding
import chapter_backup
import gpu
import projects
from config import NO_CONSOLE, ROOT
from services.common import Refused
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


@contextlib.contextmanager
def merge_slot(slug: str):
    """Hold the project's one merge at a time; Refused when another is running."""
    with MERGE_LOCK:
        if slug in MERGE_JOBS:
            raise Refused("A merge is already running for this project.", 409)
        MERGE_JOBS[slug] = None
    try:
        yield
    finally:
        with MERGE_LOCK:
            MERGE_JOBS.pop(slug, None)


@dataclass
class MergePlan:
    out: Path
    cmd: list[str]
    notes: list[str]     # what was checked and how the videos will be joined, in order


def prepare_merge(slug: str, chapters: list[int]) -> MergePlan:
    """Check that every chapter has its video (restoring it from a Drive backup when it was cleared) and
    choose how to join them. Raises Refused naming what is wrong."""
    paths: list[Path] = []
    for n in chapters:
        if projects.get_chapter(projects.load(slug), n) is None:
            raise Refused(f"No chapter {n} in this project.", 404)
        try:
            chapter_backup.restore(slug, n, ["video"])
        except chapter_backup.BackupUnavailable as e:
            raise Refused(str(e), 503) from e
        video = projects.chapter_video_path(slug, n)
        if not video.is_file():
            raise Refused(f"Chapter {n} has no rendered recap.mp4.")
        paths.append(video)

    notes, infos = [f"Probing {len(paths)} video(s)…"], []
    for n, path in zip(chapters, paths):
        info = branding.probe(path)
        if info is None:
            raise Refused(f"Could not read video info for chapter {n}.")
        infos.append(info)
        v, a = info["video"], info.get("audio") or {}
        notes.append(f"  ch {n}: {v.get('width')}x{v.get('height')} {v.get('codec_name')} "
                     f"/ audio {a.get('codec_name', '?')}@{a.get('sample_rate', '?')}")
    uniform = (
        len({(i["video"].get("width"), i["video"].get("height"), i["video"].get("codec_name")) for i in infos}) == 1
        and len({(i.get("audio") or {}).get("codec_name") for i in infos}) == 1
        and len({(i.get("audio") or {}).get("sample_rate") for i in infos}) == 1
    )
    folder = projects.merged_dir(slug)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = folder / (f"merged_{stamp}_ch" + "-".join(str(n) for n in chapters) + ".mp4")
    if uniform:
        notes.append("All inputs share one size and codec - fast stream-copy merge (no re-encode).")
        list_file = folder / "_concat_list.txt"
        write_concat_list(paths, list_file)
        cmd = build_merge_cmd_copy(gpu.ffmpeg_path(), list_file, out)
    else:
        size = (infos[0]["video"]["width"], infos[0]["video"]["height"])
        notes.append(f"Inputs differ in resolution/codec - normalizing every clip to {size[0]}x{size[1]}@24fps before merging (slower).")
        cmd = build_merge_cmd_reencode(gpu.ffmpeg_path(), size, paths, out)
    return MergePlan(out, cmd, notes)


def merge_videos(slug: str, chapters: list[int]) -> str:
    """Merge the chapters' videos and return the merged file's name. Raises Refused, or RuntimeError when ffmpeg fails."""
    with merge_slot(slug):
        plan = prepare_merge(slug, chapters)
        done = subprocess.run(plan.cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL, **NO_CONSOLE)
        if done.returncode or not plan.out.is_file():
            raise RuntimeError(f"ffmpeg exited {done.returncode}: {done.stderr.strip()[-300:]}")
        return plan.out.name
