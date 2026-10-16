"""Routes for the session-wide UI settings."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

bp = Blueprint("settings", __name__)


# Session-wide UI settings, kept SERVER-SIDE in memory (NOT browser localStorage)
# so a theme / layout choice survives page reloads while the app is running.
_SETTINGS = {"theme": "dark", "split": None}


@bp.get("/settings")
def get_settings():
    """Current session UI settings (theme + split position), held server-side."""
    return jsonify(**_SETTINGS)


@bp.post("/settings")
def set_settings():
    """Update session UI settings. Body: JSON {theme?:'dark'|'light', split?:int}."""
    data = request.get_json(silent=True) or {}
    if data.get("theme") in ("dark", "light"):
        _SETTINGS["theme"] = data["theme"]
    if "split" in data:
        try:
            # Sanity bounds only - the real clamp to the viewport happens
            # client-side in applySplit()/splitMax(), which is allowed to go
            # much wider than this on a large monitor.
            _SETTINGS["split"] = max(240, min(6000, int(data["split"])))
        except (TypeError, ValueError):
            pass
    return jsonify(**_SETTINGS)
