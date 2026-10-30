"""Routes for publishing: connected YouTube channels, the Telegram bot, and the upload queue."""
from __future__ import annotations

import io

import requests
from flask import Blueprint, jsonify, request, send_file, send_from_directory

import projects
from publishing import channels, enqueue, oauth, quota, slots, store, telegram, thumbnails, uploads
from publishing.labels import clean_tags, clean_title, MAX_DESCRIPTION
from publishing.youtube_api import ApiError
from routes.web import HERE
from services.common import Refused
from services.remote import CONTROLS

bp = Blueprint("publishing", __name__)

REVOKE_URI = "https://oauth2.googleapis.com/revoke"
THUMB_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


@bp.get("/publishing")
def publishing_page():
    return send_from_directory(HERE / "static", "publishing.html")


def _channel_views() -> list[dict]:
    return [{**channels.public(c), "quota": quota.snapshot(c["client"]["id"])} for c in channels.all_channels()]


@bp.get("/api/publishing")
def api_state():
    sign_in = oauth.current()
    return jsonify(channels=_channel_views(), telegram=telegram.state(), quota_limit=quota.limit(),
                   sign_in=sign_in.view() if sign_in else None,
                   items=[uploads.public(i) for i in uploads.all_items()])


@bp.post("/api/publishing/channels/connect")
def api_connect():
    """Body: {client_file}. Opens Google's sign-in in the browser; poll /api/publishing for its progress."""
    try:
        return jsonify(sign_in=oauth.begin((request.get_json(silent=True) or {}).get("client_file", "")).view())
    except ValueError as e:
        return jsonify(error=str(e)), 400


@bp.post("/api/publishing/channels/connect/choose")
def api_choose_channel():
    sign_in = oauth.current()
    try:
        sign_in.choose((request.get_json(silent=True) or {}).get("channel_id", ""))
    except (AttributeError, ValueError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(sign_in=sign_in.view())


@bp.delete("/api/publishing/channels/<channel_id>")
def api_disconnect(channel_id):
    channel = channels.get(channel_id)
    if channel is None:
        return jsonify(error="No such channel."), 404
    try:
        requests.post(REVOKE_URI, params={"token": channel["token"]["refresh"]}, timeout=15)
    except requests.RequestException:
        pass   # the token is forgotten here either way; revoking it at Google is a courtesy
    channels.delete(channel_id)
    return jsonify(ok=True)


@bp.post("/api/publishing/channels/<channel_id>/schedule")
def api_schedule(channel_id):
    """Body: {per_day, from_hour, to_hour}. Shorts not sent yet move to the new slots."""
    if channels.get(channel_id) is None:
        return jsonify(error="No such channel."), 404
    try:
        schedule = slots.clamp_schedule(request.get_json(silent=True) or {})
    except (TypeError, ValueError):
        return jsonify(error="The schedule needs whole numbers."), 400
    channels.update(channel_id, shorts=schedule)
    slots.reschedule(channel_id)
    return jsonify(ok=True, shorts=schedule)


@bp.post("/api/publishing/quota")
def api_quota():
    try:
        quota.set_limit((request.get_json(silent=True) or {}).get("limit"))
    except (TypeError, ValueError):
        return jsonify(error="The quota must be a whole number."), 400
    return jsonify(ok=True, limit=quota.limit())


@bp.post("/api/publishing/telegram")
def api_telegram():
    """Body: {token}. The bot is checked with Telegram, then starts listening."""
    try:
        return jsonify(telegram=telegram.configure((request.get_json(silent=True) or {}).get("token", ""), CONTROLS))
    except telegram.TelegramError as e:
        return jsonify(error=str(e)), 400


@bp.delete("/api/publishing/telegram")
def api_telegram_remove():
    telegram.remove()
    return jsonify(ok=True)


@bp.post("/api/publishing/telegram/unpair")
def api_telegram_unpair():
    telegram.unpair()
    return jsonify(ok=True, telegram=telegram.state())


def _editable(item_id: str):
    item = uploads.get(item_id)
    if item is None:
        return None, (jsonify(error="No such item."), 404)
    if item["status"] not in uploads.EDITABLE:
        return None, (jsonify(error="This item was already sent to YouTube."), 409)
    return item, None


@bp.patch("/api/publishing/items/<item_id>")
def api_edit_item(item_id):
    """Body: any of {title, description, tags}, before the item is uploaded."""
    item, refused = _editable(item_id)
    if refused:
        return refused
    body = request.get_json(silent=True) or {}
    fields = {}
    if "title" in body:
        fields["title"] = clean_title(str(body["title"]))
        if not fields["title"]:
            return jsonify(error="The title cannot be empty."), 400
    if "description" in body:
        fields["description"] = str(body["description"])[:MAX_DESCRIPTION]
    if "tags" in body:
        fields["tags"] = clean_tags(body["tags"])
    if item.get("metadata") == "pending":
        fields["metadata"] = "edited"
    return jsonify(item=uploads.public(uploads.update(item_id, **fields)))


@bp.post("/api/publishing/items/<item_id>/thumbnail")
def api_set_thumbnail(item_id):
    item, refused = _editable(item_id)
    if refused:
        return refused
    upload = request.files.get("file")
    name = (upload.filename or "") if upload else ""
    if not upload or not any(name.lower().endswith(ext) for ext in THUMB_EXTENSIONS):
        return jsonify(error="Choose a PNG, JPEG or WebP image."), 400
    folder = store.path("thumbnails")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{item_id}.jpg"
    target.write_bytes(thumbnails.jpeg_for_youtube(io.BytesIO(upload.read())))
    return jsonify(item=uploads.public(uploads.update(item_id, thumbnail=str(target))))


@bp.delete("/api/publishing/items/<item_id>/thumbnail")
def api_clear_thumbnail(item_id):
    item, refused = _editable(item_id)
    if refused:
        return refused
    return jsonify(item=uploads.public(uploads.update(item_id, thumbnail=None)))


@bp.get("/api/publishing/items/<item_id>/thumbnail")
def api_item_thumbnail(item_id):
    item = uploads.get(item_id)
    if item is None or not item.get("thumbnail"):
        return jsonify(error="No thumbnail."), 404
    try:
        return send_file(io.BytesIO(thumbnails.jpeg_for_youtube(item["thumbnail"])), mimetype="image/jpeg")
    except OSError:
        return jsonify(error="The thumbnail file is missing."), 404


@bp.post("/api/publishing/items/<item_id>/<action>")
def api_item_action(item_id, action):
    """action: approve | skip | retry."""
    if action not in ("approve", "skip", "retry"):
        return jsonify(error="Unknown action."), 404
    item = uploads.retry(item_id) if action == "retry" else uploads.decide(item_id, action == "approve")
    if item is None:
        return jsonify(error="That item is not waiting for this."), 409
    return jsonify(item=uploads.public(item))


@bp.delete("/api/publishing/items/<item_id>")
def api_remove_item(item_id):
    item, refused = _editable(item_id)
    if refused:
        return refused
    uploads.remove_where(lambda i: i["id"] != item_id)
    return jsonify(ok=True)


@bp.post("/api/projects/<slug>/youtube/queue")
def api_queue_merge(slug):
    """Body: {merge}. Puts a merged video into the upload queue, waiting for approval."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    try:
        items = enqueue.full_video(slug, (request.get_json(silent=True) or {}).get("merge", ""))
    except Refused as e:
        return jsonify(error=str(e)), e.status
    except (RuntimeError, ApiError) as e:
        return jsonify(error=str(e)), 500
    return jsonify(ok=True, items=[uploads.public(i) for i in items])
