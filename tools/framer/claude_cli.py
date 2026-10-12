"""Thin wrapper around the local `claude` CLI (the user's Claude subscription login).

One call sends a single user message (text and images) in stream-json input mode
with no tools, and parses the stream-json output as it arrives into events for the
caller: panel mentions, streamed text, running token usage, rate-limit and retry
notices. A usage limit pauses the call until it resets, then continues the
interrupted session.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from config import NO_CONSOLE

MODELS = ("opus", "sonnet", "haiku")
RESUME_PROMPT = "You were interrupted by a usage limit. Continue exactly where you left off and finish the task."
LIMIT_FALLBACK_WAIT = timedelta(minutes=5)
LIMIT_MARGIN = timedelta(minutes=1)

_LIMIT_RE = re.compile(r"usage limit|limit reached|hit your limit|rate limit", re.I)
_PANEL_ID_RE = re.compile(r"P\d{3,}")
_USAGE_FIELDS = {"input_tokens": "input", "output_tokens": "output",
                 "cache_read_input_tokens": "cache_read", "cache_creation_input_tokens": "cache_write"}
_EPOCH_RE = re.compile(r"\|\s*(\d{10})\b")
_CLOCK_RE = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)", re.I)


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
    """Turns one call's stream-json events into on_event events. Every panel id of
    this call that first appears in the structured answer is reported as a `mention_event`."""

    def __init__(self, on_event: Callable[[dict], None], usage: dict, mention_event: str, panel_ids: set[str]):
        self.on_event, self.usage, self.mention_event, self.panel_ids = on_event, usage, mention_event, panel_ids
        self.session_id: str | None = None
        self.in_answer = False
        self.answer = ""
        self.mentioned: set[str] = set()

    def handle(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "stream_event":
            self._stream(event["event"])
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
                self._track_mentions(delta["partial_json"])
        elif kind == "message_delta":
            for field, key in _USAGE_FIELDS.items():
                self.usage[key] += event["usage"].get(field) or 0
            self.on_event({"type": "usage", "usage": dict(self.usage)})

    def _track_mentions(self, chunk: str) -> None:
        self.answer += chunk
        for panel_id in _PANEL_ID_RE.findall(self.answer):
            if panel_id in self.panel_ids and panel_id not in self.mentioned:
                self.mentioned.add(panel_id)
                self.on_event({"type": self.mention_event, "panel": panel_id})


def _run(content: list[dict], schema: dict, model: str, cwd: Path, session_id: str | None,
         on_proc: Callable[[subprocess.Popen], None], on_event: Callable[[dict], None],
         usage: dict, mention_event: str, panel_ids: set[str]) -> dict:
    cmd = [shutil.which("claude"), "-p", "--input-format", "stream-json", "--output-format", "stream-json",
           "--verbose", "--include-partial-messages", "--json-schema", json.dumps(schema),
           "--tools", "", "--model", model]
    if session_id:
        cmd += ["--resume", session_id]
    proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            **NO_CONSOLE)
    on_proc(proc)
    stderr: list[str] = []
    threading.Thread(target=lambda: stderr.append(proc.stderr.read()), daemon=True).start()
    proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": content}}))
    proc.stdin.close()

    reader, result = _StreamReader(on_event, usage, mention_event, panel_ids), None
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
    return result["structured_output"]


def call(content: list[dict], schema: dict, model: str, cwd: Path, *, usage: dict, mention_event: str,
         panel_ids: set[str], on_proc: Callable[[subprocess.Popen], None], on_event: Callable[[dict], None],
         on_limit: Callable[[datetime], None]) -> dict:
    """Run one call and return its structured answer, adding its tokens, cost and
    seconds to `usage`. A usage limit waits via `on_limit`, then continues the session.
    Only ids in `panel_ids` are reported as mentions."""
    session_id = None
    while True:
        started = time.monotonic()
        try:
            return _run(content, schema, model, cwd, session_id, on_proc, on_event, usage, mention_event,
                        panel_ids)
        except UsageLimit as limit:
            on_limit(limit.resume_at)
            if limit.session_id:
                session_id, content = limit.session_id, [{"type": "text", "text": RESUME_PROMPT}]
        finally:
            usage["seconds"] = round(usage["seconds"] + time.monotonic() - started, 1)
            usage["cost_usd"] = round(usage["cost_usd"], 4)
