"""Frame planning for an AMV short: which tagged frame goes in which cut.

`plan_aura` builds a character-aura montage: faces (>= 90%), arranged so the emotional
intensity follows the music (calm intro, rising build-up, strongest frames on the drop, a
cooldown after), with a few non-face frames (battle / scenery / aftermath) spaced between.
The renderer takes the result as-is; nothing here touches the manifest.
"""
from __future__ import annotations

from .render import Cut


def _target(cut: Cut, i_in_kind: int, n_in_kind: int) -> float:
    r = i_in_kind / max(1, n_in_kind - 1)
    return {"hook": 7.5, "intro": 4.5, "build": 5.0 + 3.0 * r, "drop": 9.0, "post": 8.0 - 3.0 * r}[cut.kind]


def plan_aura(items: list[dict], cuts: list[Cut], nonface_share: float = 0.1) -> tuple[list[dict], int]:
    """items: {"path", "tag", "ch", "id"}. Returns (frames in cut order with the hero last, hero_index)."""
    faces = [x for x in items if x["tag"].get("has_face")]
    other = [x for x in items if not x["tag"].get("has_face") and not x["tag"].get("text")
             and x["tag"].get("kind") in ("action", "scenery", "aftermath")]
    slots = [c for c in cuts]
    n = len(slots)
    n_non = min(len(other), max(1, round(n * nonface_share)))
    # spread the non-face frames: one in the intro, one late in the build, the rest in the cooldown
    cand = [i for i, c in enumerate(slots) if not c.hero]
    intro = [i for i in cand if slots[i].kind == "intro"]
    build = [i for i in cand if slots[i].kind == "build"]
    post = [i for i in cand if slots[i].kind == "post"]
    non_slots: list[int] = []
    for group, take in ((intro, 1), (build, 1), (post, n_non)):
        if group and len(non_slots) < n_non:
            step = max(1, len(group) // (take + 1))
            non_slots += [group[min(len(group) - 1, step * (k + 1))] for k in range(take)][: n_non - len(non_slots)]
    non_slots = sorted(set(non_slots))[:n_non]

    kind_counts: dict[str, int] = {}
    seen: dict[str, int] = {}
    for c in slots:
        kind_counts[c.kind] = kind_counts.get(c.kind, 0) + 1

    hero_face = max(faces, key=lambda x: (x["tag"]["intensity"], x["tag"].get("face_box", [0, 0, 0, 0])[3]), default=None)
    used: set[str] = set()
    if hero_face:
        used.add(hero_face["path"])
    chosen: list[dict | None] = [None] * n
    prev: dict | None = None

    def pick(pool: list[dict], want: float, prev_item: dict | None) -> dict | None:
        best, best_score = None, 1e9
        for x in pool:
            if x["path"] in used:
                continue
            score = abs(x["tag"]["intensity"] - want) + (1.5 if x["tag"].get("text") else 0.0)  # lettering is a last resort
            if prev_item and prev_item["ch"] == x["ch"]:
                score += 0.6                      # neighbours from the same chapter look alike
            if prev_item and prev_item.get("tag", {}).get("expression") == x["tag"].get("expression"):
                score += 0.8
            if score < best_score:
                best, best_score = x, score
        return best

    for i, c in enumerate(slots):
        seen[c.kind] = seen.get(c.kind, -1) + 1
        if c.hero:
            continue
        want = _target(c, seen[c.kind], kind_counts[c.kind])
        item = pick(other if i in non_slots else faces, want, prev)
        if item is None:
            item = pick(faces + other, want, prev)
        if item is not None:
            used.add(item["path"])
            chosen[i] = item
            prev = item
    frames = [{"path": x["path"], "tag": x["tag"]} for x, c in zip(chosen, slots) if x is not None and not c.hero]
    if hero_face:
        frames.append({"path": hero_face["path"], "tag": hero_face["tag"]})
        return frames, len(frames) - 1
    return frames, 0
