"""The narration tones a chapter can be written in: how each one shapes the script's voice, and how
its voice is delivered by the text-to-speech engines.

A tone changes how a recap is worded and spoken, never what happens in it or how long it runs.
"""
from __future__ import annotations

from typing import NamedTuple

DEFAULT_TONE = "dramatic"


class Delivery(NamedTuple):
    """A speaking-rate factor (above 1 is faster) applied on top of each line's own pace (see pacing.py)."""
    speed: float


TONES = {
    "epic": {
        "label": "Epic",
        "style": ("Epic: weight and scale. Let sentences roll long at moments of power, arrival or resolve "
                  "and cut short and hard on impact. Use elevated, concrete words (vow, ruin, reckoning) only "
                  "where the notes show high stakes, and plain wording elsewhere. Frame turning points as "
                  "moments that change the course of things. No jokes."),
        "delivery": Delivery(0.96),
    },
    "serious": {
        "label": "Serious",
        "style": ("Serious: grounded, measured, matter-of-fact. Plain precise words in mostly medium-length "
                  "declarative sentences, no exclamation marks, no flourish. Let tension and feeling come "
                  "through what characters do and say, stated with restraint. Humour only when a character "
                  "plainly says something funny, and then deadpan."),
        "delivery": Delivery(0.98),
    },
    "dramatic": {
        "label": "Dramatic",
        "style": ("Dramatic: emotionally charged and close to the character. Short sentences and fragments at "
                  "moments of shock or pain, a longer sentence that lingers on a feeling, contrast between "
                  "what a character hoped and what came. Dwell on what they feel at the emotional peaks the "
                  "notes mark. Intense but not melodramatic: no stacked exclamation marks."),
        "delivery": Delivery(0.95),
    },
    "comedic": {
        "label": "Comedic",
        "style": ("Comedic: light, wry, conversational. Deadpan understatement, ironic contrast between what "
                  "a character believes and what happens, punchlines placed at the end of a sentence, playful "
                  "word choice. Joke only about things the notes show, never at the plot's expense; when a "
                  "scene is truly sad or frightening, play it straight."),
        "delivery": Delivery(1.06),
    },
}

_LEGACY = {"comedy": "comedic"}


def normalize(tone: str | None) -> str | None:
    """The canonical tone id for `tone` (including the pre-rename 'comedy'), else None."""
    tone = _LEGACY.get(tone, tone)
    return tone if tone in TONES else None


def style_instruction(tone: str | None) -> str:
    """The prompt text that sets the narration's voice for `tone`."""
    return (TONES[normalize(tone) or DEFAULT_TONE]["style"] +
            " The tone changes only how things are said: it adds no events, exaggerates nothing and makes "
            "the narration neither longer nor shorter.")


NEUTRAL = Delivery(1.0)


def delivery(tone: str | None) -> Delivery:
    """The voice adjustments for `tone`; none for a chapter that has no tone on record."""
    tone = normalize(tone)
    return TONES[tone]["delivery"] if tone else NEUTRAL
