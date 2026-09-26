"""Pre-download Chatterbox's Hugging Face model weights during setup, instead
of on the app's first render (see stages/s6_tts.py - ChatterboxTurboTTS /
ChatterboxTTS both call `from_pretrained()`, which lazily downloads on first
use with no resume/retry of its own). Run by setup.bat as a dedicated step.

Both repos are pre-fetched, not just the fast-default Turbo one: `_candidates()`
in s6_tts.py always keeps "full" Chatterbox on cuda/cpu as the CPU-OOM-recovery
and emotion-mode fallback, so a run that starts on Turbo can still hit a live
download mid-render if only Turbo were cached here.

Unauthenticated Hugging Face downloads are heavily rate-limited and can crawl
at a trickle for 10+ minutes without ever raising an error to retry on - the
connection isn't dead, just throttled, so a plain download attempt just looks
"stuck" forever. This script works around that:
  - runs each repo's download in a SEPARATE PROCESS, so it can be killed
    cleanly if it truly stalls (Python threads can't be force-killed);
  - the parent polls the on-disk size of that repo's Hugging Face cache
    folder every few seconds; if it hasn't grown in STALL_TIMEOUT_S, the
    attempt is killed and retried. This is safe to do repeatedly: Hugging
    Face Hub caches a file's partial bytes at a deterministic path
    (`<blob>.incomplete`, keyed by ETag, not per-process) and resumes it with
    an HTTP Range request on the next attempt - a retry never restarts from
    zero. The download's file lock is a real OS-level flock (Hub's
    `WeakFileLock`), so killing the process can't leave a stale lock either.
  - honours an optional HF_TOKEN env var (an authenticated request is not
    subject to the same per-IP throttling) - see SETUP_GUIDE.md for how a
    user gets one and how setup.bat picks it up.

Exit code 0 if every model finished; 1 if one or more never finished after
every retry. setup.bat treats a nonzero exit as a WARNING, not a failure: the
app still works either way, just falling back to downloading on first render
(today's behavior) for whichever model didn't get cached here. See
SETUP_GUIDE.md for a manual/offline sideload option as a last resort.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

MAX_ATTEMPTS = 6
STALL_TIMEOUT_S = 120         # kill + retry an attempt with no on-disk growth for this long
STALL_POLL_S = 5
BACKOFF_S = [5, 15, 30, 60, 90]   # between attempts; the last value repeats

# (repo_id, allow_patterns) - mirrors exactly what stages/s6_tts.py's two
# Chatterbox loaders request via chatterbox.tts_turbo/tts .from_pretrained().
MODELS: list[tuple[str, list[str]]] = [
    ("ResembleAI/chatterbox-turbo",
     ["*.safetensors", "*.json", "*.txt", "*.pt", "*.model"]),
    ("ResembleAI/chatterbox",
     ["ve.safetensors", "t3_cfg.safetensors", "s3gen.safetensors", "tokenizer.json", "conds.pt"]),
]


def _hf_cache_dir() -> Path:
    from huggingface_hub import constants
    return Path(constants.HF_HUB_CACHE)


def _repo_folder_bytes(repo_id: str) -> int:
    """Total bytes on disk for this repo's cache folder (finished blobs AND
    in-progress `*.incomplete` ones) - a growing number means real progress,
    independent of anything the child process itself reports."""
    folder_name = "models--" + repo_id.replace("/", "--")
    blobs = _hf_cache_dir() / folder_name / "blobs"
    if not blobs.is_dir():
        return 0
    return sum(f.stat().st_size for f in blobs.glob("*") if f.is_file())


def _spawn_attempt(repo_id: str, allow_patterns: list[str]) -> subprocess.Popen:
    code = (
        "from huggingface_hub import snapshot_download\n"
        f"snapshot_download(repo_id={repo_id!r}, allow_patterns={allow_patterns!r})\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )


def download_with_retry(repo_id: str, allow_patterns: list[str]) -> bool:
    print(f"==> {repo_id}")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        before = _repo_folder_bytes(repo_id)
        print(f"    attempt {attempt}/{MAX_ATTEMPTS} "
              f"({before / 1e6:.0f}MB already on disk)...")
        proc = _spawn_attempt(repo_id, allow_patterns)
        last_growth = time.monotonic()
        last_size = before
        stalled = False
        while True:
            try:
                proc.wait(timeout=STALL_POLL_S)
                break  # process exited on its own
            except subprocess.TimeoutExpired:
                pass
            size = _repo_folder_bytes(repo_id)
            if size > last_size:
                last_size = size
                last_growth = time.monotonic()
            elif time.monotonic() - last_growth > STALL_TIMEOUT_S:
                stalled = True
                break
        if stalled:
            print(f"    no progress for {STALL_TIMEOUT_S}s - looks like a stalled/"
                  f"throttled connection. Killing this attempt and retrying "
                  f"(bytes already downloaded are kept and resumed)...")
            proc.kill()
            proc.wait()
        elif proc.returncode == 0:
            print(f"    done ({_repo_folder_bytes(repo_id) / 1e6:.0f}MB).")
            return True
        else:
            out = (proc.stdout.read() if proc.stdout else "").strip()
            print(f"    attempt failed (exit {proc.returncode}):\n{out[-1500:]}")
        if attempt < MAX_ATTEMPTS:
            wait = BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)]
            print(f"    retrying in {wait}s...")
            time.sleep(wait)
    print(f"    giving up on {repo_id} after {MAX_ATTEMPTS} attempts.")
    return False


def main() -> int:
    if os.environ.get("HF_TOKEN"):
        print("Using HF_TOKEN for an authenticated (faster, less-throttled) download.")
    else:
        print("No HF_TOKEN set - the download may be slow/rate-limited by Hugging "
              "Face's anonymous-IP throttling. See SETUP_GUIDE.md - 'Speeding up "
              "the voice model download' - for how to add one (free, ~1 minute).")

    ok = True
    for repo_id, patterns in MODELS:
        ok = download_with_retry(repo_id, patterns) and ok

    if not ok:
        print("\nOne or more Chatterbox model downloads did not finish after every "
              "retry. This is NOT fatal: the app will fall back to downloading the "
              "rest on first use, same as before this step existed. If your "
              "connection can't reliably reach Hugging Face at all, see "
              "SETUP_GUIDE.md for a manual/offline sideload option.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
