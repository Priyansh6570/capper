"""The Telegram bot, the phone remote. It polls Telegram from inside the app (no server), answers only the
chat that sent it /start first, and sends the approval cards and notifications."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import requests

from publishing import channels, quota, stats, store, sync, thumbnails, uploads
from publishing.labels import chapter_label, clean_title, parse_chapters
from publishing.youtube_api import ApiError

API = "https://api.telegram.org"
FILE = "telegram.json"
POLL_S = 25
RETRY_S = 10
MESSAGE_LIMIT = 4000
CAPTION_LIMIT = 1000
LIST_LIMIT = 20
UPLOAD_BUTTONS = ("approve", "skip", "title")
HELP = """/queue - videos waiting for your approval
/schedule - what uploads next, and when
/private - my videos that are private on YouTube
/uploaded - recent uploads with links
/stats - views, watch time and subscribers of the last 7 days
/projects - the project names /run accepts
/run <project> <chapters> - start the pipeline, e.g. /run My Series 22-45
/shorts <project> <chapters> - make shorts for finished chapters, e.g. /shorts My Series 22-45
/plan - each project's next upload, buffer and what to make next
/status - pipelines running now"""


@dataclass
class Reaction:
    """The answer to a card button: shown to the user, and whether the card's buttons stay for another press."""
    text: str
    keep_buttons: bool = False


@dataclass
class Controls:
    """What the bot may ask of the app besides the upload queue."""
    run: Callable[[str, list[int]], str]    # project name or slug, chapters -> what was started
    shorts: Callable[[str, list[int]], str]
    plan: Callable[[], str]
    status: Callable[[], str]
    projects: Callable[[], list[str]]       # the names /run accepts
    act: Callable[[str, str], Reaction]     # a planner card's button: (action, card id)


class TelegramError(RuntimeError):
    pass


_controls: Controls | None = None
_stop = threading.Event()
_reply_handler: Callable[[str], None] | None = None   # takes the next plain message, after the bot asked for one


def _state() -> dict:
    return store.read(FILE, {})


def _save(**fields) -> None:
    with store.LOCK:
        store.write(FILE, {**_state(), **fields})


def state() -> dict:
    current = _state()
    return {"configured": bool(current.get("token")), "username": current.get("username"),
            "paired": current.get("chat_id") is not None}


def _api(method: str, *, token: str | None = None, http_timeout: int = 30, files: dict | None = None, **payload) -> dict:
    token = token or _state().get("token")
    if not token:
        raise TelegramError("No Telegram bot token is set.")
    try:
        if files:
            resp = requests.post(f"{API}/bot{token}/{method}", data=payload, files=files, timeout=http_timeout)
        else:
            resp = requests.post(f"{API}/bot{token}/{method}", json=payload, timeout=http_timeout)
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise TelegramError("Could not reach Telegram.") from e
    if not data.get("ok"):
        raise TelegramError(data.get("description") or f"Telegram answered {resp.status_code}.")
    return data["result"]


def _keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": text, "callback_data": data} for text, data in row] for row in rows]}


def _chat() -> int | None:
    return _state().get("chat_id")


def send(text: str, rows: list[list[tuple[str, str]]] | None = None) -> None:
    chat = _chat()
    if chat is None:
        return
    chunks = [text[i:i + MESSAGE_LIMIT] for i in range(0, len(text), MESSAGE_LIMIT)] or [""]
    for number, chunk in enumerate(chunks):
        extra = {"reply_markup": _keyboard(rows)} if rows and number == len(chunks) - 1 else {}
        _api("sendMessage", chat_id=chat, text=chunk, disable_web_page_preview=True, **extra)


def notify(text: str, rows: list[list[tuple[str, str]]] | None = None) -> None:
    """Tell the paired chat something; a notification must never break the work that triggered it."""
    try:
        send(text, rows)
    except TelegramError:
        pass


def ask(prompt: str, handler: Callable[[str], None]) -> None:
    """Send `prompt`; the user's next plain message (not a command) goes to `handler`."""
    global _reply_handler
    _reply_handler = handler
    send(prompt)


def _duration(seconds: float | None) -> str:
    total = int(seconds or 0)
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _local(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%a %d %b %H:%M")


def card_text(item: dict) -> str:
    channel = channels.get(item["channel"]) or {}
    part = f" (part {item['part']} of {item['parts']})" if item.get("parts", 1) > 1 else ""
    return (f"{item['title']}\nChannel: {channel.get('title', 'not connected')}\nLength: {_duration(item.get('duration_s'))}\n"
            f"Chapters: {chapter_label(item['chapters'])}{part}")[:CAPTION_LIMIT]


def announce_approval(item: dict) -> None:
    """Send a full video waiting for approval with its thumbnail and the Approve / Skip / Edit title buttons."""
    chat = _chat()
    if chat is None:
        return
    rows = [[("Approve", f"approve:{item['id']}"), ("Skip", f"skip:{item['id']}")], [("Edit title", f"title:{item['id']}")]]
    try:
        try:
            photo = thumbnails.jpeg_for_youtube(Path(item["thumbnail"])) if item.get("thumbnail") else None
        except OSError:
            photo = None
        if photo:
            _api("sendPhoto", chat_id=chat, caption=card_text(item), reply_markup=json.dumps(_keyboard(rows)),
                 files={"photo": ("thumbnail.jpg", photo)}, http_timeout=60)
        else:
            send(card_text(item), rows)
    except TelegramError:
        pass


def configure(token: str, controls: Controls) -> dict:
    """Use `token` for the bot: checked with Telegram first. Starts the polling."""
    token = token.strip()
    me = _api("getMe", token=token)
    previous = _state()
    same_bot = previous.get("username") == me["username"]
    _save(token=token, username=me["username"], offset=previous.get("offset", 0) if same_bot else 0,
          chat_id=previous.get("chat_id") if same_bot else None)
    start(controls)
    return state()


def unpair() -> None:
    _save(chat_id=None)


def remove() -> None:
    _stop.set()
    with store.LOCK:
        store.write(FILE, {})


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def _cmd_queue(_: str) -> None:
    waiting = [i for i in uploads.all_items() if i["status"] == "waiting_approval"]
    if not waiting:
        send("Nothing is waiting for approval.")
    for item in waiting:
        announce_approval(item)


def _due(item: dict) -> str:
    return item.get("publish_at") or item.get("approved_at") or item["created_at"]


def _when(item: dict) -> str:
    if item["status"] == "uploading":
        return "uploading now"
    if item["status"] == "scheduled":
        return _local(item["publish_at"])
    return f"retry {_local(item['next_attempt_at'])}" if item["next_attempt_at"] else "as soon as the quota allows"


def _quota_line(client_id: str) -> str:
    q = quota.snapshot(client_id)
    return f"Quota today: {q['used']:,} of {q['limit']:,} units, resets in {q['resets_in_s'] // 3600} h {q['resets_in_s'] % 3600 // 60} min."


def _schedule_lines(heading: str, items: list[dict]) -> list[str]:
    if not items:
        return []
    shown = [f"{_when(i)} - {i.get('series') or i['title']}: {i['title']}" if i["kind"] == uploads.SHORT
             else f"{_when(i)} - {i['title']}" for i in items[:LIST_LIMIT]]
    more = [f"...and {len(items) - LIST_LIMIT} more"] if len(items) > LIST_LIMIT else []
    return ["", f"{heading} ({len(items)})", *shown, *more]


def _cmd_schedule(_: str) -> None:
    upcoming = sorted((i for i in uploads.all_items() if i["status"] in ("uploading", "queued", "scheduled")), key=_due)
    lines = sorted({_quota_line(c["client"]["id"]) for c in channels.all_channels()})
    lines += _schedule_lines("Videos", [i for i in upcoming if i["kind"] == uploads.FULL])
    lines += _schedule_lines("Shorts", [i for i in upcoming if i["kind"] == uploads.SHORT])
    send("\n".join(lines if upcoming else [*lines, "Nothing is waiting to upload."]))


def _line(item: dict) -> str:
    note = " - uploaded private, publish it in Studio" if item["locked"] and item["status"] == "private" else ""
    return f"{item['title']} [{item['status']}]{note}\n{uploads.url_of(item)}"


def _cmd_private(_: str) -> None:
    for channel in channels.all_channels():
        try:
            sync.refresh_visibility(channel["id"])
        except ApiError as e:
            send(f"{channel['title']}: could not check YouTube ({e}).")
    private = [i for i in uploads.all_items() if i["status"] == "private"]
    send("\n\n".join(_line(i) for i in private[-LIST_LIMIT:]) if private else "None of your videos is private.")


def _cmd_uploaded(_: str) -> None:
    uploaded = sorted((i for i in uploads.all_items() if i["video_id"]), key=lambda i: i["uploaded_at"], reverse=True)[:10]
    send("\n\n".join(_line(i) for i in uploaded) if uploaded else "Nothing has been uploaded yet.")


def _cmd_stats(_: str) -> None:
    connected = channels.all_channels()
    if not connected:
        send("No YouTube channel is connected.")
    for channel in connected:
        try:
            send(stats.week_text(channel))
        except ApiError as e:
            send(f"{channel['title']}: could not read the statistics ({e}).")


def _cmd_run(args: str) -> None:
    project, _, chapters = args.strip().rpartition(" ")
    if not project:
        send("Usage: /run <project> <chapters>, for example /run My Series 22-45")
        return
    try:
        send(_controls.run(project.strip(), parse_chapters(chapters)))
    except ValueError as e:
        send(str(e))


def _cmd_shorts(args: str) -> None:
    project, _, chapters = args.strip().rpartition(" ")
    if not project:
        send("Usage: /shorts <project> <chapters>, for example /shorts My Series 22-45")
        return
    try:
        send(_controls.shorts(project.strip(), parse_chapters(chapters)))
    except ValueError as e:
        send(str(e))


def _cmd_plan(_: str) -> None:
    send(_controls.plan())


def _cmd_projects(_: str) -> None:
    names = _controls.projects()
    send("\n".join(names) if names else "There are no projects yet.")


def _cmd_status(_: str) -> None:
    send(_controls.status())


def _cmd_help(_: str) -> None:
    send(HELP)


COMMANDS = {"queue": _cmd_queue, "schedule": _cmd_schedule, "private": _cmd_private, "uploaded": _cmd_uploaded,
            "stats": _cmd_stats, "projects": _cmd_projects, "run": _cmd_run, "shorts": _cmd_shorts, "plan": _cmd_plan,
            "status": _cmd_status, "help": _cmd_help}


def _handle_message(message: dict) -> None:
    global _reply_handler
    chat, text = message.get("chat", {}).get("id"), (message.get("text") or "").strip()
    command, _, args = text.partition(" ")
    command = command.lstrip("/").partition("@")[0].lower() if text.startswith("/") else None
    paired = _chat()
    if paired is None:
        if command == "start":
            _save(chat_id=chat)
            send("Connected. This chat now controls ReCapper.\n\n" + HELP)
        return
    if chat != paired:
        return
    if command is None:
        handler, _reply_handler = _reply_handler, None
        if handler:
            handler(text)
        return
    _reply_handler = None
    handler = COMMANDS.get(command)
    if command == "start":
        send("Already connected.\n\n" + HELP)
    elif handler is None:
        send("Unknown command.\n\n" + HELP)
    else:
        handler(args)


def _set_title(item_id: str, text: str) -> None:
    item = uploads.get(item_id)
    if item and item["status"] == "waiting_approval" and clean_title(text):
        announce_approval(uploads.update(item_id, title=clean_title(text)))


def _handle_upload_button(action: str, item_id: str) -> str:
    item = uploads.get(item_id)
    if item is None or item["status"] != "waiting_approval":
        return "Already handled."
    if action == "title":
        ask(f"New title for: {item['title']}", lambda text: _set_title(item_id, text))
        return "Send the new title as your next message."
    uploads.decide(item_id, action == "approve")
    return "Approved - it will upload as private." if action == "approve" else "Skipped."


def _handle_callback(query: dict) -> None:
    message = query.get("message") or {}
    if message.get("chat", {}).get("id") != _chat():
        return
    action, _, argument = (query.get("data") or "").partition(":")
    if action in UPLOAD_BUTTONS:
        reaction = Reaction(_handle_upload_button(action, argument), keep_buttons=action == "title")
    else:
        reaction = _controls.act(action, argument)
    _api("answerCallbackQuery", callback_query_id=query["id"], text=reaction.text)
    if not reaction.keep_buttons:
        _api("editMessageReplyMarkup", chat_id=message["chat"]["id"], message_id=message["message_id"],
             reply_markup={"inline_keyboard": []})


def _handle(update: dict) -> None:
    if "callback_query" in update:
        _handle_callback(update["callback_query"])
    elif "message" in update:
        _handle_message(update["message"])


def _poll(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            updates = _api("getUpdates", http_timeout=POLL_S + 10, offset=_state().get("offset", 0), timeout=POLL_S,
                           allowed_updates=["message", "callback_query"])
        except TelegramError:
            stop.wait(RETRY_S)
            continue
        for update in updates:
            try:
                _handle(update)
            except Exception as e:  # noqa: BLE001 - one bad command must not stop the bot
                notify(f"That did not work: {e}")
            _save(offset=update["update_id"] + 1)


def start(controls: Controls) -> None:
    """(Re)start polling with the bot token on file; does nothing without one."""
    global _controls, _stop
    _controls = controls
    _stop.set()
    _stop = threading.Event()
    if _state().get("token"):
        threading.Thread(target=_poll, args=(_stop,), daemon=True).start()
