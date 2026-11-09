# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""A new commit keeps the Wiki pages whose source files did not change."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from codenib.wiki import carry_forward as module
from codenib.wiki.agent_wiki import AgentWiki
from codenib.wiki.carry_forward import (
    carry_forward,
    changed_files,
    page_files,
    save_snapshot,
)
from codenib.wiki.sqlite_store import SQLiteWikiStore

OUTLINE = {
    "pages": [
        {"id": "overview", "title": "Overview", "children": []},
        {
            "id": "core",
            "title": "Core",
            "children": [{"id": "auth", "title": "Auth", "children": []}],
        },
    ]
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(root))
    for name in ("a.py", "b.py", "c.py"):
        (root / "src" / name).write_text(f"# {name}\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "one")
    first = _git(root, "rev-parse", "HEAD")
    (root / "src" / "a.py").write_text("# a.py changed\n")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qam", "two")
    return root, first, _git(root, "rev-parse", "HEAD")


def _wiki(store, root: Path, commit: str) -> AgentWiki:
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            instance_id="demo",
            repo="owner/demo",
            base_commit=commit,
            commit_short=commit[:8],
            repo_dir=str(root),
            language="python",
        ),
        manifest=SimpleNamespace(
            source_fingerprint=f"fp-{commit}",
            indexes={},
            commit=commit,
            source_selection=None,
        ),
        code_graph=lambda: None,
        hierarchical_graph=lambda: None,
    )
    return AgentWiki(bundle, "fixture-model", store=store)


def _page(page_id: str, *files: str) -> dict:
    return {
        "id": page_id,
        "title": page_id.title(),
        "markdown": f"# {page_id.title()}\n\nWhat {page_id} does.",
        "citations": [{"file": file, "start_line": 1, "end_line": 1} for file in files],
        "generation": {"mode": "generated", "model": "fixture-model"},
        "quality": {"valid": True},
    }


def _publish(wiki: AgentWiki, pages: dict) -> None:
    wiki._write_cache("outline", OUTLINE)
    tree = OUTLINE["pages"]
    for page_id, page in pages.items():
        meta = wiki._find(page_id, tree)
        if page_id == "overview":
            meta = wiki._overview_page_meta(meta, tree[1:])
        wiki._write_cache(wiki._page_cache_suffix(meta), page)


def test_unchanged_pages_move_to_the_new_commit(repo, tmp_path):
    root, first, second = repo
    store = SQLiteWikiStore(tmp_path / "wiki.sqlite3")
    old = _wiki(store, root, first)
    pages = {
        "overview": _page("overview", "src/b.py"),
        "core": _page("core", "src/b.py", "src/a.py"),
        "auth": _page("auth", "src/c.py"),
    }
    _publish(old, pages)
    snapshot = tmp_path / "carry-forward.json"
    assert save_snapshot(old, snapshot, first) == 3

    new = _wiki(store, root, second)
    assert new.cached_outline() is None
    result = carry_forward(new, snapshot, root, second)

    assert result.outline_reused and result.previous_commit == first
    assert sorted(result.reused) == ["auth", "overview"]
    assert result.rewritten == ["core"]
    assert new.cached_outline() == OUTLINE
    assert new.cached_page("overview")["markdown"] == pages["overview"]["markdown"]
    assert new.cached_page("auth")["citations"] == pages["auth"]["citations"]
    # The page whose file changed is left for the model to write again.
    assert new.cached_page("core") is None
    # The earlier commit's entries are untouched.
    assert old.cached_page("core") is not None


def test_a_rerun_on_the_same_commit_does_not_replace_its_outline(repo, tmp_path):
    root, first, _second = repo
    store = SQLiteWikiStore(tmp_path / "wiki.sqlite3")
    wiki = _wiki(store, root, first)
    _publish(wiki, {"overview": _page("overview", "src/b.py")})
    snapshot = tmp_path / "carry-forward.json"
    save_snapshot(wiki, snapshot, first)

    result = carry_forward(wiki, snapshot, root, first)

    assert not result.outline_reused
    assert result.reason == "this commit already has an outline"


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (lambda data: data.update(version=0), "the earlier Wiki has another format"),
        (
            lambda data: data["prompt_versions"].update(page="0"),
            "the Wiki prompts changed",
        ),
        (lambda data: data.update(commit="f" * 40), "Git cannot compare"),
        (lambda data: data.pop("outline"), "the earlier Wiki is incomplete"),
    ],
)
def test_without_a_usable_snapshot_every_page_is_written(repo, tmp_path, edit, reason):
    root, first, second = repo
    store = SQLiteWikiStore(tmp_path / "wiki.sqlite3")
    old = _wiki(store, root, first)
    _publish(old, {"overview": _page("overview", "src/b.py")})
    snapshot = tmp_path / "carry-forward.json"
    save_snapshot(old, snapshot, first)
    data = json.loads(snapshot.read_text())
    edit(data)
    snapshot.write_text(json.dumps(data))

    new = _wiki(store, root, second)
    result = carry_forward(new, snapshot, root, second)

    assert not result.outline_reused and result.reason.startswith(reason)
    assert new.cached_outline() is None


def test_a_missing_snapshot_writes_every_page(repo, tmp_path):
    root, _first, second = repo
    new = _wiki(SQLiteWikiStore(tmp_path / "wiki.sqlite3"), root, second)

    result = carry_forward(new, tmp_path / "absent.json", root, second)

    assert result.reason == "no earlier Wiki"


def test_a_page_that_names_no_file_is_rewritten(repo, tmp_path):
    root, first, second = repo
    store = SQLiteWikiStore(tmp_path / "wiki.sqlite3")
    old = _wiki(store, root, first)
    _publish(old, {"overview": _page("overview")})
    snapshot = tmp_path / "carry-forward.json"
    save_snapshot(old, snapshot, first)

    result = carry_forward(_wiki(store, root, second), snapshot, root, second)

    assert result.rewritten == ["overview", "core", "auth"]


def test_page_files_reads_nested_evidence_and_keeps_dot_directories():
    page = {
        "citations": [{"file": "./src/a.py"}],
        "evidence": {
            "relations": [{"anchor_file": ".github/workflows/ci.yml"}],
            "items": [{"source_file": "/abs/path.py"}, {"file": "../escape.py"}],
        },
        "media_slots": [{"artifact_path": "docs/arch.png"}],
    }

    assert page_files(page) == {
        "src/a.py",
        ".github/workflows/ci.yml",
        "docs/arch.png",
    }


def test_changed_files_names_every_difference(repo):
    root, first, second = repo

    assert changed_files(root, first, second) == {"src/a.py"}
    assert changed_files(root, second, second) == set()
    assert changed_files(root, "", second) is None
    assert changed_files(root, "0" * 40, second) is None


def test_snapshot_records_the_prompt_versions(repo, tmp_path):
    root, first, _second = repo
    wiki = _wiki(SQLiteWikiStore(tmp_path / "wiki.sqlite3"), root, first)
    _publish(wiki, {"overview": _page("overview", "src/b.py")})
    snapshot = tmp_path / "nested" / "carry-forward.json"

    save_snapshot(wiki, snapshot, first)

    data = json.loads(snapshot.read_text())
    assert data["prompt_versions"] == module._prompt_versions()
    assert set(data["pages"]) == {"overview"}
    assert not list(snapshot.parent.glob(".*tmp*"))
