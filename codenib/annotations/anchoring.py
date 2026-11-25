# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Keep an annotation on its code while the code changes.

An annotation on code remembers the file, the lines and the code a person
selected. Each time annotations are read, the code is looked for again:

- ``current``: the selected code is still on the recorded lines;
- ``moved``: the same code is found elsewhere in the file (lines were
  inserted or removed above it), or in the file it was renamed to;
- ``changed``: the code itself was edited, and the closest match near the old
  place is close enough to follow;
- ``detached``: the code is gone; the annotation keeps its last lines and
  code, and the person re-attaches or resolves it.

Whitespace is ignored when comparing, so reindenting code keeps its
annotations, and a selection that starts or ends mid-line still matches.
"""

from __future__ import annotations

import difflib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

STATES = ("current", "moved", "changed", "detached")
#: How close edited code must stay to the selection to keep following it.
MIN_SIMILARITY = 0.6
#: How far from the old lines a fuzzy match is looked for.
SEARCH_WINDOW = 400
MAX_FILE_BYTES = 2_000_000


@dataclass(frozen=True)
class Found:
    state: str
    path: str
    start_line: int
    end_line: int


def _squash(line: str) -> str:
    return " ".join(line.split())


def _snippet(quote: str) -> List[str]:
    lines = [_squash(line) for line in quote.replace("\r\n", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _occurrences(lines: List[str], snippet: List[str]) -> List[Tuple[int, int]]:
    """Every (start, end) line pair, 1-based, where the snippet occurs.

    The first and last snippet lines may be the end and the start of a file
    line (a selection that began or ended mid-line); inner lines must match
    whole.
    """

    found: List[Tuple[int, int]] = []
    count = len(snippet)
    if not count:
        return found
    squashed = [_squash(line) for line in lines]
    for start in range(0, len(squashed) - count + 1):
        window = squashed[start : start + count]
        if count == 1:
            if snippet[0] and snippet[0] in window[0]:
                found.append((start + 1, start + 1))
            continue
        if not window[0].endswith(snippet[0]) or not window[-1].startswith(snippet[-1]):
            continue
        if window[1:-1] == snippet[1:-1]:
            found.append((start + 1, start + count))
    return found


CONTEXT_LINES = 3
#: The least score a place needs to keep an annotation (see ``_score``).
MIN_SCORE = 0.75
#: A place this many lines from the last one must keep all its neighbours:
#: code moved whole takes them along, while a look-alike elsewhere (a sync
#: twin of an async function) differs in one of them.
FAR_LINES = 80


def _windows(
    lines: List[str], snippet: List[str], near: int
) -> List[Tuple[int, int, float]]:
    """Windows of the snippet's length near the old lines, and how alike each is."""

    count = len(snippet)
    if not count or not lines:
        return []
    target = "\n".join(snippet)
    squashed = [_squash(line) for line in lines]
    low = max(0, near - 1 - SEARCH_WINDOW)
    high = min(len(lines) - count, near - 1 + SEARCH_WINDOW)
    found: List[Tuple[int, int, float]] = []
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(target)
    for start in range(low, max(low, high) + 1):
        matcher.set_seq1("\n".join(squashed[start : start + count]))
        if matcher.real_quick_ratio() < MIN_SIMILARITY:
            continue
        if matcher.quick_ratio() < MIN_SIMILARITY:
            continue
        ratio = matcher.ratio()
        if ratio >= MIN_SIMILARITY:
            found.append((start + 1, start + count, ratio))
    return found


def context(lines: List[str], start: int, end: int) -> Tuple[str, str]:
    """The squashed lines just above and just below a span (1-based)."""

    above = lines[max(0, start - 1 - CONTEXT_LINES) : max(0, start - 1)]
    below = lines[end : end + CONTEXT_LINES]
    return (
        "\n".join(_squash(line) for line in above),
        "\n".join(_squash(line) for line in below),
    )


def _context_match(
    lines: List[str], start: int, end: int, before: str, after: str
) -> Optional[float]:
    """How many remembered neighbour lines are still beside the span (0 to 1)."""

    want_above = before.split("\n") if before else []
    want_below = after.split("\n") if after else []
    if not want_above and not want_below:
        return None
    have_above, have_below = context(lines, start, end)
    above = have_above.split("\n") if have_above else []
    below = have_below.split("\n") if have_below else []
    hits = sum(
        1 for w, h in zip(reversed(want_above), reversed(above), strict=False) if w == h
    )
    hits += sum(1 for w, h in zip(want_below, below, strict=False) if w == h)
    return hits / (len(want_above) + len(want_below))


def _score(
    similarity: float, neighbours: Optional[float], unique: bool, distance: int
) -> float:
    """Code alike (60%), neighbours alike (40%), plus 0.2 for one fitting copy.

    Without remembered neighbours the context counts as half. A small penalty
    for distance breaks ties toward the old place.
    """

    score = 0.6 * similarity + 0.4 * (0.5 if neighbours is None else neighbours)
    # One exact copy is strong evidence, unless its neighbours say it is a
    # different place (the annotated code was deleted, a copy remains).
    if unique and similarity == 1.0 and (neighbours is None or neighbours >= 0.5):
        score += 0.2
    return score - min(0.05, distance / 20000)


def renamed_to(repo: Path, path: str, since: str) -> Optional[str]:
    """The file ``path`` became since commit ``since``, if git saw a rename."""

    if not since:
        return None
    completed = subprocess.run(
        ["git", "-C", str(repo), "diff", "-M", "--name-status", since, "--"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].startswith("R") and parts[1] == path:
            return parts[2]
    return None


def head_commit(repo: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _read_lines(file: Path) -> Optional[List[str]]:
    try:
        if not file.is_file() or file.stat().st_size > MAX_FILE_BYTES:
            return None
        return file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def locate(repo: Path, on: Dict[str, object]) -> Optional[Found]:
    """Where an annotation's code is now, or None when it is not on code."""

    path = str(on.get("path") or "")
    quote = str(on.get("quote") or "")
    start = int(on.get("start_line") or 0) or 1
    end = int(on.get("end_line") or start)
    if not path:
        return None
    repo = Path(repo)
    current_path = path
    lines = _read_lines(repo / path)
    if lines is None:
        moved_to = renamed_to(repo, path, str(on.get("commit") or ""))
        if moved_to:
            current_path = moved_to
            lines = _read_lines(repo / moved_to)
    if lines is None:
        return Found("detached", path, start, end)
    snippet = _snippet(quote)
    if not snippet:
        # Nothing selected to follow: keep the lines while the file has them.
        if end <= len(lines):
            state = "current" if current_path == path else "moved"
            return Found(state, current_path, start, end)
        return Found("detached", current_path, start, end)
    exact = _occurrences(lines, snippet)
    candidates: Dict[Tuple[int, int], float] = {span: 1.0 for span in exact}
    for window_start, window_end, ratio in _windows(lines, snippet, start):
        span = (window_start, window_end)
        candidates[span] = max(candidates.get(span, 0.0), ratio)
    before = str(on.get("before") or "")
    after = str(on.get("after") or "")
    best: Optional[Tuple[float, Tuple[int, int], float]] = None
    for span, similarity in candidates.items():
        neighbours = _context_match(lines, span[0], span[1], before, after)
        distance = abs(span[0] - start)
        if distance > FAR_LINES and neighbours is not None and neighbours < 1.0:
            continue
        score = _score(similarity, neighbours, len(exact) == 1, distance)
        if best is None or score > best[0]:
            best = (score, span, similarity)
    if best is None or best[0] < MIN_SCORE:
        return Found("detached", current_path, start, end)
    _, (new_start, new_end), similarity = best
    if similarity < 1.0:
        return Found("changed", current_path, new_start, new_end)
    if (new_start, new_end) == (start, end) and current_path == path:
        return Found("current", path, start, end)
    return Found("moved", current_path, new_start, new_end)


def follow(store, annotations, cache: Optional[Dict[str, tuple]] = None) -> List:
    """Re-locate the code each annotation sits on; save what moved.

    ``cache`` (any dict kept between calls) skips annotations whose anchor and
    file are unchanged since they were last located.
    """

    from .model import AnnotationError

    result = []
    for annotation in annotations:
        on = annotation.on
        if not on.get("path"):
            result.append(annotation)
            continue
        file = Path(store.repo) / str(on["path"])
        try:
            stat = file.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        key = (
            str(on.get("path")),
            on.get("start_line"),
            on.get("end_line"),
            annotation.anchor_state,
            signature,
        )
        if cache is not None and cache.get(annotation.id) == key:
            result.append(annotation)
            continue
        found = locate(store.repo, on)
        fresh: Dict[str, object] = {}
        if found is not None and found.state != "detached":
            lines = _read_lines(Path(store.repo) / found.path) or []
            above, below = context(lines, found.start_line, found.end_line)
            fresh = {"before": above, "after": below}
        if found is not None and (
            found.state != annotation.anchor_state
            or found.path != on.get("path")
            or found.start_line != on.get("start_line")
            or found.end_line != on.get("end_line")
            or any((on.get(key) or "") != value for key, value in fresh.items())
        ):
            new_on = {
                **on,
                **fresh,
                "path": found.path,
                "start_line": found.start_line,
                "end_line": found.end_line,
            }
            try:
                annotation = store.update(
                    annotation.id, on=new_on, anchor_state=found.state
                )
            except AnnotationError:
                pass
        if cache is not None:
            try:
                stat = (Path(store.repo) / str(annotation.on["path"])).stat()
                signature = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                signature = None
            cache[annotation.id] = (
                str(annotation.on.get("path")),
                annotation.on.get("start_line"),
                annotation.on.get("end_line"),
                annotation.anchor_state,
                signature,
            )
        result.append(annotation)
    return result
