# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""`codenib pages` sets a repository up to publish its Wiki and builds it."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from codenib import cli
from codenib.web import pages_setup


def _repo(tmp_path: Path, origin: str | None = "git@github.com:Owner/demo.git") -> Path:
    root = tmp_path / "demo"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(root)], check=True)
    if origin:
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin", origin], check=True
        )
    return root


def test_init_writes_a_story_workflow_and_prints_the_remaining_steps(tmp_path, capsys):
    root = _repo(tmp_path)

    assert cli.run(["pages", "init", str(root), "--ref", "v9.9.9"]) == 0

    workflow = root / ".github" / "workflows" / "codenib-wiki.yml"
    data = yaml.safe_load(workflow.read_text())
    job = data["jobs"]["publish"]
    assert (
        job["uses"] == "sysevol-ai/CodeNib/.github/workflows/codenib-pages.yml@v9.9.9"
    )
    assert job["with"]["wiki"] == "story"
    assert job["with"]["model"] == pages_setup.DEFAULT_WIKI_MODEL
    assert job["secrets"]["wiki_api_key"] == "${{ secrets.OPENROUTER_API_KEY }}"
    assert data[True]["push"]["branches"] == ["trunk"]  # YAML reads `on` as True
    assert data["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    out = capsys.readouterr().out
    assert "choose GitHub Actions as the source" in out
    assert "OPENROUTER_API_KEY" in out
    assert (
        "[![Wiki: CodeNib](https://owner.github.io/demo/wiki-badge.svg)]"
        "(https://owner.github.io/demo/)" in out
    )


def test_init_refuses_to_overwrite_and_always_asks_for_a_key(tmp_path, capsys):
    root = _repo(tmp_path, origin=None)
    assert cli.run(["pages", "init", str(root), "--ref", "v1.0.0"]) == 0
    assert cli.run(["pages", "init", str(root), "--ref", "v1.0.0"]) == 2
    assert "already exists" in capsys.readouterr().err

    assert cli.run(["pages", "init", str(root), "--force", "--ref", "v1"]) == 0
    data = yaml.safe_load((root / pages_setup.WORKFLOW_PATH).read_text())
    assert data["jobs"]["publish"]["with"]["wiki"] == "story"
    assert "wiki_api_key" in data["jobs"]["publish"]["secrets"]
    assert "codenib pages badge --site-url" in capsys.readouterr().out

    # Pages derived from the index alone are no longer published.
    with pytest.raises(SystemExit):
        cli.run(["pages", "init", str(root), "--wiki", "index", "--force"])


@pytest.mark.parametrize(
    "argv",
    [
        ["--secret", "GITHUB_TOKEN"],
        ["--secret", "bad name"],
        ["--ref", "../evil"],
        ["--model", "x; rm -rf /"],
    ],
)
def test_init_rejects_values_that_would_break_the_workflow(tmp_path, capsys, argv):
    root = _repo(tmp_path)

    assert cli.run(["pages", "init", str(root), *argv]) == 2
    assert not (root / pages_setup.WORKFLOW_PATH).exists()


def test_badge_uses_the_origin_or_an_explicit_address(tmp_path, capsys):
    root = _repo(tmp_path, origin="https://github.com/owner/owner.github.io")
    assert cli.run(["pages", "badge", str(root)]) == 0
    assert capsys.readouterr().out.strip() == (
        "[![Wiki: CodeNib](https://owner.github.io/wiki-badge.svg)]"
        "(https://owner.github.io/)"
    )
    assert cli.run(["pages", "badge", "--site-url", "https://docs.example.org"]) == 0
    assert "https://docs.example.org/wiki-badge.svg" in capsys.readouterr().out
    bare = _repo(tmp_path / "bare", origin=None)
    assert cli.run(["pages", "badge", str(bare)]) == 2


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("https://github.com/a/b.git", "a/b"),
        ("https://token@github.com/a/b", "a/b"),
        ("ssh://git@github.com/a/b.c.git", "a/b.c"),
        ("git@github.com:a-1/b_c", "a-1/b_c"),
        ("https://gitlab.com/a/b.git", None),
    ],
)
def test_github_repository_reads_common_remote_forms(tmp_path, origin, expected):
    assert pages_setup.github_repository(_repo(tmp_path, origin=origin)) == expected


def test_cli_defaults_match_the_workflow_defaults():
    assert cli._DEFAULT_PAGES_MODEL == pages_setup.DEFAULT_WIKI_MODEL
    assert cli._DEFAULT_PAGES_SECRET == pages_setup.DEFAULT_WIKI_SECRET
    workflow = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "codenib-pages.yml"
        ).read_text()
    )
    inputs = workflow[True]["workflow_call"]["inputs"]
    assert inputs["model"]["default"] == pages_setup.DEFAULT_WIKI_MODEL


@pytest.fixture
def build(tmp_path, monkeypatch):
    """`pages build` with its index, model and export replaced by recorders."""

    calls: dict = {"exported": None, "saved": None, "carried": None}
    manifest = tmp_path / "repo_manifest.json"
    monkeypatch.setattr(cli, "_require_modules", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "resolve_manifest_path", lambda _repo: manifest)
    monkeypatch.setattr(
        "codenib.compiler.manifest.RepoManifest.load",
        lambda _path: SimpleNamespace(commit="b" * 40),
    )
    local = SimpleNamespace(
        data_dir=tmp_path / "wiki",
        config_path=tmp_path / "wiki" / "config.yaml",
        repo_id="demo",
    )

    def prepare(repo, manifest_path, **kwargs):
        calls["prepare"] = kwargs
        return local

    monkeypatch.setattr("codenib.web.local.prepare_local_wiki", prepare)
    builder = object()
    monkeypatch.setattr(cli, "_local_agent_wiki", lambda _local: (None, "m", builder))

    def carry(wiki, snapshot, repo, commit):
        calls["carried"] = (wiki, snapshot.name, commit)
        return SimpleNamespace(
            outline_reused=True, previous_commit="a" * 40, reused=["x"], rewritten=[]
        )

    monkeypatch.setattr("codenib.wiki.carry_forward.carry_forward", carry)

    def save(wiki, snapshot, commit):
        calls["saved"] = commit
        return 1

    monkeypatch.setattr("codenib.wiki.carry_forward.save_snapshot", save)

    def export(config, repo_id, output, **kwargs):
        calls["exported"] = (repo_id, output, kwargs)
        return SimpleNamespace(output_dir=output, page_count=2)

    monkeypatch.setattr("codenib.web.static_export.export_cached_wiki", export)
    calls["report"] = {"passed": True, "expected_pages": 2, "ready_pages": 2}
    monkeypatch.setattr(
        cli, "_audit_local_wiki", lambda _local, _parts: calls["report"]
    )
    monkeypatch.setenv("WIKI_KEY", "sk-test")
    argv = [
        "pages",
        "build",
        str(tmp_path),
        "--site-output",
        str(tmp_path / "site"),
        "--base-path",
        "/demo",
        "--site-url",
        "https://o.github.io/demo",
        "--repository",
        "owner/demo",
        "--model",
        "openrouter/x/y",
        "--api-key-env",
        "WIKI_KEY",
    ]
    return calls, argv


def test_build_keeps_unchanged_pages_and_exports_a_passing_wiki(build, capsys):
    calls, argv = build

    assert cli.run(argv) == 0

    assert calls["prepare"]["repository_slug"] == "owner/demo"
    assert calls["prepare"]["api_key_env"] == "WIKI_KEY"
    assert calls["carried"][1:] == ("carry-forward.json", "b" * 40)
    assert calls["saved"] == "b" * 40
    repo_id, _output, options = calls["exported"]
    assert repo_id == "demo"
    assert options["base_path"] == "/demo"
    assert options["site_url"] == "https://o.github.io/demo"
    assert "kept 1 unchanged page(s) from aaaaaaaaaaaa" in capsys.readouterr().out


def test_build_publishes_nothing_when_the_wiki_fails_its_checks(build, capsys):
    calls, argv = build
    calls["report"] = {"passed": False, "expected_pages": 2, "ready_pages": 1}

    assert cli.run(argv) == 3

    assert calls["exported"] is None and calls["saved"] is None
    assert "was left as it was" in capsys.readouterr().out


def test_full_build_skips_the_earlier_wiki(build):
    calls, argv = build

    assert cli.run([*argv, "--full"]) == 0
    assert calls["carried"] is None


def test_listed_openrouter_models_run_with_reasoning_off_like_the_hosted_wiki():
    deepseek = "openrouter/deepseek/deepseek-v4.1-flash"

    assert cli._pages_model_options(deepseek, {}) == {
        "extra_body": {"reasoning": {"enabled": False}}
    }
    # An explicit choice wins, and other options are kept.
    explicit = {"extra_body": {"reasoning": {"effort": "low"}}, "top_p": 0.9}
    assert cli._pages_model_options(deepseek, explicit) == explicit
    merged = cli._pages_model_options(deepseek, {"extra_body": {"seed": 1}})
    assert merged["extra_body"] == {"seed": 1, "reasoning": {"enabled": False}}
    # Claude 5.x accepts no reasoning switch; other routes are left alone.
    assert cli._pages_model_options("openrouter/anthropic/claude-haiku-5.5", {}) == {}
    assert cli._pages_model_options("openai/gpt-4o-mini", {}) == {}
    assert cli._pages_model_options("openrouter/unknown/model", {}) == {}


def test_build_sends_the_reasoning_setting_to_the_local_wiki(build):
    calls, argv = build
    argv = [*argv]
    argv[argv.index("openrouter/x/y")] = "openrouter/deepseek/deepseek-v4.1-flash"

    assert cli.run(argv) == 0
    assert calls["prepare"]["model_options"] == {
        "extra_body": {"reasoning": {"enabled": False}}
    }


def _report(ready: dict) -> dict:
    return {
        "passed": all(ready.values()),
        "expected_pages": len(ready),
        "ready_pages": sum(ready.values()),
        "details": [{"id": key, "ready": value} for key, value in ready.items()],
    }


def test_build_publishes_the_pages_that_passed(build, capsys):
    calls, argv = build
    ready = {"overview": True, "a": True, "b": True, "c": True, "d": False}
    calls["report"] = _report(ready)

    assert cli.run(argv) == 0

    _repo_id, _output, options = calls["exported"]
    assert options["only_pages"] == {"overview", "a", "b", "c"}
    out = capsys.readouterr().out
    assert "Left out:    d (written again on the next commit)" in out


@pytest.mark.parametrize(
    "ready",
    [
        # Too few pages passed.
        {"overview": True, "a": True, "b": False, "c": False, "d": False},
        # The Overview is the entry; without it nothing is published.
        {"overview": False, "a": True, "b": True, "c": True, "d": True},
    ],
)
def test_build_publishes_nothing_without_the_overview_or_enough_pages(build, ready):
    calls, argv = build
    calls["report"] = _report(ready)

    assert cli.run(argv) == 3
    assert calls["exported"] is None


def test_a_passing_build_exports_the_whole_outline(build):
    calls, argv = build
    calls["report"] = _report({"overview": True, "a": True})

    assert cli.run(argv) == 0
    assert calls["exported"][2]["only_pages"] is None
