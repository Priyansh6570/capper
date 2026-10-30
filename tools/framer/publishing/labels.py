"""Chapter ranges as text, and the {series} / {chapters} / {part} placeholders of titles and descriptions."""
from __future__ import annotations

import re

PLACEHOLDER = re.compile(r"\{(series|chapters|part)\}")
MAX_TITLE = 100
MAX_DESCRIPTION = 4900
MAX_TAGS_CHARS = 450


def chapter_label(numbers: list[int]) -> str:
    """[22, 23, 24, 30] -> "22-24, 30"."""
    runs: list[list[int]] = []
    for n in sorted(set(numbers)):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def parse_chapters(text: str) -> list[int]:
    """"1, 3, 5-7" -> [1, 3, 5, 6, 7]; ValueError when the text is not a chapter list."""
    numbers: set[int] = set()
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise ValueError(f"'{part}' is not a chapter or a range like 22-45.")
        first, last = int(match[1]), int(match[2] or match[1])
        if last < first or last - first > 5000:
            raise ValueError(f"'{part}' is not a valid range.")
        numbers.update(range(first, last + 1))
    if not numbers:
        raise ValueError("Give the chapters, like 22-45.")
    return sorted(numbers)


def fill(template: str, *, series: str, chapters: str, part: int) -> str:
    values = {"series": series, "chapters": chapters, "part": str(part)}
    return PLACEHOLDER.sub(lambda m: values[m[1]], template)


def clean_title(title: str) -> str:
    """A title YouTube accepts: no angle brackets, at most 100 characters."""
    return re.sub(r"[<>]", "", title).strip()[:MAX_TITLE].strip()


def clean_tags(tags) -> list[str]:
    """Unique non-empty tags that together stay within YouTube's 500 character limit."""
    cleaned, total = [], 0
    for tag in tags if isinstance(tags, list) else []:
        tag = re.sub(r"[<>,]", "", str(tag)).strip()
        if tag and tag.lower() not in {t.lower() for t in cleaned} and total + len(tag) + 3 <= MAX_TAGS_CHARS:
            cleaned.append(tag)
            total += len(tag) + 3
    return cleaned


def part_title(title: str, template: str, part: int, parts: int) -> str:
    """The title of one part of a split video: a template without {part} gets the number appended."""
    if parts > 1 and "{part}" not in template:
        title = f"{title} - Part {part}"
    return clean_title(title)
