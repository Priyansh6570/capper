"""Building the shorts frame bank (amv/bank.py) for a project's chapters, new or already exported.

A chapter whose frames were cleared after a Drive backup gets just its `frames` back for the tagging, then
the bank is backed up (its `shorts_frames` kind) so the big `frames` folder can be cleared again. Chapters
that already have a bank are skipped. Nothing here touches the manifest, the orchestrator or a stage.

    python tools/framer/services/shorts_bank.py <project-slug> [chapter numbers...] [--rebuild]
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for p in (ROOT, ROOT / "tools" / "framer"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import chapter_backup  # noqa: E402
import projects  # noqa: E402
from amv import bank  # noqa: E402


def invalidate(work: Path) -> None:
    """The chapter's frames were cut again: its old bank no longer matches them."""
    shutil.rmtree(bank.bank_dir(work), ignore_errors=True)


def recut_frames(slug: str, n: int, dest: Path, log=print) -> int:
    """Cut the chapter's frames again from its saved mapping (boxes, flips, text blur) into `dest`, for a chapter whose
    frames were deleted. Needs the page strip, stitched from the chapter's PDF when it is gone too (that strip is
    removed again afterwards). Returns the number of frames cut; nothing of the chapter's own files is changed."""
    import json

    import config
    from PIL import Image
    from services import chapter_files, frames as frames_svc

    work, out = projects.chapter_work_path(slug, n), projects.chapter_output_path(slug, n)
    try:
        mapping = json.loads((work / "framer" / "mapping.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise FileNotFoundError(f"chapter {n} has no saved mapping") from e
    cid = f"{slug}_ch{n}"
    strip = work / "pages" / "strip_001.png"
    made_strip = not strip.is_file()
    blur = bool(projects.load(slug)["settings"]["framing"]["blur_text"])
    with config.chapter_scope(work, out):
        if made_strip:
            pdf = projects.stored_pdf(slug, n) or chapter_files.downloaded_pdf(cid)
            if pdf is None:
                raise FileNotFoundError(f"chapter {n} has no PDF to rebuild its frames from")
            log(f"chapter {n}: stitching the strip from {pdf.name}")
            chapter_files.stitch_pdf(pdf, cid)
        try:
            img = Image.open(strip).convert("RGB")
            dest.mkdir(parents=True, exist_ok=True)
            cut = 0
            for entry in mapping.get("lines", []):
                for fr in entry.get("frames", []):
                    if fr.get("file") and fr.get("bbox"):
                        frames_svc._crop_frame(img, {"bbox": fr["bbox"], "flip": fr.get("flip"),
                                                     "blur": fr.get("blur_regions")}, dest / fr["file"], blur)
                        cut += 1
            return cut
        finally:
            if made_strip:                      # the strip is huge and was not there before
                for f in (work / "pages").glob("strip_001*"):
                    f.unlink(missing_ok=True)
                try:
                    (work / "pages").rmdir()
                except OSError:
                    pass


def build_chapter(slug: str, n: int, rebuild: bool = False, log=print) -> dict:
    """Tag chapter `n` and fill its bank; returns bank.summary(). Raises FileNotFoundError when its frames cannot be had.
    Frames that are not on this PC are cut again from the chapter's mapping, so a chapter backed up as only its
    script, mapping and video (the usual case) still gets a bank."""
    work = projects.chapter_work_path(slug, n)
    if not rebuild and bank.exists(work):      # its frames, if cleared, are restored by whoever uses them
        return bank.summary(work)
    brief = projects.load(slug)["settings"]["claude"].get("series_brief", "")
    say = lambda m: log(f"chapter {n}: {m}")      # noqa: E731
    have = [s for s in bank.source_frames(work) if s["path"].is_file()]
    if have:
        bank.build(work, context=brief, rebuild=True, log=say)
    else:
        scratch = bank.bank_dir(work) / "_cut"
        try:
            if not recut_frames(slug, n, scratch, log):
                raise FileNotFoundError(f"chapter {n}: the mapping has no frames")
            bank.build(work, context=brief, rebuild=True, frames=scratch, log=say)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    try:
        chapter_backup.backup_kind(slug, n, "shorts_frames")
        chapter_backup.backup_kind(slug, n, "script")
    except (OSError, chapter_backup.BackupUnavailable, KeyError):
        pass            # a project that is not kept in a shared folder has no backup to make
    return bank.summary(work)


def build_project(slug: str, chapters: list[int] | None = None, rebuild: bool = False, log=print) -> dict[int, dict | str]:
    """Every chapter with a script and frames (or just `chapters`). {chapter: summary | error text}."""
    numbers = chapters or [c["n"] for c in projects.load(slug)["chapters"]]
    out: dict[int, dict | str] = {}
    for n in numbers:
        try:
            out[n] = build_chapter(slug, n, rebuild, log)
        except FileNotFoundError:
            out[n] = "no exported frames"
        except Exception as e:      # one bad chapter must not stop the rest
            out[n] = f"{type(e).__name__}: {e}"
            log(f"chapter {n}: {out[n]}")
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    result = build_project(args[0], [int(a) for a in args[1:]] or None, rebuild="--rebuild" in sys.argv)
    for n, r in result.items():
        print(n, r)
