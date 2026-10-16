"""The page itself and its static assets."""
from __future__ import annotations

import os
from pathlib import Path

from flask import Blueprint, jsonify, send_from_directory

from config import ROOT

bp = Blueprint("web", __name__)


HERE = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@bp.get("/")
def index():
    return send_from_directory(HERE, "index.html")


@bp.get("/__app_ping__")
def app_ping():
    """Lightweight identity marker - NOT app data. Used to tell "a previous
    ReCapper instance is already on this port" apart from "some unrelated
    program happens to be using it" (see resolve_port at startup), and to
    poll for the server coming back up after /api/restart."""
    return jsonify(app="recapper", pid=os.getpid())


@bp.get("/assets/<path:name>")
def app_asset(name):
    """Serves the app's own branding assets (logo/icon) from ROOT/assets -
    used by index.html for the favicon and header logo."""
    return send_from_directory(str(ROOT / "assets"), name)
