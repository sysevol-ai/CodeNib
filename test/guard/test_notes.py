# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Malformed hand-edited notes must not hide other notes or guard findings."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from codenib.guard.notes import add_note, load_notes


@pytest.mark.parametrize(
    "anchors",
    ["42", "0", "true", "false", "2026-11-12", "wrong", "{}", "''"],
)
def test_invalid_anchor_collection_reports_error_and_keeps_valid_notes(
    tmp_path, write_files, anchors
):
    write_files(tmp_path, {"a.py": "def f():\n    return 1\n"})
    valid = add_note(tmp_path, "A valid note.", ["a.py:f"])
    broken = tmp_path / ".codenib/notes/000-broken.md"
    broken.write_text(f"---\nanchors: {anchors}\n---\nA hand-edited note.\n")

    notes, errors = load_notes(tmp_path)

    assert [note.path for note in notes] == [valid.path]
    assert errors == [f"{broken.relative_to(tmp_path)}: anchors must be a list"]


@pytest.mark.parametrize("front_matter", ["kind: note", "anchors: null", "anchors: []"])
def test_missing_or_empty_anchors_remain_readable(tmp_path, write_files, front_matter):
    write_files(
        tmp_path,
        {".codenib/notes/note.md": f"---\n{front_matter}\n---\nAn unanchored note.\n"},
    )

    (note,), errors = load_notes(tmp_path)

    assert note.anchors == []
    assert errors == []


def test_bad_note_does_not_hide_guard_test_findings(
    git_repo, tmp_path, monkeypatch, make_graph, write_files
):
    from codenib.guard import report

    files = {
        "a.py": "def f():\n    return 1\n",
        "tests/test_a.py": "from a import f\n\ndef test_f():\n    assert f() == 1\n",
    }
    commit = git_repo.commit(files)
    base = tmp_path / "base"
    write_files(base, files)
    graph = make_graph(
        [
            ("a.py:f()", "function", "a.py", 0, 1),
            ("tests/test_a.py:test_f()", "function", "tests/test_a.py", 2, 3),
        ],
        [("tests/test_a.py:test_f()", "a.py:f()", "reference", 3)],
    )
    monkeypatch.setattr(
        report,
        "cached_snapshot",
        lambda *_args: SimpleNamespace(
            source=base, seconds=None, load_graph=lambda: graph
        ),
    )
    valid = add_note(git_repo.root, "Preserve this contract.", ["a.py:f"])
    broken = git_repo.root / ".codenib/notes/000-broken.md"
    broken.write_text("---\nanchors: 42\n---\nA hand-edited note.\n")
    (git_repo.root / "a.py").write_text("def f():\n    return 2\n")

    result = report.run_guard(git_repo.root, commit, build=False)

    assert [test.test_id for test in result.impact.tests] == ["tests/test_a.py::test_f"]
    assert [note.path for note, _symbols in result.notes_on_change] == [valid.path]
    assert result.note_errors == [
        f"{broken.relative_to(git_repo.root)}: anchors must be a list"
    ]
