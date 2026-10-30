"""The title, description and tags of a short, written by Claude from the short's script."""
from __future__ import annotations

import claude_cli
from publishing import store
from publishing.labels import MAX_DESCRIPTION, clean_tags, clean_title

SCHEMA = {
    "type": "object",
    "required": ["title", "description", "tags"],
    "properties": {"title": {"type": "string"}, "description": {"type": "string"},
                   "tags": {"type": "array", "items": {"type": "string"}}},
}
PROMPT = """Write the YouTube metadata for a vertical Short from a manhwa recap channel.
Series: {series}
The short's narration, in order:
{script}

Return:
- title: at most 70 characters, a hook that makes people watch, without clickbait lies and without hashtags;
- description: two or three sentences that tease the scene without spoiling its ending, then the series name;
- tags: 8 to 12 search tags a viewer of this kind of video would use (the series name, its genre, "manhwa recap")."""


def _stop_on_limit(resume_at) -> None:
    raise claude_cli.ClaudeError("Claude's usage limit is reached; the plan's title and description are used.")


def default_tags(series: str) -> list[str]:
    return clean_tags([series, "manhwa recap", "manga recap", "webtoon recap", "shorts"])


def write(item: dict, series: str, model: str) -> dict:
    """{title, description, tags} for the short; raises claude_cli.ClaudeError when Claude cannot answer."""
    script = "\n".join(entry["text"] for entry in item.get("script") or []) or item["description"] or item["title"]
    folder = store.path("claude")
    folder.mkdir(parents=True, exist_ok=True)
    answer = claude_cli.call([{"type": "text", "text": PROMPT.format(series=series, script=script)}], SCHEMA, model, folder,
                             usage=claude_cli.empty_usage(), mention_event="read", panel_ids=set(),
                             on_proc=lambda proc: None, on_event=lambda event: None, on_limit=_stop_on_limit)
    return {"title": clean_title(answer["title"]) or item["title"],
            "description": answer["description"].strip()[:MAX_DESCRIPTION] or item["description"],
            "tags": clean_tags(answer["tags"]) or default_tags(series)}
