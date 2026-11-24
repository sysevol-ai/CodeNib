# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codenib.annotations import AnnotationError, AnnotationStore
from codenib.annotations.model import is_id, new_id
from codenib.guard.notes import confirm_note, load_note, load_notes


def _front(path: Path) -> dict:
    return yaml.safe_load(path.read_text().split("---\n")[1])


def test_annotation_is_a_guard_note_file(tmp_path: Path) -> None:
    store = AnnotationStore(tmp_path)
    created = store.create(
        kind="question",
        text="Why does it subtract?",
        on={"page": "overview", "quote": "add two numbers", "section": "Math"},
    )
    path = tmp_path / ".codenib" / "notes" / f"{created.id}.md"
    front = _front(path)
    assert front["kind"] == "question"
    assert front["anchors"] == []
    assert front["created"] == created.created
    assert front["on"]["quote"] == "add two numbers"
    assert path.read_text().rstrip().endswith("Why does it subtract?")
    again = store.get(created.id)
    assert again.to_json() == created.to_json()
    assert [a.id for a in store.list("overview")] == [created.id]
    assert store.list("other") == []


def test_guard_skips_conversation_but_reads_notes(tmp_path: Path) -> None:
    store = AnnotationStore(tmp_path)
    store.create(kind="question", text="Why?", on={"page": "overview"})
    note = store.create(kind="note", text="Keep add pure.", on={"page": "overview"})
    notes, errors = load_notes(tmp_path)
    assert errors == []
    assert [n.kind for n in notes] == ["note"]
    assert notes[0].text == "Keep add pure."
    assert note.status == "open"


def test_guard_rewrite_keeps_annotation_keys(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    store = AnnotationStore(tmp_path)
    note = store.create(kind="note", text="Keep add pure.", on={"page": "overview"})
    path = tmp_path / ".codenib" / "notes" / f"{note.id}.md"
    front = _front(path)
    front["anchors"] = [{"symbol": "calc.py:add", "sha256": "stale"}]
    path.write_text(
        "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\nKeep add pure.\n"
    )
    guard_note = load_note(tmp_path, path.relative_to(tmp_path))
    confirm_note(tmp_path, guard_note)
    rewritten = _front(path)
    assert rewritten["id"] == note.id
    assert rewritten["on"] == {"page": "overview"}
    assert rewritten["anchors"][0]["sha256"] != "stale"
    assert store.get(note.id).anchors[0]["symbol"] == "calc.py:add"


def test_store_ignores_plain_guard_notes(tmp_path: Path) -> None:
    notes = tmp_path / ".codenib" / "notes"
    notes.mkdir(parents=True)
    (notes / "cache-gotcha.md").write_text("---\nkind: gotcha\nanchors: []\n---\nx\n")
    assert AnnotationStore(tmp_path).list() == []


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"kind": "decision", "text": "x"}, "kind must be one of"),
        ({"kind": "question", "text": "   "}, "needs text"),
        ({"kind": "question", "text": "x", "on": {"path": "../etc/passwd"}}, "inside"),
        ({"kind": "question", "text": "x", "on": {"start_line": 0}}, "positive"),
        ({"kind": "question", "text": "x", "on": "page"}, "mapping"),
        (
            {"kind": "answer", "text": "x", "files": [{"path": "/etc/passwd"}]},
            "inside the repository",
        ),
    ],
)
def test_bad_annotations_are_refused(
    tmp_path: Path, fields: dict, message: str
) -> None:
    with pytest.raises(AnnotationError, match=message):
        AnnotationStore(tmp_path).create(**fields)
    assert AnnotationStore(tmp_path).list() == []


def test_thread_runs_from_the_first_annotation(tmp_path: Path) -> None:
    store = AnnotationStore(tmp_path)
    first = store.create(kind="question", text="Why?", on={"page": "p"})
    answer = store.create(
        kind="answer", text="Because.", author="agent", reply_to=first.id
    )
    follow = store.create(kind="question", text="And then?", reply_to=answer.id)
    assert [a.id for a in store.thread(follow.id)] == [first.id, answer.id, follow.id]
    with pytest.raises(AnnotationError, match="no annotation"):
        store.create(kind="question", text="x", reply_to=new_id())


def test_ids_sort_by_time_and_validate() -> None:
    early, late = new_id(1_000.0), new_id(2_000.0)
    assert is_id(early) and is_id(late) and early < late
    assert not is_id("../../etc") and not is_id(early.lower())
    with pytest.raises(AnnotationError):
        AnnotationStore(Path("/nonexistent")).get("../x")
