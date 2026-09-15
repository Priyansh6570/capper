"""Scaffold a new series brief and mark it active.

This is a helper, NOT a pipeline stage: it does not implement `run(m) -> m` and
the orchestrator never calls it. Run it once per series, then fill in the
generated `story.md` by hand:

    python tools/new_series.py --name "tales-of-demons"

It writes the active series name to `series/active.txt` and creates
`series/<name>/story.md` with labeled, empty sections. Stage 5 reads the active
brief and trusts it over machine-vision for character names, roles and setting.

Re-running for an existing series only flips it to active; an existing story.md
is left untouched so you never lose what you wrote (use --force to overwrite).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python tools/new_series.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ROOT  # noqa: E402

TEMPLATE = """\
# {name}

## Main character
<!-- Name, age/role, defining traits. e.g. "Li Chi - a stoic young swordsman..." -->

## Relationships
<!-- Key people and how they relate. e.g. "Yuoner - childhood friend who loves him" -->

## Setting
<!-- World, era, places. e.g. "Warring States period; the Li family village near Yong Shang" -->

## Premise
<!-- The overall situation driving the story. -->

## Tone
<!-- The mood the recap should carry. e.g. "grave, tragic, war-weary" -->
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a new series brief.")
    parser.add_argument("--name", required=True, help="series name (folder name)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing story.md",
    )
    args = parser.parse_args()

    name = args.name.strip()
    if not name:
        raise SystemExit("--name must not be empty")

    series_dir = ROOT / "series" / name
    series_dir.mkdir(parents=True, exist_ok=True)

    story_path = series_dir / "story.md"
    if story_path.exists() and not args.force:
        print(f"kept existing {story_path} (use --force to overwrite)")
    else:
        story_path.write_text(TEMPLATE.format(name=name), encoding="utf-8")
        print(f"wrote template {story_path}")

    active_path = ROOT / "series" / "active.txt"
    active_path.parent.mkdir(parents=True, exist_ok=True)
    active_path.write_text(name + "\n", encoding="utf-8")
    print(f"active series set to '{name}' ({active_path})")
    print(f"now fill in {story_path}")


if __name__ == "__main__":
    main()
