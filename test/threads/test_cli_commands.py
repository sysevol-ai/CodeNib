# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Every `codenib threads` command, in process, against the local GitHub."""

import json
import subprocess

import pytest

from codenib.cli import run

REPO = "acme/widget"


@pytest.fixture()
def cli(github, tmp_path, capsys):
    store = tmp_path / "store"

    def call(*args, code=0):
        capsys.readouterr()
        status = run(["threads", *args, "--repo", REPO, "--store", str(store)])
        out = capsys.readouterr()
        assert status == code, out.err
        return out.out, out.err

    call.github = github
    call.store = store
    return call


def test_sync_show_context_events(cli, tmp_path):
    _, err = cli("sync", "7")
    assert "new events across 2 threads" in err
    out, _ = cli("show", "7", "--offline", "--no-anchors")
    assert "# Thread context: acme/widget#7" in out and "Return an empty dict." in out

    packet = json.loads(cli("show", "7", "--offline", "--no-anchors", "--json")[0])
    assert packet["tokens"] > 0 and packet["head"].startswith("gh:acme/widget#")

    target = tmp_path / "work" / "CTX.md"
    out, _ = cli("context", "7", "--offline", "--no-anchors", "--output", str(target))
    assert "Wrote" in out and "Return an empty dict." in target.read_text()

    since, _ = cli("show", "7", "--offline", "--no-anchors", "--since", "#7/comment/3")
    assert "# Thread update" in since and "Return an empty dict." not in since

    raw, _ = cli("events", "#7/comment/3")
    assert raw.startswith("[#7/comment/3]") and "Return an empty dict." in raw
    assert json.loads(cli("events", "#9/check/500", "--json")[0])[0]["kind"] == "check"


def test_show_fetches_a_thread_it_does_not_have(cli):
    out, _ = cli("show", "9", "--no-anchors")
    assert "(PR)" in out and "Checks at head111: 1 failing, 1 passing." in out
    assert "## Changes in this pull request" in out and "- src/config.py" in out


def test_errors_are_explained(cli):
    assert "is not in the store" in cli("show", "7", "--offline", code=2)[1]
    assert "was not found" in cli("sync", "404", code=2)[1]
    assert "name a thread number" in cli("sync", code=2)[1]
    cli("sync", "7", "--quiet")
    assert "no event" in cli("show", "7", "--offline", "--since", "#7/x/1", code=2)[1]
    assert "no stored event" in cli("events", "#7/comment/999", code=2)[1]
    assert "no ticket for #7" in cli("inbox", "7", code=2)[1]
    assert "no tickets" in cli("poll", code=2)[1]
    assert "must use https" in cli("sync", "7", "--api-url", "http://x.test", code=2)[1]


def test_tickets_poll_inbox_board_close(cli, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _, err = cli("open", "7", "--no-anchors", "--dir", str(work))
    assert "Ticket #7 follows #7, #9" in err
    assert "Return an empty dict." in (work / "THREAD_CONTEXT.md").read_text()
    assert cli("inbox", "7", "--no-anchors")[1].startswith("Nothing new")

    cli("poll")  # the first poll sets the cursor
    cli.github.extra_comments.append(
        {"event": "commented", "id": 8, "user": {"login": "bo", "type": "User"},
         "body": "Any progress?", "created_at": "2026-02-01T00:00:00Z"}
    )  # fmt: skip
    cli.github.updated = [7, 123]
    result = json.loads(cli("poll", "--json")[0])
    assert result["changed_on_github"] == 2 and result["refetched"] == [7]
    assert result["tickets_with_news"] == [7]

    board, _ = cli("board")
    assert "#7 [open, 1 unread, follows 2 threads] Crash on empty config" in board
    assert json.loads(cli("board", "--json")[0])[0]["unread"] == 1
    assert "Any progress?" in cli("inbox", "7", "--no-anchors", "--sync")[0]
    assert "closed" in cli("close", "7")[0] and "No tickets." in cli("board")[0]
    assert "did not exist" in cli("close", "7")[0]


def test_poll_notices_a_new_check_result_on_a_followed_pull_request(cli):
    cli("open", "7", "--no-anchors", "--output", "/dev/null")
    cli("poll")
    cli.github.check_conclusion = "success"
    out, _ = cli("poll")
    assert "ticket #7 has news" in out
    update, _ = cli("inbox", "7", "--no-anchors")
    assert "# Thread update" in update


def test_open_pulls_index_feeds_the_context(cli, tmp_path):
    repo = tmp_path / "checkout"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "config.py").write_text("def parse_config(path):\n    return 1\n")
    for args in (["init", "-q", "-b", "main"], ["add", "."],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b"],
                 ["remote", "add", "origin", "git@github.com:acme/widget.git"]):  # fmt: skip
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    cli("sync", "7", "--quiet")
    # Drop the link to #9 so that it counts as outside work, not a linked thread.
    header = cli.store / REPO / "9.header.json"
    header.unlink()
    _, err = cli("sync", "--open-pulls", "5")
    assert "indexed 1 open pull requests" in err
    out, _ = cli(
        "show", "7", "--offline", "--checkout", str(repo), "--code-budget", "0"
    )
    assert "src/config.py:1-2 `parse_config()`" in out
    assert "## Also in progress on this code" in out and "- #9 (open PR by ann" in out


def test_repository_is_inferred_from_the_origin_remote(github, tmp_path, capsys,
                                                       monkeypatch):  # fmt: skip
    repo = tmp_path / "clone"
    repo.mkdir()
    origin = "https://github.com/acme/widget.git"
    for args in (["init", "-q"], ["remote", "add", "origin", origin]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    monkeypatch.chdir(repo)
    assert run(["threads", "show", "7", "--no-anchors", "--store",
                str(tmp_path / "s")]) == 0  # fmt: skip
    assert "acme/widget#7" in capsys.readouterr().out
    monkeypatch.chdir(tmp_path)
    assert run(["threads", "show", "7", "--store", str(tmp_path / "s")]) == 2
    assert "pass --repo owner/name" in capsys.readouterr().err
