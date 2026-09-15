"""Swappable LLM backend for the reasoning stages (s2b_select, s5_script).

Both stages send a system+user prompt and expect a JSON object back. This module
hides WHICH model produces that object behind one function, `chat_json`, so the
same stages can run on either the Groq cloud API or a LOCAL Ollama model -
selected by the `MANHWA_LLM` env var - to escape Groq free-tier rate limits.

Backends (`MANHWA_LLM`, default "groq"):
- "groq":   Groq llama-3.3-70b-versatile in JSON mode, with retry/backoff for
            rate limits + transient errors. Reads `GROQ_API_KEY` from `.env`.
- "ollama": POST to a local Ollama server (http://localhost:11434 by default,
            override with `MANHWA_OLLAMA_HOST`) running the model named by
            `MANHWA_OLLAMA_MODEL` (default "qwen2.5:3b"), with format="json" and
            stream=false. No API key. A generous timeout because a small local
            model on a 4GB GPU can be slow, plus a couple of retries.

Both backends return the SAME contract: a parsed Python dict, run through the
shared `_extract_json` safety net that tolerates stray prose around the JSON.
"""
from __future__ import annotations

import json
import os
import re
import time

import httpx
from dotenv import load_dotenv

GROQ_MODEL = "llama-3.3-70b-versatile"

OLLAMA_DEFAULT_HOST = "http://localhost:11434"
OLLAMA_DEFAULT_MODEL = "qwen2.5:3b"
# Local generation on a small/4GB GPU can be slow; don't time out mid-reply.
OLLAMA_TIMEOUT_S = 600.0


def _extract_json(text: str) -> dict:
    """Parse a model reply, tolerating stray prose around the JSON object."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the first balanced-looking {...} block.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"could not parse JSON from model reply: {text[:200]!r}")


def _retry_delay_from_error(err: Exception) -> float | None:
    """Pull the API-suggested retry delay out of a 429 error message."""
    msg = str(err)
    if "429" not in msg and "rate_limit" not in msg.lower():
        return None
    # Daily-quota hits can't be retried within the run - fail fast.
    if "per day" in msg.lower() or "daily" in msg.lower():
        raise RuntimeError(
            f"Groq daily free-tier quota exhausted. Wait until reset or upgrade. "
            f"Original: {msg}"
        )
    # Groq returns hints like "Please try again in 1.23s" or "in 1m23s".
    m = re.search(r"in\s+(\d+)m([0-9.]+)s", msg)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    m = re.search(r"in\s+([0-9.]+)s", msg)
    if m:
        return float(m.group(1))
    return 10.0


def _chat_groq(system: str, user: str, max_tokens: int, temperature: float) -> dict:
    """Groq llama-3.3-70b in JSON mode with retry/backoff (the original path)."""
    from groq import Groq

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY not set. Add it to a .env file in the project root."
        )
    client = Groq(api_key=api_key)

    last_err: Exception | None = None
    for attempt in range(5):
        try:
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                return _extract_json(text)
            last_err = RuntimeError("empty response")
            time.sleep(2 ** attempt)
        except Exception as e:  # noqa: BLE001 - retry anything transient
            last_err = e
            delay = _retry_delay_from_error(e)
            if delay is not None:
                print(f"    rate-limited; sleeping {delay:.1f}s before retry")
                time.sleep(delay + 1.0)
            else:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Groq call failed after retries: {last_err}")


def _chat_ollama(system: str, user: str, max_tokens: int, temperature: float) -> dict:
    """Local Ollama chat in JSON mode. Plain HTTP, no API key, no pip package."""
    host = (os.environ.get("MANHWA_OLLAMA_HOST") or OLLAMA_DEFAULT_HOST).rstrip("/")
    model = os.environ.get("MANHWA_OLLAMA_MODEL") or OLLAMA_DEFAULT_MODEL
    url = f"{host}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "format": "json",
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            resp = httpx.post(url, json=payload, timeout=OLLAMA_TIMEOUT_S)
            resp.raise_for_status()
            text = (resp.json().get("message", {}).get("content") or "").strip()
            if text:
                return _extract_json(text)
            last_err = RuntimeError("empty response from Ollama")
        except Exception as e:  # noqa: BLE001 - retry anything transient
            last_err = e
        time.sleep(2 ** attempt)
    raise RuntimeError(
        f"Ollama call failed after retries (url={url}, model={model}). Is Ollama "
        f"running and the model pulled? Original: {last_err}"
    )


def chat_json(system: str, user: str, max_tokens: int, temperature: float = 0.8) -> dict:
    """Send system+user, get a parsed JSON dict back, from whichever backend
    `MANHWA_LLM` selects ("groq" default, or "ollama"). Same contract either way."""
    load_dotenv()  # so MANHWA_LLM / GROQ_API_KEY in .env are honoured
    backend = (os.environ.get("MANHWA_LLM") or "groq").strip().lower()
    if backend == "groq":
        return _chat_groq(system, user, max_tokens, temperature)
    if backend == "ollama":
        return _chat_ollama(system, user, max_tokens, temperature)
    raise RuntimeError(
        f"unknown MANHWA_LLM backend {backend!r}; use 'groq' or 'ollama'"
    )
