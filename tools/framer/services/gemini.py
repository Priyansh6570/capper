"""Gemini script generation and the API key it uses."""
from __future__ import annotations

import difflib
import os
import time
from pathlib import Path

import pacing
import tones
from config import ROOT
from services.chapter_files import gemini_parts_dir


# --------------------------------------------------------------------------- #
# Gemini script generation - reads the SAME gemini_parts/*.pdf files the panel
# above produces (never re-splits anything), sends each part to the Gemini
# API in order, and stitches the results into one script - replacing the old
# manual "upload each part to Gemini yourself, paste the answer back" step.
# --------------------------------------------------------------------------- #
GEMINI_NARRATION_TYPES = {
    "first_person": 'first-person, as the hero living through these events ("I", "me")',
    "third_person": 'third-person recap narration ("he", "she", "they")',
}


# Pro is the default: this app cares more about faithful dialogue extraction
# and prose quality than speed. Flash is offered as a same-quality-tier
# fallback for when Pro's (lower) free-tier rate limit is the bottleneck -
# see GEMINI_MIN_INTERVAL_S below, which paces requests generously for
# either, and _gemini_generate_part's own 429 retry/backoff on top of that.
GEMINI_MODELS = {
    "gemini-3.1-pro-preview": "Gemini 3.1 Pro - highest quality (slower, lower rate limit)",
    "gemini-3.8-flash": "Gemini 3.8 Flash - faster (higher rate limit)",
}


GEMINI_DEFAULT_MODEL = "gemini-3.1-pro-preview"


def _gemini_min_interval(model: str) -> float:
    return 8.0 if "pro" in model else 3.0


def _gemini_client():
    """Never raises anything but a plain RuntimeError with a fix-it message -
    this reaches the SSE stream as a single clear {type:"error"} frame."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:  # noqa: BLE001
        pass
    key, _source = get_gemini_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set. Add it in Settings (Script "
                            "generation) or to a .env file in the project root.")
    from google import genai
    return genai.Client(api_key=key)


def narration_voice(tone: str, narration_type: str) -> str:
    narr_desc = GEMINI_NARRATION_TYPES.get(narration_type, GEMINI_NARRATION_TYPES["first_person"])
    return f"Narrate {narr_desc}.\n\n{tones.style_instruction(tone)}"


def style_reference_directive(style_reference: str) -> str:
    return ("Match the LANGUAGE, TONE, and HUMOR of this example script as closely "
            "as you can - imitate its voice and style, not its plot or content:\n\n"
            + style_reference.strip())


def _gemini_system_prompt(tone: str, narration_type: str, style_reference: str,
                           is_first_part: bool, is_last_part: bool) -> str:
    parts = [
        "You are writing a recap narration script for one part of a manhwa/webtoon "
        "chapter, from the attached PDF (its pages are the comic's panels, in "
        "reading order, including all dialogue and sound effects as they appear).",
        narration_voice(tone, narration_type),
        "Extract EVERY piece of dialogue and every story beat faithfully - miss "
        "nothing important - but tell it as flowing narration prose, not a "
        "transcript: weave dialogue and description together naturally, the way "
        "a narrator retelling the chapter out loud would.",
        "Narrate like a skilled recap-channel storyteller, adding understanding and energy "
        "rather than reading panels out. Give every meaningful panel its due: a fight may be "
        "compressed into flowing narration, but each hit, turn, reveal and reaction that "
        "matters is still told. Besides what happens, say why it matters and what is at stake, "
        "what a character wants, feels or is thinking when the pages show it, how a rule or "
        "power of this world works when a scene shows one, and a brief reminder of an earlier "
        "event when it bears on the moment; build tension toward a turn and react when a "
        "reveal lands. There is no target length; the story sets it. The only filler to leave "
        "out is repeating an event in other words, inventing events or inner thoughts the "
        "pages do not show, generic hype (\"you won't believe what happens next\"), and "
        "stretching one panel over sentences that add nothing.",
        "Write for the ear: end each thought with a full stop and use a comma only where a "
        "speaker would pause; no dashes, ellipses, colons, semicolons or parentheses, and no "
        "quotation marks in the middle of a sentence.",
        "Begin every paragraph with a delivery tag in square brackets, one of "
        + ", ".join(f"[{name}]" for name in pacing.PACES) + ": calm for ordinary narration "
        "(most paragraphs), tense for suspense and confrontation, action for fights and fast "
        "events, reveal for a surprise or discovery (put the revelation in the paragraph's "
        "last sentence), sad for grief and quiet emotion, comic for a joke (put the punchline "
        "last). Start a new paragraph whenever the delivery changes.",
        "Output ONLY the narration prose itself, in reading order, each paragraph starting with "
        "its tag - no scene/part headings, no markdown, no bullet points, no meta-commentary "
        "about the images or the task.",
    ]
    if style_reference.strip():
        parts.append(style_reference_directive(style_reference))
    if not is_first_part:
        parts.append(
            "This PDF starts by repeating the last few pages of the previous part "
            "(included only so you have context for continuity). Do NOT re-narrate "
            "that repeated part - continue the story smoothly from where the new "
            "content begins, as if this were one uninterrupted chapter."
        )
    if not is_last_part:
        parts.append(
            "This is not the final part of the chapter - do not write an ending, "
            "conclusion, or wrap-up; the story continues in the next part."
        )
    return "\n\n".join(parts)


def _gemini_generate_part(client, model: str, system_prompt: str, pdf_bytes: bytes,
                           max_attempts: int = 5):
    """Generates the narration for one PDF part. A generator so a rate-limit
    retry can surface a live log line instead of the connection going quiet
    for up to a minute; yields {type:"log"} frames along the way and exactly
    one final {type:"part_done", text} or {type:"part_error", message}."""
    from google.genai import types
    from google.genai import errors as genai_errors

    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=[types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")],
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt, temperature=0.7,
                    max_output_tokens=8192),
            )
            text = (resp.text or "").strip()
            if not text:
                raise RuntimeError("Gemini returned an empty response")
            yield {"type": "part_done", "text": text}
            return
        except genai_errors.APIError as e:
            code = getattr(e, "code", None)
            msg = getattr(e, "message", None) or str(e)
            if code == 429 and attempt < max_attempts:
                wait = min(60, 5 * (2 ** (attempt - 1)))
                yield {"type": "log", "line": f"  rate-limited - retrying in {wait}s "
                                              f"(attempt {attempt}/{max_attempts})…"}
                time.sleep(wait)
                continue
            yield {"type": "part_error", "message": f"Gemini API error {code}: {msg}"}
            return
        except Exception as e:  # noqa: BLE001 - network hiccups etc: retry a few times too
            if attempt < max_attempts:
                wait = 3 * attempt
                yield {"type": "log", "line": f"  {type(e).__name__} - retrying in {wait}s…"}
                time.sleep(wait)
                continue
            yield {"type": "part_error", "message": f"{type(e).__name__}: {e}"}
            return


def _dedupe_overlap(prev_text: str, next_text: str, window: int = 700, min_match: int = 30) -> str:
    """The PDF parts share a few overlap pages for continuity (see PART_OVERLAP)
    - the prompt asks Gemini not to re-narrate them, but models don't always
    comply. Finds the longest matching block between the tail of `prev_text`
    and the head of `next_text`; if it's substantial, drops everything in
    `next_text` up to the end of that match (extended to the next whitespace
    so it doesn't cut mid-sentence). A no-op when there's no real duplication
    - the common case when the model followed the instruction."""
    a = prev_text[-window:]
    b = next_text[:window]
    match = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    if match.size < min_match:
        return next_text
    cut = match.b + match.size
    rest = next_text[cut:]
    return rest.lstrip()


def _stitch_script_parts(texts: list[str]) -> str:
    if not texts:
        return ""
    out = [texts[0].strip()]
    for t in texts[1:]:
        out.append(_dedupe_overlap(out[-1], t.strip()))
    return "\n\n".join(p for p in out if p)


def build_gemini_script_stream(chapter_id: str, gemini_settings: dict):
    parts_dir = gemini_parts_dir(chapter_id)
    part_files = sorted(parts_dir.glob("*.pdf"))
    if not part_files:
        yield {"type": "error", "message": "No Gemini parts found for this chapter - "
                                            "download the chapter first (parts are "
                                            "created automatically)."}
        return
    try:
        client = _gemini_client()
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "message": str(e)}
        return

    model = gemini_settings.get("model") or GEMINI_DEFAULT_MODEL
    if model not in GEMINI_MODELS:
        model = GEMINI_DEFAULT_MODEL
    tone = gemini_settings["tone"]
    narration_type = gemini_settings.get("narration_type") or "first_person"
    style_reference = gemini_settings.get("style_reference") or ""

    n = len(part_files)
    min_interval = _gemini_min_interval(model)
    texts: list[str] = []
    last_call = 0.0
    yield {"type": "start", "of_parts": n, "message": f"Generating script with {model} - "
                                                       f"{n} part(s)…"}
    for i, path in enumerate(part_files, start=1):
        yield {"type": "log", "line": f"Generating part {i} of {n}…"}
        yield {"type": "progress", "part": i, "of_parts": n}
        if i > 1:
            wait = min_interval - (time.monotonic() - last_call)
            if wait > 0:
                time.sleep(wait)
        system_prompt = _gemini_system_prompt(tone, narration_type, style_reference,
                                               i == 1, i == n)
        pdf_bytes = path.read_bytes()
        text = None
        for ev in _gemini_generate_part(client, model, system_prompt, pdf_bytes):
            if ev["type"] == "part_done":
                text = ev["text"]
            elif ev["type"] == "part_error":
                yield {"type": "error", "part": i, "of_parts": n,
                       "message": f"Part {i} of {n} failed: {ev['message']} - fix the "
                                  f"issue above and click Generate again to retry."}
                return
            else:
                yield ev
        last_call = time.monotonic()
        texts.append(text or "")
        yield {"type": "log", "line": f"  part {i} of {n} done ({len(text or '')} chars)."}

    script = _stitch_script_parts(texts)
    yield {"type": "result", "script": script}


# --------------------------------------------------------------------------- #
# Gemini API key - the ONE credential in this app that is genuinely global
# (not per-project, unlike tone/style/model above), and NEVER in project.json.
#
# Preferred storage is the OS credential store (Windows Credential Manager,
# via the `keyring` package) instead of a plaintext .env line - this is
# obfuscation, not absolute secrecy: a local app's key is always ultimately
# readable on the machine it runs on, one way or another, by whoever controls
# that machine. The real protection was always that it's each user's OWN key
# and never leaves their machine - keyring just keeps it out of a plaintext
# file that's one accidental "open in Notepad" or careless screen-share away
# from being read. Falls back to the existing .env convention (same as
# GROQ_API_KEY/HF_TOKEN elsewhere in this codebase) when keyring has no usable
# backend - still local-only, just plaintext again.
#
# A key already sitting in .env from before this existed is migrated into
# keyring (and removed from .env) the first time it's read, so installs that
# predate this also get the improvement, not just new keys saved from now on.
# --------------------------------------------------------------------------- #
_KEYRING_SERVICE = "ReCapper"


_KEYRING_ACCOUNT = "GEMINI_API_KEY"


def _keyring_backend():
    try:
        import keyring
        keyring.get_keyring()  # raises/returns a no-op fail backend if none is usable
        return keyring
    except Exception:  # noqa: BLE001 - keyring missing or no OS backend -> caller falls back to .env
        return None


def _env_file_path() -> Path:
    return ROOT / ".env"


def _read_env_dict() -> dict:
    path = _env_file_path()
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        out[k.strip()] = v.strip()
    return out


def _set_env_value(key: str, value: str | None) -> None:
    """Set (or, when `value` is falsy, remove) one KEY=value line in the
    project-root .env file, preserving every other line untouched - a
    read-modify-write version of what setup.bat does with a one-shot
    append for HF_TOKEN, since this can also be called to update/clear an
    already-set key from the running app, not just write it once."""
    path = _env_file_path()
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
    out, found = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            found = True
            if value:
                out.append(f"{key}={value}")
            continue  # drop the line entirely when clearing
        out.append(line)
    if value and not found:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")


def get_gemini_key() -> tuple[str, str]:
    """Returns (key, source); source is "keyring", "env", or "" (unset)."""
    kr = _keyring_backend()
    env_key = _read_env_dict().get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    if not kr:
        return env_key, ("env" if env_key else "")
    try:
        key = kr.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
    except Exception:  # noqa: BLE001
        key = None
    if key:
        return key, "keyring"
    if env_key:  # one-time migration off plaintext storage
        try:
            kr.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, env_key)
            _set_env_value("GEMINI_API_KEY", None)
            return env_key, "keyring"
        except Exception:  # noqa: BLE001 - migration failed; .env still has it
            pass
    return env_key, ("env" if env_key else "")


def set_gemini_key(key: str) -> str:
    """Saves `key` and returns where it ended up: "keyring" or "env"."""
    os.environ["GEMINI_API_KEY"] = key  # effective immediately, no restart needed
    kr = _keyring_backend()
    if kr:
        try:
            kr.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, key)
            _set_env_value("GEMINI_API_KEY", None)  # don't leave a stale plaintext copy
            return "keyring"
        except Exception:  # noqa: BLE001
            pass
    _set_env_value("GEMINI_API_KEY", key)
    return "env"


def clear_gemini_key() -> None:
    os.environ.pop("GEMINI_API_KEY", None)
    kr = _keyring_backend()
    if kr:
        try:
            kr.delete_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        except Exception:  # noqa: BLE001
            pass
    _set_env_value("GEMINI_API_KEY", None)
