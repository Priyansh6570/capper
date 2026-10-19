"""HTTP routes, one blueprint per feature. register_blueprints() adds them all to the app."""
from __future__ import annotations

from flask import Flask


def register_blueprints(app: Flask) -> None:
    from routes import web, settings, chapters, jobs, files, editor_state, system, projects, merge, library, batch, backup, shorts

    for module in (web, settings, chapters, jobs, files, editor_state, system, projects, merge, library, batch, backup, shorts):
        app.register_blueprint(module.bp)
