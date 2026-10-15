"""Fails when a tracked .py file imports a third-party package no requirements file lists.

Standard library only, and it never looks at the venv: the answer must be the same on a
machine with leftovers from older versions and on a fresh install. Run by the git pre-commit
hook (.githooks/pre-commit); `--unused` also lists requirements nothing imports directly.

kaggle/ is skipped: those scripts run in Kaggle's own environment.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENT_FILES = ("requirements.txt", "requirements-gpu.txt", "requirements-nodeps.txt")
SKIPPED_DIRS = ("kaggle",)

# Import name -> the distribution(s) that provide it, where the two names differ.
DISTRIBUTIONS = {
    "PIL": ("Pillow",),
    "bs4": ("beautifulsoup4",),
    "chatterbox": ("chatterbox-tts",),
    "cv2": ("opencv-python-headless", "opencv-python"),
    "dotenv": ("python-dotenv",),
    "edge_tts": ("edge-tts",),
    "fitz": ("PyMuPDF",),
    "google": ("google-genai",),
    "phonemizer": ("phonemizer-fork",),
}

# Used only inside try/except ImportError fallbacks (stage 2 / 2a OCR and person detection);
# ultralytics alone would pull a second torch into the venv.
OPTIONAL = {"ultralytics", "easyocr", "pytesseract", "paddleocr"}


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def tracked_python_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [ROOT / line for line in out.splitlines() if line.split("/")[0] not in SKIPPED_DIRS]


def imported_modules(path: Path) -> set[str]:
    modules = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def listed_requirements() -> set[str]:
    names = set()
    for filename in REQUIREMENT_FILES:
        for line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                names.add(normalize(re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)[0]))
    return names


def main(argv: list[str]) -> int:
    files = tracked_python_files()
    local = {f.stem for f in files} | {f.parent.name for f in files if f.name == "__init__.py"}
    importers = defaultdict(set)
    for path in files:
        try:
            modules = imported_modules(path)
        except SyntaxError as error:
            print(f"[ERROR] cannot parse {path.relative_to(ROOT).as_posix()}: {error}")
            return 1
        for module in modules - local - set(sys.stdlib_module_names) - OPTIONAL:
            importers[module].add(path.relative_to(ROOT).as_posix())

    listed = listed_requirements()
    uncovered = {m: files for m, files in importers.items()
                 if not listed & {normalize(d) for d in DISTRIBUTIONS.get(m, (m,))}}
    for module, paths in sorted(uncovered.items()):
        pip_name = DISTRIBUTIONS.get(module, (module,))[0]
        print(f"[MISSING] '{module}' (pip: {pip_name}) is imported by {', '.join(sorted(paths))} "
              f"but is not in {' / '.join(REQUIREMENT_FILES)}")

    if "--unused" in argv:
        used = {normalize(d) for m in importers for d in DISTRIBUTIONS.get(m, (m,))}
        print("Listed but not imported by any tracked file: " + ", ".join(sorted(listed - used)))
    return 1 if uncovered else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
