"""Proactive suggestions: while no pipeline runs and Claude has usage left, tell the user (on Telegram) which project
most needs content and what to make next. A suggestion is not repeated for the same chapters until a few hours
have passed; a different project or range is a new situation and is sent at once."""
from __future__ import annotations

import logging
import threading
import time

import claude_cli
import claude_usage
import projects
from publishing import store, telegram
from services import cards, planning
from services.batching import active_chapters, pipeline_running
from services.planning import Plan

log = logging.getLogger(__name__)

CHECK_EVERY_S = 300
REPEAT_AFTER_S = 4 * 3600
SUGGEST_BELOW_DAYS = 14          # a project with more video than this ready needs nothing yet
FILE = "advisor.json"


def all_plans() -> list[Plan]:
    return [planning.build(p["slug"], active_chapters(p["slug"])) for p in projects.list_projects()]


def claude_has_room(slug: str) -> bool:
    """True while both usage windows (as the CLI's rate-limit events last reported them) are under the limits at
    which the project's own pipeline would stop and wait; a window whose reset has passed counts as empty."""
    limits = projects.load(slug)["settings"]["batch"]
    usage = claude_usage.snapshot()
    return all(usage[w]["percent"] < limits[f"{w}_pct"] for w in claude_usage.WINDOWS if w in usage)


def suggest() -> None:
    if not telegram.state()["paired"] or pipeline_running():
        return
    plan = planning.neediest(all_plans())
    if plan is None or plan.buffer_days >= SUGGEST_BELOW_DAYS:
        return
    situation = [plan.slug, plan.suggestion[0], plan.suggestion[-1]]
    last = store.read(FILE, {})
    if last.get("situation") == situation and time.time() - last["at"] < REPEAT_AFTER_S:
        return
    if not claude_has_room(plan.slug) or not claude_cli.cli_status()["ready"]:
        return
    store.write(FILE, {"situation": situation, "at": time.time()})
    cards.send_suggestion(plan)


def _loop() -> None:
    while True:
        try:
            suggest()
        except Exception:  # noqa: BLE001 - the advisor must outlive any single failure
            log.exception("The suggestion check failed.")
        time.sleep(CHECK_EVERY_S)


def start() -> None:
    threading.Thread(target=_loop, daemon=True).start()
