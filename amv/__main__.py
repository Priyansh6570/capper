"""Prototype CLI:  python -m amv --track "shorts/bg music/xcool.mp3" --frames a.png b.png ... -o out.mp4

--analyze only prints the beat/drop analysis. --preview renders a half-size, 15fps check.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import bank, music, render, styles


def main() -> int:
    ap = argparse.ArgumentParser(prog="amv")
    ap.add_argument("--track", required=True)
    ap.add_argument("--frames", nargs="*", default=[])
    ap.add_argument("--frames-json", help="JSON list of frame paths")
    ap.add_argument("-o", "--out", default="amv_test.mp4")
    ap.add_argument("--length", type=float, default=None, help="seconds; omit to end where the music section ends")
    ap.add_argument("--start", type=float)
    ap.add_argument("--drop", type=float, help="drop time in the track (seconds)")
    ap.add_argument("--tempo-scale", type=float, default=1.0, choices=[0.5, 1.0, 2.0])
    ap.add_argument("--hero", type=int, default=0, help="index of the frame shown on the drop")
    ap.add_argument("--style", choices=["push", "alt"], default="push", help="push: every cut zooms in; alt: alternate in/out")
    ap.add_argument("--recipe", choices=sorted(styles.STYLES), help="style recipe; frames then come from --bank")
    ap.add_argument("--point", choices=list(render.POINTS), help="focus point for --recipe pivot (default eye)")
    ap.add_argument("--bank", nargs="*", default=[], help="chapter work folders whose shorts frame bank to use")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--analyze", action="store_true")
    args = ap.parse_args()

    a = music.analyze(args.track, args.tempo_scale)
    print(f"bpm {a['bpm']}  duration {a['duration']}s  beats {len(a['beats'])}  drops {a['drops']}")
    if args.analyze:
        return 0
    items: list[dict] = []
    for work in args.bank:
        items += bank.load(Path(work))
    if args.recipe and not items:
        print("--recipe needs --bank <chapter work folder>... with a built bank", file=sys.stderr)
        return 2
    frames = list(args.frames)
    if args.frames_json:   # a list of paths or of {"path", "tag"} objects
        frames += json.loads(Path(args.frames_json).read_text(encoding="utf-8"))
    if not frames and not items:
        print("no frames given", file=sys.stderr)
        return 2
    out = render.render(frames, args.track, args.out, args.length, args.tempo_scale, args.start,
                        args.drop, args.hero, args.preview,
                        progress=lambda p: print(f"\r{p * 100:3.0f}%", end="", flush=True),
                        style=args.style, recipe=args.recipe, items=items, point=args.point)
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
