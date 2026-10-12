"""Script engine that drives the local `claude` CLI (the user's Claude subscription
login) instead of the Gemini API.

One chapter is two calls in the chapter's panel folder: pass 1 reads every panel
and returns a factual breakdown, pass 2 resumes that session and writes the
narration blocks from the breakdown alone. Each call's stream-json output is
parsed as it arrives into events (panel reads, streamed text, token usage) for
the caller. Progress and token totals are saved after each pass, and the breakdown
then feeds the project's story-so-far summary and character sheet.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from atomic_io import atomic_write_json, read_json_with_backup_fallback
from config import NO_CONSOLE

MODELS = ("opus", "sonnet", "haiku")
READ_BATCH = 8
RESUME_PROMPT = "You were interrupted by a usage limit. Continue exactly where you left off and finish the task."
LIMIT_FALLBACK_WAIT = timedelta(minutes=5)
LIMIT_MARGIN = timedelta(minutes=1)

_LIMIT_RE = re.compile(r"usage limit|limit reached|hit your limit|rate limit", re.I)
_PANEL_ID_RE = re.compile(r"P\d{3,}")
_USAGE_FIELDS = {"input_tokens": "input", "output_tokens": "output",
                 "cache_read_input_tokens": "cache_read", "cache_creation_input_tokens": "cache_write"}
_EPOCH_RE = re.compile(r"\|\s*(\d{10})\b")
_CLOCK_RE = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)", re.I)

_STRINGS = {"type": "array", "items": {"type": "string"}}


def _named(key: str) -> dict:
    return {"type": "array", "items": {
        "type": "object", "required": ["name", key],
        "properties": {"name": {"type": "string"}, key: {"type": "string"}}}}


BREAKDOWN_SCHEMA = {
    "type": "object",
    "required": ["chapter_summary", "characters", "scenes", "uncertain"],
    "properties": {
        "chapter_summary": {"type": "string"},
        "characters": _named("description"),
        "scenes": {"type": "array", "items": {
            "type": "object",
            "required": ["panel_ids", "location", "characters_present", "what_happens", "wants", "objects"],
            "properties": {
                "panel_ids": _STRINGS,
                "location": {"type": "string"},
                "characters_present": _STRINGS,
                "what_happens": {"type": "string"},
                "wants": _named("wants"),
                "objects": {"type": "array", "items": {
                    "type": "object", "required": ["name_in_dialogue", "role"],
                    "properties": {"name_in_dialogue": {"type": "string"}, "role": {"type": "string"}}}},
            }}},
        "uncertain": {"type": "array", "items": {
            "type": "object", "required": ["panel_ids", "note"],
            "properties": {"panel_ids": _STRINGS, "note": {"type": "string"}}}},
    },
}

NARRATION_SCHEMA = {
    "type": "object",
    "required": ["blocks"],
    "properties": {"blocks": {"type": "array", "items": {
        "type": "object", "required": ["panel_ids", "narration"],
        "properties": {"panel_ids": _STRINGS, "narration": {"type": "string"}}}}},
}


class ClaudeError(RuntimeError):
    pass


class UsageLimit(ClaudeError):
    def __init__(self, resume_at: datetime, session_id: str | None):
        super().__init__(f"Usage limit reached; resumes at {resume_at:%H:%M}")
        self.resume_at, self.session_id = resume_at, session_id


def cli_status() -> dict:
    exe = shutil.which("claude")
    if not exe:
        return {"ready": False, "message": "Claude Code is not installed. Install it from "
                "https://claude.com/claude-code, then reload this page."}
    try:
        proc = subprocess.run([exe, "auth", "status"], capture_output=True, text=True,
                              encoding="utf-8", timeout=30, **NO_CONSOLE)
        logged_in = bool(json.loads(proc.stdout).get("loggedIn"))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        logged_in = False
    if not logged_in:
        return {"ready": False, "message": "Claude Code is installed but not logged in. Open a "
                "terminal, run `claude auth login`, sign in with your Claude account, then reload."}
    return {"ready": True, "message": "Claude Code is installed and logged in."}


def _reset_time(text: str) -> datetime:
    now = datetime.now().astimezone()
    if epoch := _EPOCH_RE.search(text):
        return datetime.fromtimestamp(int(epoch[1])).astimezone()
    if clock := _CLOCK_RE.search(text):
        hour = int(clock[1]) % 12 + (12 if clock[3].lower() == "pm" else 0)
        at = now.replace(hour=hour, minute=int(clock[2] or 0), second=0, microsecond=0)
        return at if at > now else at + timedelta(days=1)
    return now + LIMIT_FALLBACK_WAIT


def empty_usage() -> dict:
    return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "seconds": 0.0}


def sum_usage(items: list[dict]) -> dict:
    total = empty_usage()
    for item in items:
        for key in total:
            total[key] += item.get(key, 0)
    return total


class _StreamReader:
    """Turns one call's stream-json events into on_event events: panel reads,
    streamed text, running token usage, and rate-limit / retry notices."""

    def __init__(self, on_event: Callable[[dict], None], usage: dict):
        self.on_event, self.usage = on_event, usage
        self.session_id: str | None = None
        self.in_answer = False
        self.answer = ""
        self.covered = ""

    def handle(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "stream_event":
            self._stream(event["event"])
        elif kind == "assistant":
            for block in event["message"]["content"]:
                if block["type"] == "tool_use" and block["name"] == "Read":
                    self.on_event({"type": "read", "panel": Path(block["input"]["file_path"]).stem})
        elif kind == "rate_limit_event":
            status = event["rate_limit_info"].get("status")
            if status != "allowed":
                self.on_event({"type": "log", "line": f"Claude rate limit: {status}"})
        elif kind == "system" and event.get("subtype") == "init":
            self.session_id = event["session_id"]
        elif kind == "system" and event.get("subtype") == "api_retry":
            detail = {k: v for k, v in event.items() if k not in ("type", "subtype", "session_id", "uuid")}
            self.on_event({"type": "log", "line": f"Claude API retry: {json.dumps(detail, ensure_ascii=False)}"})

    def _stream(self, event: dict) -> None:
        kind = event["type"]
        if kind == "content_block_start":
            self.in_answer = event["content_block"].get("name") == "StructuredOutput"
        elif kind == "content_block_delta":
            delta = event["delta"]
            if delta["type"] == "text_delta":
                self.on_event({"type": "text", "text": delta["text"]})
            elif delta["type"] == "input_json_delta" and self.in_answer:
                self.on_event({"type": "text", "text": delta["partial_json"]})
                self._track_cover(delta["partial_json"])
        elif kind == "message_delta":
            for field, key in _USAGE_FIELDS.items():
                self.usage[key] += event["usage"].get(field) or 0
            self.on_event({"type": "usage", "usage": dict(self.usage)})

    def _track_cover(self, chunk: str) -> None:
        self.answer += chunk
        ids = _PANEL_ID_RE.findall(self.answer)
        if ids and ids[-1] > self.covered:
            self.covered = ids[-1]
            self.on_event({"type": "cover", "panel": self.covered})


def _call(prompt: str, schema: dict, model: str, cwd: Path, session_id: str | None,
          on_proc: Callable[[subprocess.Popen], None], on_event: Callable[[dict], None],
          usage: dict) -> dict:
    cmd = [shutil.which("claude"), "-p", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--json-schema", json.dumps(schema),
           "--allowedTools", "Read", "--model", model]
    if session_id:
        cmd += ["--resume", session_id]
    proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            **NO_CONSOLE)
    on_proc(proc)
    stderr: list[str] = []
    threading.Thread(target=lambda: stderr.append(proc.stderr.read()), daemon=True).start()
    proc.stdin.write(prompt)
    proc.stdin.close()

    reader, result = _StreamReader(on_event, usage), None
    for line in proc.stdout:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            result = event
        else:
            reader.handle(event)
    proc.wait()
    if result is None:
        raise ClaudeError("".join(stderr).strip() or f"claude exited with code {proc.returncode}")
    usage["cost_usd"] += result.get("total_cost_usd") or 0
    on_event({"type": "usage", "usage": dict(usage)})
    if result.get("is_error"):
        text = str(result.get("result") or "".join(stderr))
        if result.get("api_error_status") == 429 or _LIMIT_RE.search(text):
            raise UsageLimit(_reset_time(text) + LIMIT_MARGIN, reader.session_id)
        raise ClaudeError(text)
    if not result.get("structured_output"):
        raise ClaudeError("Claude returned no structured output.")
    return result


def load_memory(path: Path) -> dict:
    try:
        memory = read_json_with_backup_fallback(path)
    except (OSError, ValueError):
        memory = {}
    return {"chapters": memory.get("chapters", {}), "characters": memory.get("characters", {})}


def _story_so_far(memory: dict, ch_no: int) -> str:
    earlier = sorted((int(n), text) for n, text in memory["chapters"].items() if int(n) < ch_no)
    return "\n".join(f"Chapter {n}: {text}" for n, text in earlier) or "(this is the first chapter)"


def _character_sheet(memory: dict) -> str:
    return "\n".join(f"- {name}: {text}" for name, text in memory["characters"].items()) or "(none yet)"


def _breakdown_prompt(memory: dict, ch_no: int, brief: str, panel_ids: list[str]) -> str:
    return (
        f"You are preparing a recap of chapter {ch_no} of a manhwa. Its {len(panel_ids)} panels are the "
        f"image files {panel_ids[0]}.png to {panel_ids[-1]}.png in the current directory, in reading "
        f"order. Use the Read tool on EVERY panel, in order, before you answer, issuing the Read calls "
        f"for {READ_BATCH} panels at a time in a single turn.\n\n"
        f"Series brief:\n{brief.strip() or '(none)'}\n\n"
        f"Story so far:\n{_story_so_far(memory, ch_no)}\n\n"
        f"Character sheet:\n{_character_sheet(memory)}\n\n"
        "Return a factual breakdown of this chapter: a short chapter summary; every character who "
        "appears (updating their sheet entry); and, scene by scene with the panel ids it spans, the "
        "location, who is present, what happens, what each character wants, and any plot-relevant "
        "object (named exactly as the dialogue names it, or an empty name if dialogue never does). "
        "Never guess. Anything you cannot establish from the panels and the context above, such as a "
        "name, a relationship or what an object is, goes into `uncertain` with its panel ids."
    )


def _narration_prompt(style: str, panel_ids: list[str]) -> str:
    return (
        "Using only the facts in your breakdown, and without re-reading the panels, write the recap "
        f"narration for this chapter.\n\n{style}\n\n"
        "Return ordered blocks. Each block lists the panel ids it covers, in reading order, and the "
        "narration spoken over them (one to three sentences). Cover every panel with story content "
        f"({panel_ids[0]} to {panel_ids[-1]}).\n"
        "Narrate events and their meaning: what happens, why it matters, what characters want. Never "
        "describe what is visible in a panel. Do not resolve anything you flagged as uncertain. If the "
        "art leaves an object unclear, call it what the dialogue calls it, or leave it unnamed."
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_narration(state_path: Path) -> dict | None:
    """{blocks, saved_at} once pass 2 has completed for the chapter, else None."""
    try:
        return read_json_with_backup_fallback(state_path).get("narration")
    except (OSError, ValueError):
        return None


def read_usage(state_path: Path) -> dict | None:
    """{pass1, pass2, total} token usage saved for the chapter, else None."""
    try:
        return read_json_with_backup_fallback(state_path).get("usage")
    except (OSError, ValueError):
        return None


def run_chapter(*, panels_dir: Path, state_path: Path, memory_path: Path, ch_no: int, brief: str,
                style: str, models: dict, panel_ids: list[str],
                on_event: Callable[[dict], None],
                on_limit: Callable[[datetime], None],
                on_proc: Callable[[subprocess.Popen], None]) -> list[dict]:
    """Run (or resume) both passes for one chapter and return its narration blocks.
    `on_event` receives a step event as each stage starts and the stream events of
    each call, all tagged with their pass (None for the story update)."""
    try:
        state = read_json_with_backup_fallback(state_path)
    except (OSError, ValueError):
        state = {}
    if state.get("memory_applied"):
        state = {}
    memory = load_memory(memory_path)

    def call(number, prompt, schema, model, session_id=None):
        usage = empty_usage()
        while True:
            started = time.monotonic()
            try:
                return _call(prompt, schema, model, panels_dir, session_id, on_proc,
                             lambda event: on_event({**event, "pass": number}), usage), usage
            except UsageLimit as limit:
                on_limit(limit.resume_at)
                if limit.session_id:
                    session_id, prompt = limit.session_id, RESUME_PROMPT
            finally:
                usage["seconds"] = round(usage["seconds"] + time.monotonic() - started, 1)
                usage["cost_usd"] = round(usage["cost_usd"], 4)

    if "breakdown" not in state:
        on_event({"type": "step", "pass": 1, "label": "Reading the panels"})
        out, usage = call(1, _breakdown_prompt(memory, ch_no, brief, panel_ids), BREAKDOWN_SCHEMA,
                          models["comprehension"])
        state.update(session_id=out["session_id"], breakdown=out["structured_output"],
                     usage={"pass1": usage})
        atomic_write_json(state_path, state)

    if "narration" not in state:
        on_event({"type": "step", "pass": 2, "label": "Writing the narration"})
        out, usage = call(2, _narration_prompt(style, panel_ids), NARRATION_SCHEMA, models["narration"],
                          state["session_id"])
        known = set(panel_ids)
        blocks = [{"panel_ids": ids, "narration": block["narration"].strip()}
                  for block in out["structured_output"]["blocks"]
                  if (ids := [i for i in block["panel_ids"] if i in known]) and block["narration"].strip()]
        state["narration"] = {"blocks": blocks, "saved_at": _now()}
        passes = {**state.get("usage", {}), "pass2": usage}
        state["usage"] = {**passes, "total": sum_usage(list(passes.values()))}
        atomic_write_json(state_path, state)

    on_event({"type": "step", "pass": None, "label": "Updating the story so far"})
    breakdown = state["breakdown"]
    memory["chapters"][str(ch_no)] = breakdown["chapter_summary"]
    memory["characters"].update({c["name"]: c["description"] for c in breakdown["characters"]})
    atomic_write_json(memory_path, memory)
    state["memory_applied"] = True
    atomic_write_json(state_path, state)
    return state["narration"]["blocks"]
