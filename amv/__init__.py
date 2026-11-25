"""AMV-style Shorts: music analysis (music.py) and the beat-synced renderer (render.py)."""
import subprocess
import sys

# The app runs without a window: every program it starts (ffmpeg, ffprobe) must be started with this, or Windows
# opens a terminal for it. Same value as config.NO_CONSOLE, kept here so amv/ stays importable on its own.
NO_CONSOLE = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
