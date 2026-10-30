"""What the Telegram bot may ask of the app: start the pipeline or shorts for a project, report on the running ones,
show each project's upload plan and carry out the buttons of the planner's cards (see cards.py)."""
from __future__ import annotations

import calendar

import batch
import projects
from publishing import telegram
from publishing.labels import chapter_label, parse_chapters
from publishing.telegram import Controls, Reaction
from services import advisor, autopublish, cards, planning
from services.batching import BATCHES, SHORTS_BATCHES, job_summary, start_batch, start_shorts
from services.cards import days_text, minutes_text
from services.common import Refused

LINE_LIMIT = 25
LOG_TAIL = 40


def find_project(text: str) -> dict:
    """The project called `text` (its name or slug, or a part of its name that only one project has)."""
    wanted, listed = text.strip().casefold(), projects.list_projects()
    exact = [p for p in listed if wanted in (p["slug"].casefold(), p["name"].casefold())]
    found = exact or [p for p in listed if wanted in p["name"].casefold()]
    if not found:
        raise ValueError(f"There is no project called '{text}'.")
    if len(found) > 1:
        raise ValueError("Several projects match: " + ", ".join(p["name"] for p in found) + ".")
    return found[0]


def _project_with(project_text: str, numbers: list[int]) -> dict:
    """The project called `project_text`, which must have every chapter of `numbers`."""
    found = find_project(project_text)
    known = {c["n"] for c in projects.load(found["slug"])["chapters"]}
    if missing := [n for n in numbers if n not in known]:
        raise ValueError(f"{found['name']} has no chapter {chapter_label(missing)}.")
    return found


def run(project_text: str, numbers: list[int]) -> str:
    found = _project_with(project_text, numbers)
    try:
        start_batch(found["slug"], {n: list(batch.LANE_STAGES) for n in numbers})
    except Refused as e:
        raise ValueError(str(e)) from e
    started = f"Started {found['name']}, chapters {chapter_label(numbers)}."
    if projects.load(found["slug"])["settings"]["youtube"]["channel"]:
        return f"{started} When they are done you are asked whether to merge them."
    return f"{started} The project has no YouTube channel, so its video will not be queued."


def shorts(project_text: str, numbers: list[int]) -> str:
    found = _project_with(project_text, numbers)
    try:
        started, skipped = start_shorts(found["slug"], numbers)
    except Refused as e:
        raise ValueError(str(e)) from e
    text = f"Making shorts for {found['name']}, chapters {chapter_label(started)}."
    return text + "".join(f"\nSkipped chapter {s['n']}: {s['reason']}." for s in skipped)


def project_names() -> list[str]:
    return [p["name"] for p in projects.list_projects()]


def _chapter_line(chapter: dict) -> str:
    progress = (chapter.get("job") or {}).get("progress")
    state = chapter["stage"] or chapter["state"]
    detail = f" - {chapter['error']}" if chapter.get("error") else f" {progress:.0%}" if progress else ""
    return f"chapter {chapter['n']}: {state} ({chapter['state']}){detail}"


def status() -> str:
    lines = []
    for label, running in (("Pipeline", BATCHES), ("Shorts", SHORTS_BATCHES)):
        for slug, current in running.items():
            if not current.active:
                continue
            view = current.summary(job_summary)
            done = sum(c["state"] == "done" for c in view["chapters"])
            lines.append(f"{projects.load(slug)['name']} - {label}, {done} of {len(view['chapters'])} chapters done"
                         + (" (held at the Claude usage limit)" if view.get("hold") else ""))
            lines += [f"  {_chapter_line(c)}" for c in view["chapters"] if c["state"] != "done"]
    shown = lines[:LINE_LIMIT] + ([f"...and {len(lines) - LINE_LIMIT} more lines"] if len(lines) > LINE_LIMIT else [])
    return "\n".join(shown) or "No pipeline is running."


def _upload_days(days: list[int]) -> str:
    return "daily" if len(days) == 7 else ", ".join(calendar.day_abbr[d] for d in days)


def _plan_block(plan: planning.Plan) -> str:
    if not plan.has_channel:
        return f"{plan.name}\n  no YouTube channel is set in its settings"
    upcoming = (f"chapters {chapter_label(plan.suggestion)}, about {minutes_text(plan.suggestion_minutes)}"
                if plan.suggestion else "no chapters left to make")
    return (f"{plan.name}\n  Uploads {_upload_days(plan.days)}, at least {plan.min_minutes} min each "
            f"(a chapter is {minutes_text(plan.avg_minutes)} on average, {plan.per_upload} per upload)\n"
            f"  Next upload: {plan.next_upload:%a %d %b}\n"
            f"  Buffer: {days_text(plan.buffer_days)} - {minutes_text(plan.buffer_minutes)} ready "
            f"({len(plan.queued)} queued, {len(plan.merged)} merged, {len(plan.unmerged)} finished)\n"
            f"  Next to make: {upcoming}")


def plan_text() -> str:
    plans = sorted(advisor.all_plans(), key=lambda p: (not p.has_channel, p.buffer_days, p.name))
    return "\n\n".join(map(_plan_block, plans)) or "There are no projects yet."


def _batch_of(card: dict) -> batch.Batch:
    current = (SHORTS_BATCHES if card["shorts"] else BATCHES).get(card["slug"])
    if current is None:
        raise ValueError("This project's batch is gone.")
    return current


def _retry(from_start: bool):
    def handler(card_id: str, card: dict) -> Reaction:
        _batch_of(card).retry(card["n"], from_start=from_start)
        cards.discard(card_id)
        return Reaction("Starting over." if from_start else "Retrying the stage that failed.")
    return handler


def _drop(card_id: str, card: dict) -> Reaction:
    _batch_of(card).skip(card["n"])
    cards.discard(card_id)
    return Reaction("Chapter skipped.")


def _error(card_id: str, card: dict) -> Reaction:
    chapter = next(c for c in _batch_of(card).view(job_summary)["chapters"] if c["n"] == card["n"])
    telegram.send(f"Chapter {card['n']}: {chapter['error'] or 'no error is recorded'}\n\n"
                  + "\n".join(chapter["log"][-LOG_TAIL:]))
    return Reaction("The full error is below.", keep_buttons=True)


def _run_suggestion(card_id: str, card: dict) -> Reaction:
    telegram.send(run(card["slug"], card["chapters"]))
    cards.discard(card_id)
    return Reaction("Started.")


def _later(card_id: str, card: dict) -> Reaction:
    cards.discard(card_id)
    return Reaction("Not now.")


def _merge(card_id: str, card: dict) -> Reaction:
    planning.check_range(planning.build(card["slug"], set()), card["chapters"])
    cards.discard(card_id)
    autopublish.publish_in_background(card["slug"], card["chapters"])
    return Reaction("Merging; the upload approval follows here.")


def _ask_range(slug: str, problem: str = "") -> None:
    def received(text: str) -> None:
        try:
            numbers = parse_chapters(text)
            plan = planning.build(slug, set())
            planning.check_range(plan, numbers)
        except ValueError as e:
            _ask_range(slug, f"{e} ")
            return
        cards.send_merge_proposal(plan, numbers)

    telegram.ask(f"{problem}Which chapters should be merged? For example 5-9.", received)


def _change_range(card_id: str, card: dict) -> Reaction:
    cards.discard(card_id)
    _ask_range(card["slug"])
    return Reaction("Send the chapters.")


ACTIONS = {"retry": _retry(False), "redo": _retry(True), "drop": _drop, "error": _error, "run": _run_suggestion,
           "later": _later, "merge": _merge, "range": _change_range}


def act(action: str, card_id: str) -> Reaction:
    card = cards.get(card_id)
    if card is None or action not in ACTIONS:
        return Reaction("That card has expired.")
    try:
        return ACTIONS[action](card_id, card)
    except ValueError as e:
        return Reaction(str(e))


CONTROLS = Controls(run=run, shorts=shorts, plan=plan_text, status=status, projects=project_names, act=act)
