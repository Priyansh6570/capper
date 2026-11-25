"""Looks for the quote short: a look is the whole design of the words and the overlay around them, so every series has a
face of its own instead of one template for all.

    ember   hot embers and light shafts, bold condensed type centred in the lower third           (action, rebirth)
    scroll  an inked scroll: serif type on the left beside a growing line, blossom petals, corner marks   (martial arts, wuxia)
    royal   gold and cinema bars: capitals with a rule above, golden dust and bokeh                  (nobles, empires, magic)
    neon    a screen: tall capitals that slam in, scanlines, a glitch now and then, corner brackets   (systems, tech, games)
    blood   heavy black type that slams in, embers, smoke and a dark vignette, no border             (revenge, dark fantasy)
    frost   cold and quiet: thin capitals mid-screen, snow, mist and a hairline frame                  (shadows, ice, loneliness)

A look only describes; `amv.styles.quote` builds the layers and `amv.cinema.CinemaText` draws the words from it.
`resolve(setting, slug)` gives the look a project uses: its own choice (`settings.shorts.look`), else the table below, else
one picked from the project's name so a series always keeps the same one. Nothing here touches a file.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Look:
    name: str
    label: str
    font: str
    align: str = "center"                 # center | left
    center_y: float = 0.63                # vertical centre of the block (fraction of the height)
    max_width: int = 860
    size: int = 108
    margin_x: int = 72                    # left looks: the block starts here
    upper: bool = False
    entrance: str = "rise"                # rise (out of a blur) | slam (drops in big, with a flash) | track (slides in, fades up)
    rule: str = "none"                    # bar (a line beside a left block) | top (a short line above a centred one) | none
    accent: tuple | None = None           # BGR; None = the quote's mood colour. A quote's own colour always wins
    layers: tuple = ()                    # (kind, strength, scope) scope: "hold" = while the quote is spoken, "all" = the whole short
    border: str = "hairline"              # hairline | brackets | bars | none
    grain: float = 0.6


LOOKS: dict[str, Look] = {look.name: look for look in (
    Look("ember", "Ember", "anton", center_y=0.63, size=108, accent=None, border="hairline",
         layers=(("aurora", 0.55, "hold"), ("rays", 0.6, "hold"), ("bokeh", 1.0, "all"), ("embers", 1.0, "hold"), ("mist", 0.4, "hold"))),
    Look("scroll", "Scroll", "cormorant", align="left", center_y=0.60, max_width=700, size=100, margin_x=104, entrance="track",
         rule="bar", accent=(205, 170, 255), border="brackets",
         layers=(("petals", 1.0, "all"), ("mist", 0.55, "hold"), ("rays", 0.4, "hold"), ("bokeh", 0.45, "all"), ("dust", 0.5, "hold"))),
    Look("royal", "Royal", "cinzel", center_y=0.62, max_width=840, size=88, upper=True, entrance="rise", rule="top",
         accent=(90, 190, 238), border="bars",
         layers=(("dust", 0.9, "all"), ("bokeh", 0.8, "all"), ("rays", 0.7, "hold"), ("aurora", 0.3, "hold"))),
    Look("neon", "Neon", "bebas", center_y=0.60, max_width=880, size=132, upper=True, entrance="slam",
         accent=(255, 225, 60), border="brackets",
         layers=(("scan", 1.0, "all"), ("aurora", 0.5, "hold"), ("bokeh", 0.45, "all"))),
    Look("blood", "Blood", "archivo", center_y=0.64, max_width=880, size=92, upper=True, entrance="slam",
         accent=(60, 50, 235), border="none", grain=0.95,
         layers=(("vignette", 0.8, "all"), ("embers", 1.2, "hold"), ("mist", 0.6, "hold"), ("rays", 0.5, "hold"))),
    Look("frost", "Frost", "oswald", center_y=0.54, max_width=820, size=96, upper=True, entrance="track", rule="top",
         accent=(255, 225, 170), border="hairline",
         layers=(("snow", 1.0, "all"), ("mist", 0.6, "hold"), ("bokeh", 0.4, "all"))),
)}
DEFAULT = "ember"

# what each known series uses; any other project gets one from its name (stable), or its own choice
BY_PROJECT = {
    "return-of-the-mount-hua-sect": "scroll",
    "my-disciples-are-all-big-villains-2": "ember",
    "magic-emperor": "royal",
    "the-count-s-youngest-son-is-a-player": "royal",
    "nano-machine": "neon",
    "the-world-s-best-engineer-2": "neon",
    "revenge-of-the-iron-blooded-sword-hound": "blood",
    "shadow-slave": "frost",
}


def resolve(setting: str | None, slug: str | None = None) -> str:
    """A look name: the project's choice when it names one, else the series' table entry, else one from the slug."""
    if setting in LOOKS:
        return setting
    if slug in BY_PROJECT:
        return BY_PROJECT[slug]
    if slug:
        names = list(LOOKS)
        return names[int(hashlib.sha1(slug.encode()).hexdigest(), 16) % len(names)]
    return DEFAULT


def get(name: str | None) -> Look:
    return LOOKS.get(name or "", LOOKS[DEFAULT])
