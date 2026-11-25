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
    ap.add_argument("--backdrop", help="mood layers, e.g. \"mono,rain:0.8@0-12,film@12-\" (mono sepia film rain ripple vignette leak dust)")
    ap.add_argument("--lyrics", help="LRC file of the track's lyrics (recipe lyrics)")
    ap.add_argument("--no-outro", action="store_true", help="no closing message about the playlist at the end")
    ap.add_argument("--look", help="quote look: ember scroll royal neon blood frost (default: the one for --series / ember)")
    ap.add_argument("--font", default="", help="text font: anton montserrat oswald poppins archivo barlow bebas cormorant")
    ap.add_argument("--quote", help="quote id in --quotes, or the quote text itself (recipe quote)")
    ap.add_argument("--quotes", help="quotes.json of the project (recipe quote)")
    ap.add_argument("--mood", help="mood of an ad-hoc --quote text: calm sad tense action reveal cold nostalgic")
    ap.add_argument("--series", default="", help="series name (helps find the protagonist)")
    ap.add_argument("--character", help="whose frames to use (recipe quote); default: the most frequent face in the bank")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--analyze", action="store_true")
    args = ap.parse_args()

    if args.recipe == "lyrics":
        from . import lyrics_short
        if not args.lyrics:
            print("--recipe lyrics needs --lyrics <file.lrc>", file=sys.stderr)
            return 2
        out = lyrics_short.make(args.track, args.bank, args.lyrics, args.out, args.mood or "tense", args.character, args.length,
                                args.preview, args.backdrop, args.tempo_scale, series=args.series, font=args.font,
                                progress=lambda p: print(f"\r{p * 100:3.0f}%", end="", flush=True))
        print(f"\n-> {out}")
        return 0
    if args.recipe == "quote":
        from . import quote_short, quotes
        q = next((x for x in quotes.load(args.quotes) if x["id"] == args.quote), None) if args.quotes else None
        if q is None and args.quote:
            q = quotes.normalise({"text": args.quote, "mood": args.mood or "calm"})
        if q is None:
            print("--recipe quote needs --quote <id> --quotes <file>, or --quote \"text\" [--mood m]", file=sys.stderr)
            return 2
        out = quote_short.make(args.track, args.bank, q, args.out, args.character, args.length, args.preview, args.backdrop,
                               args.tempo_scale, series=args.series, look=args.look, outro=not args.no_outro,
                               progress=lambda p: print(f"\r{p * 100:3.0f}%", end="", flush=True))
        print(f"\n-> {out}")
        return 0
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
                        style=args.style, recipe=args.recipe, items=items, point=args.point, backdrop=args.backdrop)
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
