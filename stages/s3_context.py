"""Stage 3 - Per-panel context + emotion.  Runs on: Groq API (laptop).

Calls Groq's multimodal Llama 4 Scout once per panel (reading order) with the
panel crop, and asks for a short scene/expression/setting description plus ONE
emotion tag drawn strictly from the EMOTIONS vocabulary below (the shared set
stage 6's TTS maps to). Results fill `panel.context` and `panel.emotion`.

JSON mode (`response_format={"type": "json_object"}`) is used to get a clean
`{context, emotion}` object back; if the returned emotion isn't in EMOTIONS we
default to "neutral".

Output is also written to `work/<chapter>/context/context.txt` so you can read
all the per-panel descriptions without parsing the manifest.

Reads `GROQ_API_KEY` from a `.env` file in the project root.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq
from PIL import Image

from manifest import Manifest

EMOTIONS = ["neutral", "tense", "excited", "sad", "angry", "surprised"]

MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
# Groq free tier is ~30 req/min on this model. Pace just under that.
MIN_INTERVAL_S = 2.5

# Groq caps a base64-image request at 4MB and 33MP. Crops get re-encoded to
# JPEG and downscaled below this longest-edge so we stay comfortably under both.
MAX_EDGE_PX = 1536

SYSTEM_PROMPT = """You analyze single panels from a manga/manhwa page for a \
recap pipeline. For the panel image you receive, respond with a JSON object \
containing exactly two keys:
- "context": 1-2 sentences describing the scene, the characters' expressions, \
and the setting. Be concrete and visual; no preamble.
- "emotion": ONE word, the dominant emotion of the panel, chosen STRICTLY from \
this list: neutral, tense, excited, sad, angry, surprised.

Return only the JSON object, nothing else."""

USER_TEXT = """Describe this panel and tag its emotion. Reply as JSON with \
"context" and "emotion" keys. The "emotion" must be one of: neutral, tense, \
excited, sad, angry, surprised."""


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
    m = re.search(r"in\s+([0-9.]+)s", msg)
    if m:
        return float(m.group(1))
    m = re.search(r"in\s+(\d+)m([0-9.]+)s", msg)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    return 10.0


def _encode_crop(crop_path: str) -> str:
    """Load a panel crop, downscale to stay under Groq's limits, return a
    base64 JPEG data URL."""
    with Image.open(crop_path) as img:
        img = img.convert("RGB")
        longest = max(img.size)
        if longest > MAX_EDGE_PX:
            scale = MAX_EDGE_PX / longest
            img = img.resize(
                (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                Image.LANCZOS,
            )
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _describe_panel(client: Groq, data_url: str) -> tuple[str, str]:
    """One vision call with simple retry/backoff for transient errors + rate
    limits. Returns (context, emotion) with emotion validated against EMOTIONS."""
    last_err: Exception | None = None
    for attempt in range(5):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": USER_TEXT},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    },
                ],
                response_format={"type": "json_object"},
                temperature=0.4,
                max_tokens=200,
            )
            raw = (resp.choices[0].message.content or "").strip()
            if not raw:
                last_err = RuntimeError("empty response")
                time.sleep(2 ** attempt)
                continue
            data = json.loads(raw)
            context = str(data.get("context", "")).strip()
            emotion = str(data.get("emotion", "")).strip().lower()
            if emotion not in EMOTIONS:
                emotion = "neutral"
            if not context:
                context = "(no description)"
            return context, emotion
        except json.JSONDecodeError as e:
            # Malformed JSON despite json_object mode - retry a couple times.
            last_err = e
            time.sleep(2 ** attempt)
        except Exception as e:  # noqa: BLE001 - retry anything transient
            last_err = e
            delay = _retry_delay_from_error(e)
            if delay is not None:
                print(f"    rate-limited; sleeping {delay:.1f}s before retry")
                time.sleep(delay + 1.0)
            else:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Groq vision call failed after retries: {last_err}")


def run(m: Manifest) -> Manifest:
    load_dotenv()
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY not set. Add it to a .env file in the project root."
        )
    client = Groq(api_key=api_key)

    out_dir = Path(m.work_dir) / "context"
    out_dir.mkdir(parents=True, exist_ok=True)

    panels = m.kept_panels()  # the matched narration beats (STAGE B)
    lines: list[str] = []
    last_call = 0.0
    for panel in panels:
        # Prefer the clean character crop (bubbles trimmed) s2a produced; fall
        # back to the raw candidate crop.
        frame = panel.frame_image
        if not frame:
            raise RuntimeError(
                f"panel {panel.id} has no crop; run stage 2 first."
            )
        data_url = _encode_crop(frame)

        wait = MIN_INTERVAL_S - (time.monotonic() - last_call)
        if wait > 0:
            time.sleep(wait)
        context, emotion = _describe_panel(client, data_url)
        last_call = time.monotonic()

        panel.context = context
        panel.emotion = emotion
        lines.append(f"[{panel.order:03d}] ({emotion}) {context}")

    (out_dir / "context.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"  tagged {len(panels)} panels with context + emotion -> {out_dir / 'context.txt'}")
    m.stage = "context"
    return m
