# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codenib.annotations import AnnotationError, AnnotationStore, TaskStore
from codenib.annotations.agents import parse_claude_output
from codenib.annotations.runner import accept, dispatch, reject, run_task, task_patch

PAGE = {
    "id": "math",
    "title": "Math helpers",
    "markdown": (
        "# Math helpers\n\nIntro.\n\n## Adding numbers\n\n"
        "`add` takes **two numbers** and returns their sum [E1](#evidence-E1).\n\n"
        "## Other\n\nNothing here [E2](#evidence-E2).\n"
    ),
    "citations": [
        {"file": "calc.py", "start_line": 1, "end_line": 2, "node_name": "add"},
        {"file": "README.md", "start_line": 1, "end_line": 1},
    ],
}


def lookup(page_id: str):
    return PAGE if page_id == "math" else None


def _question(
    store: AnnotationStore, text: str = "Does it add?", kind: str = "question"
):
    return store.create(
        kind=kind,
        text=text,
        on={
            "page": "math",
            "section": "Adding numbers",
            "quote": "two numbers",
            "block": "add takes two numbers and returns their sum",
        },
    )


def _branches(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "codenib/*"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_question_is_answered_in_place(git_repo: Path, fake_agent) -> None:
    store = AnnotationStore(git_repo)
    question = _question(store)
    task = dispatch(git_repo, [question.id])
    assert store.get(question.id).status == "dispatched"
    assert task.kind == "question"

    done = run_task(
        git_repo, task.id, agent=fake_agent, page_lookup=lookup, repo_name="t/calc"
    )

    assert done.status == "done", done.error
    assert done.usage == {
        "input_tokens": 150,
        "output_tokens": 20,
        "cost_usd": 0.01,
        "turns": 3,
        "duration_ms": 1200,
    }
    answer = store.get(done.results[0])
    assert answer.kind == "answer" and answer.author == "agent"
    assert answer.reply_to == question.id and answer.task == task.id
    assert answer.on == question.on
    assert answer.files == [{"path": "calc.py", "start_line": 1, "end_line": 2}]
    assert store.get(question.id).status == "answered"
    prompt = fake_agent.prompt_path.read_text()
    assert f"## Annotation {question.id} (question)" in prompt
    assert "section 'Adding numbers'" in prompt
    assert "> two numbers" in prompt
    assert "- calc.py:1-2 (add)" in prompt
    assert "README.md" not in prompt  # cited by another section only
    assert (git_repo / "calc.py").read_text().count("a - b") == 1
    assert _branches(git_repo) == ""


def test_change_runs_in_a_worktree_until_accepted(git_repo: Path, fake_agent) -> None:
    store = AnnotationStore(git_repo)
    change = _question(store, "Make add add.", kind="change")
    task = dispatch(git_repo, [change.id])

    seen = {}

    def citing(changed):
        seen.update(changed)
        return [
            {"id": "math", "title": "Math", "files": ["calc.py"]},
            {"id": "api", "title": "API", "files": ["calc.py"]},
        ]

    done = run_task(
        git_repo, task.id, agent=fake_agent, page_lookup=lookup, pages_citing=citing
    )

    assert done.status == "needs_review", done.error
    assert done.diff["files"] == [{"path": "calc.py", "added": 1, "deleted": 1}]
    assert [p["id"] for p in done.diff["pages"]] == ["math", "api"]
    assert seen == {"calc.py": [(2, 2)]}
    assert "codenib/task-" in _branches(git_repo)
    assert "a - b" in (git_repo / "calc.py").read_text()  # working tree untouched
    assert "+    return a + b" in task_patch(git_repo, task.id)
    assert store.get(change.id).status == "needs_review"
    kinds = sorted((store.get(i).kind, store.get(i).page) for i in done.results)
    assert kinds == [("summary", "api"), ("summary", "math")]
    (elsewhere,) = [store.get(i) for i in done.results if store.get(i).page == "api"]
    assert elsewhere.text.startswith("A change requested on [Math helpers](?p=math)")
    assert elsewhere.reply_to == ""  # reviewed on the page that asked for it

    accepted = accept(git_repo, task.id)

    assert accepted.status == "done"
    assert "a + b" in (git_repo / "calc.py").read_text()
    assert store.get(change.id).status == "applied"
    assert not (TaskStore(git_repo).run_dir(task.id) / "worktree").exists()
    with pytest.raises(AnnotationError, match="not waiting"):
        accept(git_repo, task.id)


def test_rejected_change_leaves_no_branch(git_repo: Path, fake_agent) -> None:
    store = AnnotationStore(git_repo)
    change = _question(store, "Make add add.", kind="change")
    task = dispatch(git_repo, [change.id])
    run_task(git_repo, task.id, agent=fake_agent, page_lookup=lookup)

    rejected = reject(git_repo, task.id)

    assert rejected.status == "rejected"
    assert _branches(git_repo) == ""
    assert store.get(change.id).status == "rejected"
    assert "a - b" in (git_repo / "calc.py").read_text()
    again = dispatch(git_repo, [change.id])  # a rejected change can go again
    assert again.inputs == [change.id]


def test_accept_refuses_a_diff_that_no_longer_applies(
    git_repo: Path, fake_agent
) -> None:
    store = AnnotationStore(git_repo)
    change = _question(store, "Make add add.", kind="change")
    task = dispatch(git_repo, [change.id])
    run_task(git_repo, task.id, agent=fake_agent, page_lookup=lookup)
    (git_repo / "calc.py").write_text("def add(a, b):\n    return b - a\n")

    with pytest.raises(AnnotationError, match="no longer applies"):
        accept(git_repo, task.id)
    assert TaskStore(git_repo).get(task.id).status == "needs_review"


def test_missing_results_are_asked_for_once_more(
    git_repo: Path, fake_agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODE", "empty-first")
    store = AnnotationStore(git_repo)
    question = _question(store)
    task = dispatch(git_repo, [question.id])

    done = run_task(git_repo, task.id, agent=fake_agent, page_lookup=lookup)

    assert done.status == "done" and done.attempts == 2
    assert fake_agent.calls_path.read_text() == "2"
    assert (
        "previous run returned no usable result" in fake_agent.prompt_path.read_text()
    )
    assert done.usage["cost_usd"] == 0.02


def test_a_mangled_id_still_lands_on_its_annotation(
    git_repo: Path, fake_agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODE", "mangle")
    store = AnnotationStore(git_repo)
    question = _question(store)
    task = dispatch(git_repo, [question.id])

    done = run_task(git_repo, task.id, agent=fake_agent, page_lookup=lookup)

    assert done.status == "done" and done.attempts == 1
    assert store.get(done.results[0]).reply_to == question.id


def test_agent_failure_ends_the_task(
    git_repo: Path, fake_agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODE", "error")
    store = AnnotationStore(git_repo)
    change = _question(store, "Make add add.", kind="change")
    task = dispatch(git_repo, [change.id])

    done = run_task(git_repo, task.id, agent=fake_agent, page_lookup=lookup)

    assert done.status == "failed"
    assert "model overloaded" in done.error
    assert store.get(change.id).status == "failed"
    assert _branches(git_repo) == ""
    assert dispatch(git_repo, [change.id]).inputs == [change.id]


def test_dispatch_refuses_notes_answers_and_a_second_task(git_repo: Path) -> None:
    store = AnnotationStore(git_repo)
    note = store.create(kind="note", text="Keep add pure.", on={"page": "math"})
    with pytest.raises(AnnotationError, match="only questions and change requests"):
        dispatch(git_repo, [note.id])
    first, second = _question(store), _question(store, "Another?")
    dispatch(git_repo, [first.id])
    with pytest.raises(AnnotationError, match="already running"):
        dispatch(git_repo, [second.id])
    with pytest.raises(AnnotationError, match="select at least one"):
        dispatch(git_repo, [])


def test_standing_notes_and_threads_reach_the_prompt(
    git_repo: Path, fake_agent
) -> None:
    store = AnnotationStore(git_repo)
    store.create(kind="note", text="add must stay pure.", on={"page": "math"})
    question = _question(store)
    task = dispatch(git_repo, [question.id])
    first = run_task(git_repo, task.id, agent=fake_agent, page_lookup=lookup)
    follow = store.create(
        kind="question",
        text="And for floats?",
        reply_to=first.results[0],
        on=question.on,
    )
    task = dispatch(git_repo, [follow.id])

    run_task(git_repo, task.id, agent=fake_agent, page_lookup=lookup)

    prompt = fake_agent.prompt_path.read_text()
    assert "## Standing notes\n- add must stay pure. (note on page math)" in prompt
    assert "- person (question): Does it add?" in prompt
    assert "- agent (answer): Answer 0 from calc.py:1-2" in prompt


def test_claude_output_parsing() -> None:
    run = parse_claude_output(
        '{"type":"result","subtype":"success","result":"x {\\"results\\": []}",'
        '"usage":{"input_tokens":3,"cache_creation_input_tokens":4,"output_tokens":5},'
        '"total_cost_usd":0.5,"num_turns":2,"duration_ms":9}'
    )
    assert run.structured == {"results": []} and run.error == ""
    assert run.usage["input_tokens"] == 7
    assert parse_claude_output("not json").error
    assert (
        parse_claude_output('[{"type":"system"}]').error
        == "the agent printed no result record"
    )


def test_changed_ranges_follow_the_base_numbering() -> None:
    from codenib.annotations.runner import changed_ranges

    patch = (
        "diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n"
        "@@ -10,3 +10,4 @@ def f():\n x\n+y\n--- not a header\n z\n"
        "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n"
        "@@ -0,0 +1,2 @@\n+a\n+b\n"
    )
    assert changed_ranges(patch) == {"a.py": [(10, 11)], "new.py": [(1, 1)]}


def test_an_annotation_on_code_sends_the_code_not_a_page_section(
    git_repo: Path, fake_agent
) -> None:
    store = AnnotationStore(git_repo)
    question = store.create(
        kind="question",
        text="Why subtract?",
        on={
            "page": "math",
            "path": "calc.py",
            "start_line": 1,
            "end_line": 2,
            "quote": "def add(a, b):\n    return a - b",
        },
    )
    task = dispatch(git_repo, [question.id])

    run_task(git_repo, task.id, agent=fake_agent, page_lookup=lookup)

    prompt = fake_agent.prompt_path.read_text()
    assert "On the code calc.py:1-2" in prompt
    assert (
        "They selected this code:\n```\ndef add(a, b):\n    return a - b\n```" in prompt
    )
    assert "The section reads" not in prompt


def test_the_agent_hears_when_annotated_code_changed_or_is_gone(
    git_repo: Path, fake_agent
) -> None:
    store = AnnotationStore(git_repo)
    on = {"page": "math", "path": "calc.py", "start_line": 1, "end_line": 1}
    changed = store.create(kind="question", text="A?", on={**on, "quote": "x"})
    store.update(changed.id, anchor_state="changed")
    gone = store.create(kind="question", text="B?", on={**on, "quote": "y"})
    store.update(gone.id, anchor_state="detached")
    task = dispatch(git_repo, [changed.id, gone.id])

    run_task(git_repo, task.id, agent=fake_agent, page_lookup=lookup)

    prompt = fake_agent.prompt_path.read_text()
    assert "edited since they selected it" in prompt
    assert "That code is no longer in the file." in prompt
