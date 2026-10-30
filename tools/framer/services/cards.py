"""The Telegram planner's cards: a failed chapter, a merge proposal, a suggestion to make more. What a card's buttons
act on is kept in publishing/cards.json, so a button still works after the app restarted."""
from __future__ import annotations

import uuid

import projects
from publishing import store, telegram
from publishing.labels import chapter_label
from services import planning
from services.planning import Plan

FILE = "cards.json"
KEPT = 60
ERROR_SUMMARY = 300


def add(kind: str, **fields) -> str:
    card_id = uuid.uuid4().hex[:10]
    with store.LOCK:
        cards = store.read(FILE, {})
        cards[card_id] = {"kind": kind, **fields}
        store.write(FILE, dict(list(cards.items())[-KEPT:]))
    return card_id


def get(card_id: str) -> dict | None:
    return store.read(FILE, {}).get(card_id)


def discard(card_id: str) -> None:
    with store.LOCK:
        cards = store.read(FILE, {})
        if cards.pop(card_id, None) is not None:
            store.write(FILE, cards)


def minutes_text(minutes: float) -> str:
    return f"{minutes:.0f} min" if minutes >= 100 else f"{minutes:.1f} min"


def days_text(days: float) -> str:
    return f"{days:g} day" + ("" if days == 1 else "s")


def announce_failure(slug: str, n: int, stage: str, error: str, shorts: bool) -> None:
    card = add("failure", slug=slug, n=n, shorts=shorts)
    summary = (error.strip().splitlines() or [""])[0][:ERROR_SUMMARY]
    telegram.notify(f"{projects.load(slug)['name']}: chapter {n} failed at {stage}.\n{summary}",
                    [[("Retry from failed stage", f"retry:{card}"), ("Redo all stages", f"redo:{card}")],
                     [("Skip chapter", f"drop:{card}"), ("Show full error", f"error:{card}")]])


def send_merge_proposal(plan: Plan, numbers: list[int]) -> None:
    card = add("merge", slug=plan.slug, chapters=numbers)
    total = plan.length_of(numbers)
    telegram.notify(f"{plan.name}: ready to merge into one video\nChapters {chapter_label(numbers)} ({len(numbers)})\n"
                    f"Total {minutes_text(total)} (at least {plan.min_minutes} min per upload"
                    f"{', this is under it' if total < plan.min_minutes else ''})",
                    [[("Merge", f"merge:{card}"), ("Change range", f"range:{card}"), ("Later", f"later:{card}")]])


def propose_merge(slug: str) -> None:
    """After a pipeline finished: offer to merge the project's next finished chapters into one upload."""
    plan = planning.build(slug, set())
    if not plan.has_channel or not plan.unmerged:
        return
    numbers = planning.merge_proposal(plan)
    if numbers and plan.length_of(numbers) >= plan.min_minutes:
        send_merge_proposal(plan, numbers)
    else:
        telegram.notify(f"{plan.name}: {minutes_text(plan.length_of(sorted(plan.unmerged)))} of finished chapters "
                        f"({chapter_label(sorted(plan.unmerged))}) is not enough for an upload of "
                        f"{plan.min_minutes} min, or a chapter between them is missing.")


def send_suggestion(plan: Plan) -> None:
    card = add("suggestion", slug=plan.slug, chapters=plan.suggestion)
    telegram.notify(f"{plan.name} needs content: {days_text(plan.buffer_days)} of video ready "
                    f"({minutes_text(plan.buffer_minutes)}).\nSuggested: chapters {chapter_label(plan.suggestion)}, "
                    f"about {minutes_text(plan.suggestion_minutes)}.",
                    [[("Run", f"run:{card}"), ("Later", f"later:{card}")]])
