"""Stage 8 (add-on) - Shorts: vertical 9:16 videos of the segments the Shorts pipeline picked.

It is not part of the linear stage chain (STAGE_ORDER, orchestrator.py): it runs on a chapter that
already has its narration, frames and video, and leaves `m.stage` alone. It reads
work/<chapter>/shorts/plan.json, written by the framer's find step, and renders the chapter's own
frames and narration lines `first_line`..`last_line` of every planned segment with stage 7's renderer
on a portrait canvas, under the same media settings as the main video (the project's `shorts` settings
choose the layout, `frame` or `cover`, and may replace the watermark, the background, and the music and volumes).

A planned short with a `script` (the find step writes one: a new recap narration of the scene that ends on a teaser,
each line naming the chapter lines whose panels to show) is voiced here with the chapter's own voice engine
(`s6_tts.voice`, into work/<chapter>/shorts/audio, the chapter's narration is untouched) and rendered over those
panels; a plan without a script replays the chapter's own lines and audio. Every short gets the rounded recapper logo
in its top left corner (assets/ReCapper.png).

Output: output/<chapter>/shorts/short_NN.mp4 and shorts.json, the plan's entries plus each file (rewritten after
every short).

Run from the project root:  python -m stages.s8_shorts --chapter <id>
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from config import NO_CONSOLE, ROOT, SHORTS_FOLDER, SHORTS_INDEX, SHORTS_PLAN, chapter_output_dir, chapter_work_dir
from manifest import Manifest, Panel
from stages import s6_tts as s6
from stages import s7_assemble as s7


LOGO = ROOT / "assets" / "ReCapper.png"
LOGO_WIDTH = 0.16      # of the video width
LOGO_MARGIN = 0.04     # of the video width, from the left edge; the top margin is a bit larger (the apps' own top bar)
LOGO_CORNER = 0.22     # corner radius, of the logo's side
LOGO_BORDER = 0.035    # border thickness, of the logo's side


def rounded_logo(width: int, dest: Path) -> Path | None:
    """The recapper logo `width` px wide with curved corners and a thin white border, saved to `dest`
    (None when the logo file is missing)."""
    from PIL import Image, ImageDraw
    try:
        logo = Image.open(LOGO).convert("RGBA")
    except OSError:
        return None
    side = max(16, width)
    scale = 4   # drawn larger and scaled down, for smooth curves
    big = side * scale
    logo = logo.resize((big, round(big * logo.height / logo.width)), Image.LANCZOS)
    mask = Image.new("L", logo.size, 0)
    radius = round(big * LOGO_CORNER)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, logo.width - 1, logo.height - 1), radius=radius, fill=255)
    out = Image.new("RGBA", logo.size, (0, 0, 0, 0))
    out.paste(logo, (0, 0), mask)
    ImageDraw.Draw(out).rounded_rectangle((0, 0, logo.width - 1, logo.height - 1), radius=radius,
                                          outline=(255, 255, 255, 255), width=max(2, round(big * LOGO_BORDER)))
    out.resize((side, round(side * logo.height / logo.width)), Image.LANCZOS).save(dest)
    return dest


def add_logo(video: Path, render: s7.RenderPlan) -> None:
    """Burn the rounded logo into the top left corner of `video` (video re-encoded, audio copied)."""
    size = render.out_size
    logo = rounded_logo(round(size[0] * LOGO_WIDTH), video.with_name(f"_logo_{video.stem}.png"))
    if logo is None:
        print("  logo missing: assets/ReCapper.png", flush=True)
        return
    tmp = video.with_name(f"_logo_{video.name}")
    x, y = round(size[0] * LOGO_MARGIN), round(size[0] * LOGO_MARGIN * 2)
    try:
        subprocess.run([s7._require_ffmpeg(), "-y", "-loglevel", "error", "-i", str(video), "-i", str(logo),
                        "-filter_complex", f"[0:v][1:v]overlay={x}:{y}:format=auto[v]", "-map", "[v]", "-map", "0:a?",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy",
                        "-movflags", "+faststart", str(tmp)], check=True, **NO_CONSOLE)
        tmp.replace(video)
    finally:
        tmp.unlink(missing_ok=True)
        logo.unlink(missing_ok=True)


MAX_FRAMES_PER_LINE = 8
FRAME_SECONDS = 1.6    # a frame stays on screen about this long: shorts are fast paced, the picture keeps switching


def _frames_of(order: int, beats: dict[int, Panel], mapping: dict[int, dict]) -> list[str]:
    """The images of the chapter line `order`: all the frames its mapping shows, else its crop."""
    frames = [f["image"] for f in (mapping.get(order) or {}).get("frames", []) if f.get("image")]
    return frames or ([beats[order].crop] if order in beats and beats[order].crop else [])


def script_beats(short: dict, beats: dict[int, Panel], mapping: dict[int, dict]) -> tuple[list[Panel], dict[int, dict]]:
    """The beats of a short with a written script (`short["script"]`: new narration lines, each naming the chapter
    lines whose panels to show) and the mapping that gives them those panels. The lines are not voiced yet, so how
    many of the frames each shows is decided afterwards (`fit_frames`)."""
    made, new_mapping = [], {}
    for k, line in enumerate(short["script"], start=1):
        frames = []
        for order in line["source_lines"]:
            frames += [f for f in _frames_of(order, beats, mapping) if f not in frames]
        made.append(Panel(id=f"short{short['id']:02d}_{k:02d}", bbox=[0, 0, 0, 0], order=k, crop=frames[0] if frames else None,
                          emotion=line.get("pace"), script_line=line["text"]))
        new_mapping[k] = {"order": k, "mode": "sequence", "frames": [{"image": f} for f in frames[:MAX_FRAMES_PER_LINE]]}
    return made, new_mapping


def fit_frames(short: dict, panels: list[Panel], mapping: dict[int, dict], beats: dict[int, Panel],
               chapter: dict[int, dict]) -> None:
    """Once the lines are voiced: show each line as many frames as keep one on screen about FRAME_SECONDS (spread
    over the line's own panels in story order; when it names fewer than that, the panels just before and after
    them are added, so the cuts stay on the scene)."""
    for k, (panel, line) in enumerate(zip(panels, short["script"]), start=1):
        seconds = float(panel.duration_s or 0) or FRAME_SECONDS
        want = max(1, min(MAX_FRAMES_PER_LINE, round(seconds / FRAME_SECONDS)))
        own = [f["image"] for f in mapping[k]["frames"]]
        sources = line["source_lines"]
        frames = list(own)
        lo, hi = (min(sources), max(sources)) if sources else (0, -1)
        while len(frames) < want and (lo > min(beats, default=0) or hi < max(beats, default=0)):
            for order in (lo - 1, hi + 1):
                if order in beats and len(frames) < want:
                    extra = [f for f in _frames_of(order, beats, chapter) if f not in frames]
                    frames = extra + frames if order < lo else frames + extra
            lo, hi = lo - 1, hi + 1
        if len(frames) > want:   # evenly spread over the candidates
            frames = [frames[round(i * (len(frames) - 1) / max(1, want - 1))] for i in range(want)] if want > 1 else frames[:1]
        mapping[k]["frames"] = [{"image": f} for f in frames]
        if frames:
            panel.crop = frames[0]


def voice_scripts(m: Manifest, work: Path, planned: list[tuple[dict, list[Panel]]]) -> None:
    """Voice the written scripts of all the shorts together (the voice engine loads once), into the shorts folder
    of the chapter's work dir; the chapter's own narration is left alone."""
    every = [panel for _, panels in planned for panel in panels]
    if every:
        s6.voice(Manifest(chapter_id=m.chapter_id, source_pdf="", work_dir=str(work), beats=every), work / "audio")


MAX_OVERSHOOT_S = 20.0   # a track this much longer than the narration is still a good fit


def pick_track(library: list[dict], theme: str | None, narration_s: float, used: set[str],
               short_track: str | None = None) -> dict | None:
    """The track of the project's music library for a short: the one Claude chose for it when the plan names it
    (`short_track`, written to fit that track's length and drop), else those of the short's theme (all of them when none
    has it), preferring one the narration fits in (the short then runs as long as the track), the closest in
    length, and one not used by another short of the chapter while there is a choice."""
    pool = [t for t in library if t.get("theme") == theme] or list(library)
    if short_track:
        pool = [t for t in library if t.get("id") == short_track] or pool
    fresh = [t for t in pool if t["file"] not in used] or pool
    fits = [t for t in fresh if narration_s <= t["duration_s"] <= narration_s + MAX_OVERSHOOT_S]
    longer = [t for t in fresh if t["duration_s"] >= narration_s]
    if fits or longer:
        return min(fits or longer, key=lambda t: t["duration_s"] - narration_s)
    return max(fresh, key=lambda t: t["duration_s"]) if fresh else None


def run(m: Manifest) -> Manifest:
    plan = json.loads((chapter_work_dir(m.chapter_id) / SHORTS_FOLDER / SHORTS_PLAN).read_text(encoding="utf-8"))
    out_dir = chapter_output_dir(m.chapter_id) / SHORTS_FOLDER
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)

    beats = {b.order: b for b in m.kept_panels()}
    mapping = s7.load_mapping(m.chapter_id)
    settings = s7.load_media_settings(m.chapter_id)
    shorts_settings = settings.get("shorts") or {}
    settings = {**settings, **(shorts_settings.get("look") or {}), **(shorts_settings.get("sound") or {})}
    cover = shorts_settings.get("layout") == "cover"
    library = [t for t in shorts_settings.get("library") or [] if Path(t["file"]).is_file()]
    music_share = float(shorts_settings.get("music_share") or 0.4)
    used: set[str] = set()
    rendered = []
    index = out_dir / SHORTS_INDEX
    work = chapter_work_dir(m.chapter_id) / SHORTS_FOLDER
    # a short with a script is a new narration over chosen panels; one without (a plan from before) replays its lines
    scripted = {short["id"]: script_beats(short, beats, mapping) for short in plan["shorts"] if short.get("script")}
    voice_scripts(m, work, [(short, scripted[short["id"]][0]) for short in plan["shorts"] if short["id"] in scripted])
    for short in plan["shorts"]:
        if short["id"] in scripted:
            fit_frames(short, *scripted[short["id"]], beats, mapping)
    for number, short in enumerate(plan["shorts"], start=1):
        print(f"shorts {number}/{len(plan['shorts'])}: {short['title']}", flush=True)
        if short["id"] in scripted:
            segment, short_mapping = scripted[short["id"]]
        else:
            segment = [beats[order] for order in sorted(beats) if short["first_line"] <= order <= short["last_line"]]
            short_mapping = mapping
        chosen = {}

        def pick_music(narration_s: float) -> tuple[Path, float, str] | None:
            track = pick_track(library, short.get("theme"), narration_s, used, short.get("track")) if library else None
            if track is None:
                return None
            used.add(track["file"])
            chosen.update(music=track["name"], music_theme=track.get("theme"))
            return Path(track["file"]), float(track["duration_s"]), track["name"], track.get("drop_s")

        render = s7.prepare_render(segment, short_mapping, settings, s7.PORTRAIT, cover, pick_music, music_share,
                                 short["peak"] + 1 if short.get("peak") is not None else None)
        out = out_dir / f"short_{short['id']:02d}.mp4"
        s7.execute_render(render, out)
        add_logo(out, render)
        rendered.append({**short, "file": out.name, "size": out.stat().st_size, "duration_s": round(render.total, 1), **chosen})
        index.write_text(  # after every short, so the app can show each one as soon as it is ready
            json.dumps({"chapter_id": m.chapter_id, "shorts": rendered}, indent=2, ensure_ascii=False), encoding="utf-8")
    return m


def main() -> None:
    ap = argparse.ArgumentParser(description="Render the planned shorts of a chapter")
    ap.add_argument("--chapter", required=True)
    run(Manifest.load(chapter_work_dir(ap.parse_args().chapter)))


if __name__ == "__main__":
    main()
