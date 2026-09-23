# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import subprocess

from codenib.wiki.context_evidence import (
    blame_history,
    covering_test_blocks,
    find_test_references,
    history_evidence_blocks,
    repository_has_history,
)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_find_test_references_names_tests_that_mention_the_symbol(tmp_path):
    _write(
        tmp_path / "tests" / "test_sessions.py",
        "def test_should_strip_auth_host_change():\n"
        "    assert should_strip_auth('http://a', 'http://b')\n\n"
        "def test_unrelated():\n"
        "    assert True\n\n"
        "async def test_rebuild_auth_keeps_same_host():\n"
        "    mixin.rebuild_auth(req, resp)\n",
    )
    _write(
        tmp_path / "pkg" / "sessions_test.go",
        "func TestRebuildAuth(t *testing.T) {\n    rebuild_auth()\n}\n",
    )
    _write(tmp_path / "src" / "sessions.py", "def should_strip_auth(): ...\n")

    refs = find_test_references(
        str(tmp_path),
        [
            "src/sessions.py:SessionRedirectMixin.should_strip_auth()",
            "src/sessions.py:SessionRedirectMixin.rebuild_auth()",
        ],
    )

    assert [(ref.file, ref.name) for ref in refs] == [
        ("tests/test_sessions.py", "test_should_strip_auth_host_change"),
        ("tests/test_sessions.py", "test_rebuild_auth_keeps_same_host"),
        ("pkg/sessions_test.go", "TestRebuildAuth"),
    ]
    blocks = covering_test_blocks(refs)
    assert blocks[0]["kind"] == "test"
    assert blocks[0]["file"] == "tests/test_sessions.py"
    assert "covers `should_strip_auth`: test should strip auth host change" in (
        blocks[0]["content"]
    )
    assert blocks[0]["test_names"] == [
        "test_should_strip_auth_host_change",
        "test_rebuild_auth_keeps_same_host",
    ]


def test_find_test_references_handles_missing_repository_and_short_names(tmp_path):
    assert find_test_references(None, ["a.py:run()"]) == []
    assert find_test_references(str(tmp_path / "missing"), ["a.py:run()"]) == []
    _write(tmp_path / "tests" / "test_x.py", "def test_run():\n    run()\n")
    # Leaf names shorter than four characters match too much source to cite.
    assert find_test_references(str(tmp_path), ["a.py:run()"]) == []


def _git(repo, *args):
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "PATH": "/usr/bin:/bin",
        },
    )


def test_blame_history_reports_commit_subjects_for_a_span(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / "mod.py", "def strip():\n    return 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat: add strip helper")
    _write(repo / "mod.py", "def strip():\n    # host changed\n    return 2\n")
    _git(repo, "commit", "-q", "-am", "fix: strip auth when the host changes")
    _write(repo / "mod.py", "def strip():\n    # host changed\n    return 3\n")

    assert repository_has_history(str(repo)) is True
    entries = blame_history(str(repo), "mod.py", 1, 3)
    assert {entry.subject for entry in entries} == {
        "fix: strip auth when the host changes",
        "feat: add strip helper",
    }
    # Uncommitted working-tree lines carry no subject and are dropped.
    assert all(entry.sha.strip("0") for entry in entries)

    blocks = history_evidence_blocks(str(repo), [("mod.py", 1, 3, "mod.py:strip()")])
    assert blocks[0]["kind"] == "history"
    assert "fix: strip auth when the host changes" in blocks[0]["content"]
    assert blocks[0]["symbol"] == "mod.py:strip() history"


def test_history_is_skipped_without_a_full_clone(tmp_path):
    assert repository_has_history(str(tmp_path)) is False
    assert history_evidence_blocks(str(tmp_path), [("a.py", 1, 2, "a.py:f()")]) == []
