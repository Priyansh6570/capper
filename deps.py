"""Python dependency check and install for the venv that runs it. The single code path behind
setup.bat, the installer (which runs setup.bat) and the framer's in-app updater.

    python deps.py check [--json]   exit 1 when anything in the requirement files is missing or off-pin
    python deps.py install          install what the requirement files list; exit 1 on a required failure

Install order: requirements-gpu.txt (only when torch is absent, or CPU-only on a PC with an NVIDIA
GPU), then requirements.txt, then each requirements-nodeps.txt entry with --no-deps. Everything
after the first step runs under a constraints file freezing the installed torch/torchaudio, so no
dependency can swap the CUDA build for a CPU one: pip fails loudly instead. The install ends by
verifying torch.cuda.is_available() and says plainly when it is False.
"""
from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path
from typing import Iterator, NamedTuple

from config import NO_CONSOLE, ROOT
import gpu

GPU_FILE = "requirements-gpu.txt"
MAIN_FILE = "requirements.txt"
NODEPS_FILE = "requirements-nodeps.txt"
TORCH_PACKAGES = ("torch", "torchaudio")

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class InstallResult(NamedTuple):
    ok: bool
    failed_optional: list[str]


def requirement_specs(filename: str) -> list[str]:
    path = ROOT / filename
    if not path.exists():
        return []
    lines = (line.split("#", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("-")]


def requirement_name(spec: str) -> str:
    return _NAME_RE.match(spec)[0]


def _pinned_version(spec: str) -> str | None:
    match = re.search(r"==\s*([^\s;,]+)", spec)
    return match[1] if match else None


def _installed_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _torch_needs_cuda_build() -> bool:
    """A PC with an NVIDIA GPU needs the +cuXXX torch build; the plain PyPI wheel is CPU-only."""
    versions = [_installed_version(name) for name in TORCH_PACKAGES]
    return None in versions or (gpu.nvidia_gpu() is not None and any("+cu" not in v for v in versions))


def missing_packages() -> list[str]:
    """Requirement names that are not installed, or installed at a version other than their `==` pin;
    torch/torchaudio when absent or, on an NVIDIA PC, the CPU-only build."""
    importlib.invalidate_caches()
    missing = []
    for filename in (MAIN_FILE, NODEPS_FILE):
        for spec in requirement_specs(filename):
            name, pin = requirement_name(spec), _pinned_version(spec)
            installed = _installed_version(name)
            if installed is None or (pin and installed != pin):
                missing.append(name)
    if _torch_needs_cuda_build():
        missing += [requirement_name(spec) for spec in requirement_specs(GPU_FILE)]
    return missing


def _pip(*args: str) -> Iterator[str]:
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *args]
    yield "$ " + " ".join(cmd)
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, bufsize=1, encoding="utf-8",
                            errors="replace", **NO_CONSOLE)
    yield from (line.rstrip("\n") for line in proc.stdout)
    return proc.wait()


def _torch_constraints() -> Path | None:
    importlib.invalidate_caches()
    pins = [f"{name}=={version}" for name in TORCH_PACKAGES if (version := _installed_version(name))]
    if not pins:
        return None
    path = Path(tempfile.gettempdir()) / "recapper_torch_constraints.txt"
    path.write_text("\n".join(pins) + "\n", encoding="utf-8")
    return path


def _verify_cuda() -> Iterator[str]:
    """Yields a plain-language verdict on whether torch still sees the GPU."""
    if gpu.nvidia_gpu() is None:
        yield "No NVIDIA GPU detected: narration will run on the CPU."
        return
    info = gpu.torch_info()
    if info and info["available"]:
        yield f"[OK] torch {info['version']}: torch.cuda.is_available() is True."
        return
    yield ("[WARN] torch.cuda.is_available() is FALSE: GPU acceleration is NOT active and narration will "
           "run on the CPU. Fix: .venv\\Scripts\\python -m pip install --force-reinstall -r requirements-gpu.txt")


def install() -> Iterator[str]:
    """Yields pip output and status lines; returns an InstallResult."""
    if _torch_needs_cuda_build():
        yield "Installing the CUDA build of PyTorch..."
        if (yield from _pip("-r", str(ROOT / GPU_FILE))) != 0:
            yield "[FAIL] The CUDA build of PyTorch did not install."
            return InstallResult(False, [])

    constraints = _torch_constraints()
    guard = ["-c", str(constraints)] if constraints else []
    yield f"Installing the packages in {MAIN_FILE}..."
    if (yield from _pip("-r", str(ROOT / MAIN_FILE), *guard)) != 0:
        yield f"[FAIL] The packages in {MAIN_FILE} did not install."
        return InstallResult(False, [])

    failed = []
    for spec in requirement_specs(NODEPS_FILE):
        yield f"Installing {spec} (without its dependencies)..."
        if (yield from _pip("--no-deps", spec)) != 0:
            failed.append(requirement_name(spec))
            yield f"[WARN] {spec} did not install."

    yield from _verify_cuda()
    return InstallResult(True, failed)


def _drain(steps: Iterator[str]) -> InstallResult:
    while True:
        try:
            print(next(steps), flush=True)
        except StopIteration as done:
            return done.value


def _main(args: list[str]) -> int:
    command = args[0] if args else ""
    if command == "check":
        missing = missing_packages()
        if args[1:] == ["--json"]:
            print(json.dumps(missing))
        else:
            print("Missing or off-pin: " + ", ".join(missing) if missing else "All requirements are installed.")
        return 1 if missing else 0
    if command == "install":
        result = _drain(install())
        if result.failed_optional:
            print("[WARN] Not installed: " + ", ".join(result.failed_optional))
        return 0 if result.ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
