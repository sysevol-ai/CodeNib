# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Annotations follow their code as it moves, changes or is renamed."""

from __future__ import annotations

import subprocess
from pathlib import Path

from codenib.annotations import AnnotationStore
from codenib.annotations.anchoring import follow, head_commit, locate

CODE = [
    "def add(a, b):",
    "    total = a + b",
    "    return total",
    "",
    "",
    "def scale(x, factor):",
    "    result = x * factor",
    "    return result",
]


def _write(repo: Path, lines, name: str = "calc.py") -> None:
    (repo / name).write_text("\n".join(lines) + "\n")


def _on(**extra):
    return {
        "path": "calc.py",
        "start_line": 6,
        "end_line": 7,
        "quote": "def scale(x, factor):\n    result = x * factor",
        **extra,
    }


def test_unchanged_code_is_current(tmp_path: Path) -> None:
    _write(tmp_path, CODE)
    found = locate(tmp_path, _on())
    assert (found.state, found.start_line, found.end_line) == ("current", 6, 7)


def test_lines_inserted_above_move_the_annotation(tmp_path: Path) -> None:
    _write(tmp_path, ["import os", "import sys", ""] + CODE)
    found = locate(tmp_path, _on())
    assert (found.state, found.start_line, found.end_line) == ("moved", 9, 10)


def test_reindented_code_still_matches(tmp_path: Path) -> None:
    indented = ["class Calc:"] + ["    " + line if line else line for line in CODE]
    _write(tmp_path, indented)
    found = locate(tmp_path, _on())
    assert (found.state, found.start_line, found.end_line) == ("moved", 7, 8)


def test_a_selection_that_starts_and_ends_mid_line(tmp_path: Path) -> None:
    _write(tmp_path, ["# header"] + CODE)
    on = _on(start_line=2, end_line=3, quote="total = a + b\n    return")
    found = locate(tmp_path, on)
    assert (found.state, found.start_line, found.end_line) == ("moved", 3, 4)


def test_edited_code_is_followed_as_changed(tmp_path: Path) -> None:
    edited = list(CODE)
    edited[5] = "def scale(x, factor=1):"
    _write(tmp_path, ["# one", "# two"] + edited)
    found = locate(tmp_path, _on())
    assert (found.state, found.start_line, found.end_line) == ("changed", 8, 9)


def test_deleted_code_detaches_and_keeps_its_lines(tmp_path: Path) -> None:
    _write(tmp_path, CODE[:4])
    found = locate(tmp_path, _on())
    assert (found.state, found.start_line, found.end_line) == ("detached", 6, 7)
    assert locate(tmp_path, _on(path="gone.py")).state == "detached"
    assert locate(tmp_path, {"page": "p"}) is None


def test_a_renamed_file_is_followed_through_git(git_repo: Path) -> None:
    _write(git_repo, CODE)
    git = ["git", "-C", str(git_repo), "-c", "maintenance.auto=false"]
    commit = ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm"]
    subprocess.run([*git, "add", "calc.py"], check=True)
    subprocess.run([*git, *commit, "code"], check=True)
    anchored_at = head_commit(git_repo)
    subprocess.run([*git, "mv", "calc.py", "arith.py"], check=True)
    subprocess.run([*git, *commit, "rename"], check=True)

    found = locate(git_repo, _on(commit=anchored_at))

    assert (found.state, found.path, found.start_line) == ("moved", "arith.py", 6)


def test_follow_saves_new_places_and_skips_unchanged_files(tmp_path: Path) -> None:
    _write(tmp_path, CODE)
    store = AnnotationStore(tmp_path)
    note = store.create(kind="question", text="Why?", on={"page": "p", **_on()})
    page_note = store.create(kind="question", text="Page?", on={"page": "p"})
    cache: dict = {}

    first = follow(store, [note, page_note], cache)
    assert first[0].anchor_state == "current" and first[1] is page_note

    _write(tmp_path, ["# new line"] + CODE)
    moved = follow(store, store.list(), cache)
    saved = store.get(note.id)
    assert saved.anchor_state == "moved"
    assert (saved.on["start_line"], saved.on["end_line"]) == (7, 8)
    assert {a.id for a in moved} == {note.id, page_note.id}

    before = (tmp_path / ".codenib" / "notes" / f"{note.id}.md").stat().st_mtime_ns
    follow(store, store.list(), cache)  # nothing changed: nothing written
    after = (tmp_path / ".codenib" / "notes" / f"{note.id}.md").stat().st_mtime_ns
    assert before == after


REPEATED = [
    "def send_auth(",
    "    self,",
    "    auth,",
    "    follow_redirects: bool,",
    "    history: list,",
    ") -> Response:",
    "    flow = auth.sync_flow()",
    "",
    "def send_redirects(",
    "    self,",
    "    follow_redirects: bool,",
    "    history: list,",
    ") -> Response:",
    "    while True:",
]


def _annotate_repeated(tmp_path: Path):
    _write(tmp_path, REPEATED)
    store = AnnotationStore(tmp_path)
    from codenib.annotations.anchoring import context

    before, after = context(REPEATED, 4, 5)
    on = {
        "page": "p",
        "path": "calc.py",
        "start_line": 4,
        "end_line": 5,
        "quote": "follow_redirects: bool,\n    history: list,",
        "before": before,
        "after": after,
    }
    return store, store.create(kind="question", text="Why?", on=on)


def test_repeated_code_is_told_apart_by_its_neighbours(tmp_path: Path) -> None:
    store, note = _annotate_repeated(tmp_path)

    _write(tmp_path, ["# header"] + REPEATED)
    (found,) = follow(store, [store.get(note.id)])
    assert (found.anchor_state, found.on["start_line"]) == ("moved", 5)

    edited = ["# header"] + list(REPEATED)
    edited[4] = "    follow_redirects: bool = False,"
    _write(tmp_path, edited)
    (found,) = follow(store, [store.get(note.id)])
    # Edited in place: it stays on its own function, not the identical copy.
    assert (found.anchor_state, found.on["start_line"]) == ("changed", 5)


def test_deleted_code_does_not_jump_to_an_identical_copy(tmp_path: Path) -> None:
    store, note = _annotate_repeated(tmp_path)
    _write(tmp_path, REPEATED[:3] + REPEATED[5:])

    (found,) = follow(store, [store.get(note.id)])

    assert found.anchor_state == "detached"
    assert found.on["start_line"] == 4  # its last place, kept


def test_a_far_look_alike_does_not_take_a_deleted_annotation(tmp_path: Path) -> None:
    def twin(kind: str):
        return [
            f"def {kind}_send(",
            "    self,",
            "    auth,",
            "    follow_redirects: bool,",
            "    history: list,",
            ") -> Response:",
            f"    flow = auth.{kind}_flow()",
            "    return flow",
        ]

    filler = [f"# filler {n}" for n in range(120)]
    lines = twin("sync") + filler + twin("async")
    _write(tmp_path, lines)
    from codenib.annotations.anchoring import context

    start = len(twin("sync")) + len(filler) + 4
    before, after = context(lines, start, start + 1)
    on = {
        "path": "calc.py",
        "start_line": start,
        "end_line": start + 1,
        "quote": "follow_redirects: bool,\n    history: list,",
        "before": before,
        "after": after,
    }
    assert locate(tmp_path, on).state == "current"

    _write(tmp_path, lines[: start - 1] + lines[start + 1 :])  # delete them

    assert locate(tmp_path, on).state == "detached"

    moved = ["# new"] * 150 + lines  # the whole file moved down 150 lines
    _write(tmp_path, moved)
    found = locate(tmp_path, on)
    assert (found.state, found.start_line) == ("moved", start + 150)
