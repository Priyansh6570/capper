"""Central configuration. Everything is local and file-based by design."""
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_SETTINGS_FILE = ROOT / "app_settings.json"   # settings of this PC, see tools/framer/app_settings.py


def apply_model_cache_setting() -> None:
    """Point huggingface_hub at the model cache folder saved in app_settings.json (a bigger drive, say)
    by setting HF_HOME. An HF_HOME already in the environment wins. This runs when config is imported,
    because every process of the app imports config before it touches huggingface_hub."""
    if os.environ.get("HF_HOME"):
        return
    try:
        folder = json.loads(APP_SETTINGS_FILE.read_text(encoding="utf-8")).get("hf_home")
    except (OSError, ValueError):
        return
    if folder:
        os.environ["HF_HOME"] = folder


apply_model_cache_setting()


def apply_scratch_setting() -> None:
    """Keep the heavy temporary and cache folders off the system drive. Torch, CUDA, Triton and Python's
    tempfile write gigabytes to %TEMP% and %LOCALAPPDATA% while a voice model runs; when the model cache
    (`hf_home`) is on another drive, their folders go next to it, under `scratch`. A variable already set
    in the environment wins. Runs at import, before torch or CUDA is loaded."""
    cache = os.environ.get("HF_HOME")
    if not cache:
        return
    scratch = Path(cache).parent / "recapper_scratch"
    folders = {"TEMP": "tmp", "TMP": "tmp", "TMPDIR": "tmp", "TORCH_HOME": "torch",
               "XDG_CACHE_HOME": "xdg", "CUDA_CACHE_PATH": "cuda", "TRITON_CACHE_DIR": "triton",
               "TORCHINDUCTOR_CACHE_DIR": "inductor", "NUMBA_CACHE_DIR": "numba"}
    try:
        for name, sub in folders.items():
            if not os.environ.get(name) or name in ("TEMP", "TMP", "TMPDIR"):
                (scratch / sub).mkdir(parents=True, exist_ok=True)
                os.environ[name] = str(scratch / sub)
        import tempfile
        tempfile.tempdir = str(scratch / "tmp")
    except OSError:
        pass   # the drive is not there; carry on with the defaults


apply_scratch_setting()

# Extra subprocess.Popen/run kwargs that suppress the console window a
# console-subsystem child (python/ffmpeg/git/...) would otherwise get on
# Windows whenever the parent has none of its own - true for this whole app
# once launched via tray_launcher.py's pythonw.exe. Spread into every
# subprocess call the app makes: `subprocess.run(cmd, **NO_CONSOLE)`.
NO_CONSOLE = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
WORK_DIR = ROOT / "work"         # per-chapter intermediate files
OUTPUT_DIR = ROOT / "output"     # final artifacts (the video)
PROJECTS_DIR = ROOT / "projects"  # framer project system (tools/framer/projects.py)

# Where the Shorts add-on keeps its files, under a chapter's work/ and output/ folders (stages/s8_shorts.py).
SHORTS_FOLDER = "shorts"
SHORTS_PLAN = "plan.json"      # work/<ch>/shorts/: the segments picked for the chapter
SHORTS_INDEX = "shorts.json"   # output/<ch>/shorts/: the rendered shorts with their titles and descriptions

# --------------------------------------------------------------------------- #
# Chapter-folder redirect for the framer's project system.
#
# Every stage resolves its folder via chapter_work_dir()/chapter_output_dir()
# below, imported as bare functions (`from config import chapter_work_dir`), so
# the redirect must live INSIDE these function bodies - and must keep reading
# the module-global WORK_DIR/OUTPUT_DIR at call time, not a captured default,
# so a test that patches config.WORK_DIR directly keeps working.
#
# Two ways a redirect gets installed:
#   - `chapter_scope()` sets a ContextVar - used by the Flask app (in-process,
#     one project+chapter per request).
#   - MANHWA_WORK_ROOT / MANHWA_OUTPUT_ROOT env vars - used by a subprocess
#     (the orchestrator spawned by /generate_stream), read once at import.
#
# Either way, chapter_id is deliberately IGNORED once a redirect is active:
# only one chapter is ever in scope at a time, and the folder already encodes
# project+chapter. With no scope and no env vars, behavior is unchanged, so
# CLI runs over legacy work/<chapter>/ folders are unaffected.
# --------------------------------------------------------------------------- #
_ENV_WORK_ROOT = os.environ.get("MANHWA_WORK_ROOT")
_ENV_OUTPUT_ROOT = os.environ.get("MANHWA_OUTPUT_ROOT")

_scope: ContextVar[tuple[Path, Path] | None] = ContextVar("chapter_scope", default=None)


def set_chapter_scope(work_dir, output_dir) -> None:
    _scope.set((Path(work_dir), Path(output_dir)))


def clear_chapter_scope() -> None:
    _scope.set(None)


@contextmanager
def chapter_scope(work_dir, output_dir):
    """Redirect chapter_work_dir()/chapter_output_dir() to fixed folders for the
    duration of the block, regardless of the chapter_id passed to them."""
    token = _scope.set((Path(work_dir), Path(output_dir)))
    try:
        yield
    finally:
        _scope.reset(token)

# Stage 1 — page render resolution. Higher = sharper but bigger files.
PAGE_DPI = 150

def chapter_work_dir(chapter_id: str) -> Path:
    scoped = _scope.get()
    if scoped is not None:
        return scoped[0]
    if _ENV_WORK_ROOT:
        return Path(_ENV_WORK_ROOT)
    return WORK_DIR / chapter_id


def chapter_output_dir(chapter_id: str) -> Path:
    scoped = _scope.get()
    if scoped is not None:
        return scoped[1]
    if _ENV_OUTPUT_ROOT:
        return Path(_ENV_OUTPUT_ROOT)
    return OUTPUT_DIR / chapter_id
