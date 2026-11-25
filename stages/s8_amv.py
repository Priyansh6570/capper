"""Stage 8 (add-on) - AMV shorts: beat-synced vertical edits of a chapter's frame bank over phonk tracks.

Replaces the narration shorts of `s8_shorts.py` for the daily shorts pipeline. Like that stage it is outside the
stage chain (STAGE_ORDER, orchestrator.py) and leaves the manifest alone: it reads files and writes files.

    in : work/<chapter>/shorts_bank/  (the tagged frames, built by the framer's tagging job; see amv/bank.py)
         the track folder (shorts/bg music/*.mp3), optionally a project's quotes.json (style `quote`)
    out: output/<chapter>/shorts/short_NN.mp4, short_NN.jpg (thumbnail: a beautiful female face, no text; amv/thumb.py) and shorts.json (same index the upload queue reads: file, title,
         description, duration_s, script) - new shorts are APPENDED, numbering continues, nothing is deleted.

Every short gets one style (aura, story, twist, pivot, adaptive, quote - see amv/styles.py) and one track, rotated
by `--offset` so another call gives other combinations. The title/description written here are plain fallbacks;
publishing/metadata.py rewrites them with Claude before upload.

Run from the project root:
    python -m stages.s8_amv --chapter <id> --count 2 --styles aura,adaptive,quote --series "Magic Emperor" \
        [--quotes projects/<slug>/quotes.json] [--offset 3]
Progress lines match what the framer's job tracker reads: "shorts i/n: title" and "render NN%".
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from config import NO_CONSOLE, ROOT, SHORTS_FOLDER, SHORTS_INDEX, chapter_output_dir, chapter_work_dir

TRACKS = ROOT / "shorts" / "bg music"
DEFAULT_STYLES = ("quote",)       # every short is a quote short (style 6); the other styles still work when asked for by name


def tracks(folder: Path = TRACKS) -> list[Path]:
    return sorted(p for p in folder.glob("*.mp3"))


def split_files(text: str | None) -> list[Path] | None:
    """`--track-files a.mp3|b.mp3` as paths (None when not given)."""
    files = [Path(x) for x in (text or "").split("|") if x.strip()]
    return files or None


def duration_of(path: Path) -> float | None:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, check=True, **NO_CONSOLE).stdout.strip()
        return round(float(out), 1)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def read_index(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))["shorts"]
    except (OSError, ValueError, KeyError):
        return []


def story_lines(items: list[dict], n: int = 6) -> list[str]:
    """The chapter's script lines (kept in the bank beside each frame), in reading order, `n` spread over it."""
    seen: list[str] = []
    for x in sorted(items, key=lambda x: (str(x.get("ch", "")), x.get("order", 0))):
        line = (x.get("line") or "").strip()
        if line and line not in seen:
            seen.append(line)
    if len(seen) <= n:
        return seen
    return [seen[int(i * len(seen) / n)] for i in range(n)]


def fallback_title(series: str, style: str, lines: list[str]) -> str:
    head = (lines[0] if lines else style).split(".")[0].strip()
    title = f"{series}: {head}" if series else head
    if len(title) > 70:
        title = title[:70].rsplit(" ", 1)[0]          # never cut a word in half
    return title.rstrip(" ,;:")


def make_quotes(path: Path, items: list[dict], series: str) -> list[dict]:
    """A project with no quotes.json yet gets one: Claude writes lines in the protagonist's voice (the file is then
    the project's quote bank, editable on the Shorts page). Lines it is not sure of are marked in `note`."""
    from amv import cast, quotes as qmod
    who = cast.protagonist_name(items, series, log=lambda *a: print(*a, flush=True)) or "the main protagonist"
    print(f"no quotes for {series or 'this project'} yet: asking Claude for lines said by {who}", flush=True)
    try:
        found = qmod.suggest(series or "this series", who, n=10)
    except Exception as e:  # noqa: BLE001 - reported below as a clear error
        raise SystemExit(f"could not make quotes for {series}: {type(e).__name__}: {e}")
    if not found:
        raise SystemExit(f"Claude returned no quotes for {series}")
    qmod.save(path, found)
    print(f"  wrote {len(found)} quotes -> {path}", flush=True)
    return qmod.load(path)


def run(chapter: str, count: int, styles: list[str], series: str, quotes_file: str | None, offset: int,
        track_dir: Path = TRACKS, preview: bool = False, track_files: list[Path] | None = None,
        look: str | None = None) -> list[dict]:
    from amv import bank, quotes as qmod, render
    work = chapter_work_dir(chapter)
    out_dir = chapter_output_dir(chapter) / SHORTS_FOLDER
    out_dir.mkdir(parents=True, exist_ok=True)
    items = bank.load(work)
    if not items:
        raise SystemExit(f"no shorts bank for {chapter}: run the frame tagging first ({work / 'shorts_bank'})")
    music_files = [p for p in track_files if p.is_file()] if track_files else tracks(track_dir)
    if not music_files:
        raise SystemExit(f"no .mp3 tracks in {track_dir}")
    quotes = qmod.load(quotes_file) if quotes_file and Path(quotes_file).is_file() else []
    if "quote" in styles and not quotes and quotes_file:
        quotes = make_quotes(Path(quotes_file), items, series)
    styles = [s for s in styles if s != "quote" or quotes]
    if not styles:
        raise SystemExit("no usable style (quote needs a quotes.json with lines)")
    lines = story_lines(items)
    index = out_dir / SHORTS_INDEX
    done = read_index(index)
    number = max([s.get("id", 0) for s in done] + [0])
    made: list[dict] = []
    for i in range(count):
        style = styles[(offset + i) % len(styles)]
        track = music_files[(offset + i) % len(music_files)]
        quote = quotes[(offset + i) % len(quotes)] if style == "quote" else None
        number += 1
        out = out_dir / f"short_{number:02d}.mp4"
        about = quote["text"] if quote else None
        title = fallback_title(series, style, [about] if about else lines)
        print(f"shorts {i + 1}/{count}: {title} [{style}, {track.stem}]", flush=True)
        step = lambda p: print(f"  render {int(p * 100)}%", flush=True)
        try:
            if quote:
                from amv import quote_short
                quote_short.make(track, [work], quote, out, None, None, preview, None, 1.0, progress=step,
                                 log=lambda *a: print(*a, flush=True), series=series, look=look)
            else:
                render.render([], track, out, None, 1.0, None, None, 0, preview, progress=step, recipe=style, items=items)
        except Exception as e:  # noqa: BLE001 - one short failing (a track without a usable drop) must not stop the rest
            print(f"  skipped ({type(e).__name__}: {e})", flush=True)
            number -= 1
            continue
        thumb = None
        try:
            from amv import thumb as thumbs
            thumb = thumbs.pick(items, work, number - 1, out.with_suffix(".jpg"), log=lambda *a: print(*a, flush=True))
        except Exception as e:  # noqa: BLE001 - a short without a thumbnail is still a short
            print(f"  no thumbnail ({type(e).__name__}: {e})", flush=True)
        entry = {"id": number, "file": out.name, "title": title, "style": style, "track": track.name,
                 "thumbnail": thumb.name if thumb else None,
                 "description": (about or (lines[0] if lines else ""))[:400], "size": out.stat().st_size,
                 "duration_s": duration_of(out), "script": [{"text": t} for t in ([about] if about else lines)]}
        made.append(entry)
        done.append(entry)
        index.write_text(json.dumps({"chapter_id": chapter, "shorts": done}, indent=2, ensure_ascii=False), encoding="utf-8")
    if not made:
        raise SystemExit("no short was rendered")
    return made


def main() -> None:
    ap = argparse.ArgumentParser(description="Render AMV shorts of a chapter from its frame bank")
    ap.add_argument("--chapter", required=True)
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--styles", default=",".join(DEFAULT_STYLES))
    ap.add_argument("--series", default="")
    ap.add_argument("--quotes")
    ap.add_argument("--offset", type=int, default=0, help="rotates which style / track / quote the shorts start with")
    ap.add_argument("--tracks", help="folder of .mp3 tracks (default shorts/bg music)")
    ap.add_argument("--track-files", help="these audio files (separated by |) instead of a folder: a project's own music")
    ap.add_argument("--look", help="quote look (amv/looks.py): ember scroll royal neon blood frost; default: the project's")
    ap.add_argument("--preview", action="store_true")
    a = ap.parse_args()
    run(a.chapter, a.count, [s for s in a.styles.split(",") if s], a.series, a.quotes, a.offset,
        Path(a.tracks) if a.tracks else TRACKS, a.preview, split_files(a.track_files), a.look)


if __name__ == "__main__":
    sys.exit(main())
