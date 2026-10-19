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
        "style": ("Comedic: a funny, sarcastic best friend retelling this chapter, so the viewer is entertained "
                  "all the way through. Be openly funny, not just light: roughly every second or third block "
                  "that is not serious carries a joke, and a chapter with no jokes is a failure. Mix these, and "
                  "never use the same kind twice in a row: (1) puns and wordplay on what is on screen (a sword "
                  "fight 'cuts both ways', a lazy villain 'phones in' his evil plan); (2) masked swearing, written the way it should be spoken, with no dashes or asterisks: minced oaths and "
                  "bleeped words such as 'what the fudge', 'son of a biscuit', 'holy shiitake mushrooms', 'you "
                  "absolute muppet', 'that little bleep', 'he called him a big bleeping idiot', or a mock-polite "
                  "insult like 'you absolute walnut'; never a real slur or anything aimed at a group of people; "
                  "(3) quick, punchy asides: sarcasm, exaggerated understatement ('this went well'), "
                  "ironic contrast between what a character believes and what happens, a deadpan one-liner "
                  "after a dramatic beat, a mock-serious comparison, a roast of an obviously dumb decision; "
                  "(4) rule-of-three setups with a twist in the last item. Put the punchline at the END of the "
                  "sentence and keep each joke to one short sentence. Give such blocks the \"comic\" pace. "
                  "Never joke in a truly sad, tragic or frightening scene, a death or an emotional peak: play "
                  "those straight (calm or sad pace) and let the humour come back in the next light moment. "
                  "Jokes are always about the characters, the situation or the genre's clichés, never at the "
                  "plot's expense: every plot fact must stay accurate."),
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
