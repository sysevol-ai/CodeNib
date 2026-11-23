# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""A change reviewed hunk by hunk."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codenib.annotations import AnnotationError, AnnotationStore, TaskStore
from codenib.annotations.hunks import all_hunks, parse_patch, patch_for
from codenib.annotations.runner import accept, dispatch, reject, review, run_task

PATCH = """diff --git a/a.py b/a.py
index 1..2 100644
--- a/a.py
+++ b/a.py
@@ -1,3 +1,3 @@
 one
-two
+TWO
 three
@@ -20,3 +20,3 @@ def f():
 x
-y
+Y
 z
diff --git a/new.md b/new.md
new file mode 100644
index 0000000..1
--- /dev/null
+++ b/new.md
@@ -0,0 +1 @@
+hello
diff --git a/logo.png b/logo.png
index 1..2 100644
Binary files a/logo.png and b/logo.png differ
"""


def test_a_patch_splits_into_hunks_and_rebuilds_from_any_of_them() -> None:
    files = parse_patch(PATCH)
    hunks = all_hunks(files)
    assert [(h.id, h.path, h.added, h.deleted) for h in hunks] == [
        ("0.0", "a.py", 1, 1),
        ("0.1", "a.py", 1, 1),
        ("1.0", "new.md", 1, 0),
        ("2.0", "logo.png", 0, 0),
    ]
    assert patch_for(files, hunks and [h.id for h in hunks]) == PATCH
    second = patch_for(files, ["0.1"])
    assert second.startswith("diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py")
    assert "-two" not in second and "-y\n+Y" in second
    assert patch_for(files, ["2.0"]) == (
        "diff --git a/logo.png b/logo.png\nindex 1..2 100644\n"
        "Binary files a/logo.png and b/logo.png differ\n"
    )
    assert patch_for(files, []) == ""


def _long_calc(repo: Path) -> None:
    body = ["def add(a, b):", "    return a - b", ""]
    body += [f"# filler {n}" for n in range(30)]
    body += ["", "def scale(x):", "    return x * 1", ""]
    (repo / "calc.py").write_text("\n".join(body))
    git = ["git", "-C", str(repo), "-c", "maintenance.auto=false"]
    subprocess.run([*git, "add", "calc.py"], check=True)
    subprocess.run(
        [*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "long"],
        check=True,
    )


@pytest.fixture
def change(git_repo: Path, fake_agent, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "multi")
    _long_calc(git_repo)
    store = AnnotationStore(git_repo)
    request = store.create(kind="change", text="Fix both.", on={"page": "math"})
    task = dispatch(git_repo, [request.id])
    done = run_task(git_repo, task.id, agent=fake_agent, page_lookup=lambda _p: None)
    assert done.status == "needs_review", done.error
    return git_repo, done, request


def test_hunks_are_accepted_and_discarded_one_at_a_time(change) -> None:
    repo, task, request = change
    hunks = review(repo, task.id)["hunks"]
    assert [(h["id"], h["path"], h["state"]) for h in hunks] == [
        ("0.0", "NOTES.md", "open"),
        ("1.0", "calc.py", "open"),
        ("1.1", "calc.py", "open"),
    ]

    partly = accept(repo, task.id, ["1.1"])

    assert partly.status == "needs_review"
    text = (repo / "calc.py").read_text()
    assert "x * 2" in text and "a - b" in text  # only the second hunk
    assert AnnotationStore(repo).get(request.id).status == "needs_review"
    with pytest.raises(AnnotationError, match="already decided"):
        accept(repo, task.id, ["1.1"])
    with pytest.raises(AnnotationError, match="no hunk 9.9"):
        reject(repo, task.id, ["9.9"])

    reject(repo, task.id, ["1.0"])
    finished = accept(repo, task.id)  # the rest: the new file

    assert finished.status == "done"
    assert finished.diff["accepted"] == ["1.1", "0.0"]
    assert finished.diff["rejected"] == ["1.0"]
    assert (repo / "NOTES.md").read_text() == "new file\n"
    assert "a - b" in (repo / "calc.py").read_text()
    assert AnnotationStore(repo).get(request.id).status == "applied"
    assert [h["state"] for h in review(repo, task.id)["hunks"]] == [
        "accepted",
        "rejected",
        "accepted",
    ]
    assert not (TaskStore(repo).run_dir(task.id) / "worktree").exists()


def test_discarding_every_hunk_rejects_the_change(change) -> None:
    repo, task, request = change
    reject(repo, task.id, ["0.0"])
    finished = reject(repo, task.id)

    assert finished.status == "rejected"
    assert AnnotationStore(repo).get(request.id).status == "rejected"
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "codenib/*"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert branches == ""
    assert not (repo / "NOTES.md").exists()
