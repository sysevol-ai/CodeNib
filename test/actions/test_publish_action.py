# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ACTION_PATH = ROOT / ".github" / "actions" / "publish" / "action.yml"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "codenib-pages.yml"
SMOKE_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "codenib-publish-smoke.yml"
# sysevol-ai/wiki-action, the Marketplace action, calls this action at a
# pinned commit with these inputs and publishes its `site-path` output.
# Renaming any of them would break that action's next release.
WIKI_ACTION_INPUTS = frozenset(
    {
        "repository-path",
        "base-path",
        "site-url",
        "wiki",
        "wiki-model",
        "wiki-api-key-env",
        "wiki-full-rebuild",
        "wiki-graph",
        "upload-context",
    }
)
PINNED_ACTION_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+@[0-9a-f]{40}$")


def _load(path: Path) -> dict[str, Any]:
    value = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(value, dict)
    return value


def _steps(value: dict[str, Any]) -> list[dict[str, Any]]:
    if "runs" in value:
        return list(value["runs"]["steps"])
    result = []
    for job in value["jobs"].values():
        result.extend(job.get("steps") or [])
    return result


def _assert_external_actions_are_sha_pinned(value: dict[str, Any]) -> None:
    for step in _steps(value):
        uses = step.get("uses")
        if not uses or str(uses).startswith("./"):
            continue
        assert PINNED_ACTION_RE.fullmatch(str(uses)), uses


def _run_resolve_step(
    tmp_path: Path, **env_overrides: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    action = _load(ACTION_PATH)
    resolve = next(step for step in _steps(action) if step.get("id") == "inputs")
    output_path = tmp_path / "github-output"
    env = os.environ.copy()
    env.update(
        {
            "ACTION_REF": "0123456789abcdef",
            "GITHUB_ACTION_PATH": str(ACTION_PATH.parent),
            "GITHUB_OUTPUT": str(output_path),
            "GITHUB_REPOSITORY": "example/project",
            "GITHUB_REPOSITORY_ID": "123456",
            "GITHUB_SHA": "a" * 40,
            "INPUT_ARTIFACT_NAME": "",
            "INPUT_BASE_PATH": "/",
            "INPUT_CACHE": "true",
            "INPUT_CONTEXT_OUTPUT": "",
            "INPUT_EMBEDDING_DIMENSION": "",
            "INPUT_EMBEDDING_ENDPOINT": "",
            "INPUT_EMBEDDING_MODEL": "",
            "INPUT_EMBEDDING_PROVIDER": "",
            "INPUT_PRESET": action["inputs"]["preset"]["default"],
            "INPUT_PYTHON_VERSION": "3.12",
            "INPUT_REPOSITORY": "",
            "INPUT_REPOSITORY_PATH": str(ROOT),
            "INPUT_RETENTION_DAYS": "14",
            "INPUT_REVISION": "",
            "INPUT_SITE_OUTPUT": "",
            "INPUT_UPLOAD_CONTEXT": "true",
            "INPUT_WIKI": action["inputs"]["wiki"]["default"],
            "INPUT_WIKI_KEY_ENV": "CODENIB_ACTION_WIKI_KEY",
            "INPUT_WIKI_GRAPH": action["inputs"]["wiki-graph"]["default"],
            "RUNNER_OS": "Linux",
            "RUNNER_TEMP": str(tmp_path),
        }
    )
    env.update(env_overrides)
    result = subprocess.run(
        ["bash", "-c", str(resolve["run"])],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    output = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
    return result, output


def test_publish_action_has_secret_free_inputs_and_stable_outputs() -> None:
    action = _load(ACTION_PATH)

    assert action["runs"]["using"] == "composite"
    assert "api-key" not in action["inputs"]
    assert "embedding-api-key-env" in action["inputs"]
    assert {
        "site-path",
        "context-path",
        "context-manifest",
        "artifact-name",
        "cache-hit",
        "cache-key",
        "source-commit",
    } <= set(action["outputs"])
    assert action["inputs"]["preset"]["default"] == "fast"
    resolve = next(step for step in _steps(action) if step.get("id") == "inputs")
    assert "fast|semantic)" in resolve["run"]
    assert "graph|full" not in resolve["run"]
    _assert_external_actions_are_sha_pinned(action)


def test_publish_action_shell_blocks_parse_with_bash() -> None:
    action = _load(ACTION_PATH)

    for index, step in enumerate(_steps(action)):
        script = str(step.get("run") or "")
        if not script:
            continue
        result = subprocess.run(
            ["bash", "-n"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"step {index}: {result.stderr}"


def test_publish_action_builds_frontend_before_source_install() -> None:
    action = _load(ACTION_PATH)
    names = [step.get("name") for step in _steps(action)]

    assert names.index("Build static frontend") < names.index("Install CodeNib")
    resolve = next(step for step in _steps(action) if step.get("id") == "inputs")
    assert "web/dist" in resolve["run"]
    # npm ci in the frontend build reads the restored npm cache.
    assert names.index("Cache npm downloads") < names.index("Build static frontend")


def test_publish_action_keeps_untrusted_inputs_out_of_shell_source() -> None:
    action = _load(ACTION_PATH)

    for step in _steps(action):
        script = str(step.get("run") or "")
        assert "${{ inputs." not in script
    publish = next(
        step
        for step in _steps(action)
        if step.get("name") == "Publish repository context"
    )
    assert "GITHUB_TOKEN" not in publish["env"]
    assert "github.token" not in str(publish)
    assert "command=(" in publish["run"]
    assert '"${command[@]}"' in publish["run"]


def test_publish_action_resolves_valid_inputs(tmp_path: Path) -> None:
    result, output = _run_resolve_step(tmp_path)
    source_commit = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert result.returncode == 0, result.stderr
    assert (
        f"artifact_name=codenib-context-example-project-{source_commit[:12]}" in output
    )
    assert f"site_path={tmp_path / 'codenib-site'}" in output
    assert f"context_path={tmp_path / 'codenib-context'}" in output
    assert "embedding_provider=\n" in output
    assert "extras=agent,graph\n" in output
    assert f"source_path={ROOT}" in output
    assert f"source_commit={source_commit}" in output


def test_publish_action_keys_the_npm_cache_where_a_remote_action_runs(
    tmp_path: Path,
) -> None:
    # Used remotely, the action runs from the runner's _actions directory,
    # outside GITHUB_WORKSPACE, where setup-node cannot hash a lockfile for its
    # npm cache and fails the run; the resolve step keys that cache instead.
    action = _load(ACTION_PATH)
    steps = _steps(action)
    node = next(step for step in steps if "setup-node" in str(step.get("uses")))
    assert not {"cache", "cache-dependency-path"} & set(node.get("with") or {})
    assert "hashFiles(" not in ACTION_PATH.read_text(encoding="utf-8")
    npm = next(step for step in steps if step.get("name") == "Cache npm downloads")
    assert npm["with"] == {
        "path": "~/.npm",
        "key": "${{ steps.inputs.outputs.npm_cache_key }}",
    }

    remote = tmp_path / "_actions" / "sysevol-ai" / "CodeNib" / ("b" * 40)
    for relative in (
        ".github/actions/publish/action.yml",
        "pyproject.toml",
        "web/package-lock.json",
    ):
        target = remote / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    result, output = _run_resolve_step(
        tmp_path, GITHUB_ACTION_PATH=str(remote / ".github" / "actions" / "publish")
    )
    lock = hashlib.sha256((ROOT / "web" / "package-lock.json").read_bytes())

    assert result.returncode == 0, result.stderr
    assert f"source_path={remote.resolve()}\n" in output
    assert f"npm_cache_key=codenib-npm-Linux-{lock.hexdigest()}\n" in output


def test_publish_action_defaults_semantic_to_local_huggingface(
    tmp_path: Path,
) -> None:
    result, output = _run_resolve_step(tmp_path, INPUT_PRESET="semantic")

    assert result.returncode == 0, result.stderr
    assert "embedding_provider=huggingface\n" in output
    assert "extras=semantic,agent,graph\n" in output


def test_publish_action_selects_remote_extra_for_openai(tmp_path: Path) -> None:
    result, output = _run_resolve_step(
        tmp_path,
        INPUT_PRESET="semantic",
        INPUT_EMBEDDING_PROVIDER="openai",
    )

    assert result.returncode == 0, result.stderr
    assert "embedding_provider=openai\n" in output
    assert "extras=semantic-remote,agent,graph\n" in output


def test_publish_action_rejects_retired_github_models(tmp_path: Path) -> None:
    result, output = _run_resolve_step(
        tmp_path,
        INPUT_PRESET="semantic",
        INPUT_EMBEDDING_PROVIDER="github_models",
    )

    assert result.returncode == 2
    assert "unsupported embedding provider" in result.stderr
    assert output == ""


def test_publish_action_rejects_output_command_injection(tmp_path: Path) -> None:
    result, output = _run_resolve_step(
        tmp_path,
        INPUT_EMBEDDING_PROVIDER="openai\nartifact_name=forged",
    )

    assert result.returncode == 2
    assert "must not contain a line break" in result.stderr
    assert "forged" not in output


def test_publish_action_rejects_unsupported_provider(tmp_path: Path) -> None:
    result, output = _run_resolve_step(
        tmp_path,
        INPUT_PRESET="semantic",
        INPUT_EMBEDDING_PROVIDER="unknown",
    )

    assert result.returncode == 2
    assert "unsupported embedding provider" in result.stderr
    assert output == ""


def test_publish_action_rejects_overlapping_outputs(tmp_path: Path) -> None:
    site_path = tmp_path / "publish"
    result, output = _run_resolve_step(
        tmp_path,
        INPUT_SITE_OUTPUT=str(site_path),
        INPUT_CONTEXT_OUTPUT=str(site_path / "context"),
    )

    assert result.returncode == 2
    assert "site-output and context-output must not overlap" in result.stderr
    assert output == ""


def test_publish_action_cache_identity_is_public_and_commit_addressed() -> None:
    action = _load(ACTION_PATH)
    resolve = next(step for step in _steps(action) if step.get("id") == "inputs")
    script = resolve["run"]

    assert "git -C" in script
    assert "source_commit" in script
    assert "GITHUB_SHA" not in script
    assert "tr '[:upper:]' '[:lower:]'" in script
    assert "INPUT_PRESET" in script
    assert "INPUT_EMBEDDING_PROVIDER" in script
    assert "INPUT_EMBEDDING_ENDPOINT" in script
    assert "API_KEY" not in script
    model_cache = next(
        step
        for step in _steps(action)
        if step.get("name") == "Cache local embedding model"
    )
    assert model_cache["with"]["path"] == "~/.cache/huggingface"
    assert model_cache["with"]["key"] == (
        "${{ steps.inputs.outputs.embedding_cache_key }}"
    )
    assert "embedding_provider == 'huggingface'" in model_cache["if"]
    restore = next(
        step
        for step in _steps(action)
        if step.get("name") == "Restore incremental repository state"
    )
    save = next(
        step
        for step in _steps(action)
        if step.get("name") == "Save incremental repository state"
    )
    assert restore["with"]["key"] == "${{ steps.inputs.outputs.cache_key }}"
    assert save["with"]["key"] == "${{ steps.inputs.outputs.cache_key }}"


def test_reusable_workflow_is_fork_safe_and_binds_its_exact_revision() -> None:
    workflow = _load(WORKFLOW_PATH)
    assert workflow["on"]["workflow_call"]["inputs"]["preset"]["default"] == "fast"
    build = workflow["jobs"]["build"]
    condition = build["if"]

    assert "pull_request_target" in condition
    assert "head.repo.full_name == github.repository" in condition
    assert build["permissions"] == {
        "contents": "read",
        "pages": "write",
    }
    caller_checkout = next(
        step
        for step in build["steps"]
        if step.get("name") == "Checkout caller repository"
    )
    assert caller_checkout["with"]["path"] == "repository"
    exact_checkout = next(
        step
        for step in build["steps"]
        if step.get("name") == "Checkout the exact CodeNib workflow revision"
    )
    assert exact_checkout["with"]["repository"] == "${{ job.workflow_repository }}"
    assert exact_checkout["with"]["ref"] == "${{ job.workflow_sha }}"
    assert exact_checkout["with"]["path"] == ".codenib-action"
    assert exact_checkout["with"]["persist-credentials"] == "false"
    secret_steps = [
        step for step in build["steps"] if "secrets.embedding_api_key" in str(step)
    ]
    assert [step["name"] for step in secret_steps] == [
        "Build Wiki and context artifact"
    ]
    publish = secret_steps[0]
    assert publish["with"]["repository-path"] == "repository"
    secret_expression = publish["env"]["CODENIB_ACTION_EMBEDDING_KEY"]
    assert "inputs.preset == 'semantic'" in secret_expression
    assert "inputs.embedding-provider == 'openai'" in secret_expression
    _assert_external_actions_are_sha_pinned(workflow)


def test_reusable_workflow_deploys_only_the_published_site() -> None:
    workflow = _load(WORKFLOW_PATH)
    build = workflow["jobs"]["build"]
    upload = next(
        step
        for step in build["steps"]
        if step.get("name") == "Upload GitHub Pages artifact"
    )
    deploy = workflow["jobs"]["deploy"]

    assert upload["with"]["path"] == "${{ steps.publish.outputs.site-path }}"
    assert deploy["needs"] == "build"
    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}
    assert deploy["environment"]["name"] == "github-pages"
    assert workflow["concurrency"]["cancel-in-progress"] == "false"


def test_hosted_publish_smoke_is_narrow_and_sha_pinned() -> None:
    workflow = _load(SMOKE_WORKFLOW_PATH)
    job = workflow["jobs"]["no-model"]

    assert "head.repo.full_name == github.repository" in job["if"]
    publish = next(
        step
        for step in job["steps"]
        if step.get("name") == "Exercise local publish Action"
    )
    assert publish["with"]["cache"] == "false"
    assert "preset" not in publish["with"]  # Exercise the actual Action default.
    assert publish["with"]["upload-context"] == "false"
    assert publish["with"]["repository-path"] == "${{ steps.fixture.outputs.path }}"
    _assert_external_actions_are_sha_pinned(workflow)


@pytest.mark.parametrize(
    ("overrides", "extras"),
    [
        ({"INPUT_WIKI": "story"}, "agent,graph"),
        ({"INPUT_WIKI": "story", "INPUT_WIKI_GRAPH": "false"}, "agent"),
        ({"INPUT_WIKI": "story", "INPUT_PRESET": "semantic"}, "semantic,agent,graph"),
    ],
)
def test_story_wiki_installs_the_model_client_and_graph(
    tmp_path: Path, overrides: dict[str, str], extras: str
) -> None:
    result, output = _run_resolve_step(tmp_path, **overrides)

    assert result.returncode == 0, result.stderr
    assert f"extras={extras}\n" in output


def test_story_wiki_rejects_unknown_modes_and_keeps_its_own_cache(
    tmp_path: Path,
) -> None:
    result, _output = _run_resolve_step(tmp_path, INPUT_WIKI="essay")
    assert result.returncode == 2
    assert "wiki must be story" in result.stderr

    (tmp_path / "index").mkdir()
    (tmp_path / "story").mkdir()
    _index, index_output = _run_resolve_step(
        tmp_path / "index", INPUT_WIKI="index", CODENIB_ACTION_INDEX_WIKI_CHECK="1"
    )
    _story, story_output = _run_resolve_step(tmp_path / "story", INPUT_WIKI="story")

    def prefix(output: str) -> str:
        return next(
            line for line in output.splitlines() if line.startswith("cache_prefix=")
        )

    # Story state carries Wiki pages; an index-only run never restores it.
    assert prefix(index_output) != prefix(story_output)


def test_published_wikis_are_model_written(tmp_path: Path) -> None:
    action = _load(ACTION_PATH)
    assert action["inputs"]["wiki"]["default"] == "story"

    # Pages derived from the index alone are built only by CodeNib's own check.
    removed, _output = _run_resolve_step(tmp_path, INPUT_WIKI="index")
    assert removed.returncode == 2
    assert "wiki: index was removed" in removed.stderr

    no_key, _output = _run_resolve_step(tmp_path, INPUT_WIKI_KEY_ENV="")
    assert no_key.returncode == 2
    assert "No model key" in no_key.stderr

    smoke = _load(SMOKE_WORKFLOW_PATH)
    publish = next(
        step
        for step in smoke["jobs"]["no-model"]["steps"]
        if step.get("name") == "Exercise local publish Action"
    )
    assert publish["with"]["wiki"] == "index"
    assert publish["env"]["CODENIB_ACTION_INDEX_WIKI_CHECK"] == "1"


def test_story_wiki_runs_before_the_state_is_saved() -> None:
    action = _load(ACTION_PATH)
    steps = _steps(action)
    names = [step.get("name") for step in steps]

    graph = names.index("Index the code graph for the Wiki")
    write = names.index("Write the model-written Wiki")
    assert (
        names.index("Publish repository context")
        < graph
        < write
        < names.index("Save incremental repository state")
    )
    assert steps[graph]["continue-on-error"] == "true"
    script = steps[write]["run"]
    assert "codenib pages build" in script and '"${command[@]}"' in script
    # A Wiki that fails its checks stops the run; the last deployment stays.
    assert "status -eq 3" in script and "::error" in script and "exit 1" in script
    assert "::warning" not in script
    assert "WIKI_KEY_ENV" in steps[write]["env"]
    assert not any("wiki_api_key" in str(value) for value in steps[write]["env"])
    # Each page's progress line reaches the log as it is written.
    assert steps[write]["env"]["PYTHONUNBUFFERED"] == "1"


_FAKE_CODENIB = """#!/usr/bin/env bash
echo "$*" >> "$CALLS"
case "$1" in
  toolchain)
    echo "  [MISSING] Java graph scip-java: not found"
    exit "${TOOLCHAIN_STATUS:-0}"
    ;;
  index)
    echo "  symbol_graph   fresh (0.87s)${GRAPH_NOTE:-}"
    exit "${INDEX_STATUS:-0}"
    ;;
esac
"""


def _run_graph_step(tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    action = _load(ACTION_PATH)
    step = next(
        step
        for step in _steps(action)
        if step.get("name") == "Index the code graph for the Wiki"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "codenib"
    fake.write_text(_FAKE_CODENIB, encoding="utf-8")
    fake.chmod(0o755)
    return subprocess.run(
        ["bash", "-c", step["run"]],
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CALLS": str(tmp_path / "calls"),
            "RUNNER_TEMP": str(tmp_path),
            "INPUT_REPOSITORY_PATH": ".",
            **env,
        },
        text=True,
        capture_output=True,
        check=False,
    )


def test_a_missing_graph_provider_leaves_only_its_language_out(
    tmp_path: Path,
) -> None:
    # One small Java file without scip-java used to skip the whole graph:
    # the failed toolchain install stopped the step before indexing.
    result = _run_graph_step(
        tmp_path,
        TOOLCHAIN_STATUS="1",
        GRAPH_NOTE=" [partial: python; unavailable: java]",
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
    assert calls == ["toolchain install . --scope graph", "index . --preset graph"]
    assert (
        "::warning title=CodeNib Wiki::No graph provider for java on this runner"
        in result.stdout
    )


def test_the_graph_step_warns_only_when_a_language_is_left_out(
    tmp_path: Path,
) -> None:
    result = _run_graph_step(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "::warning" not in result.stdout


def test_the_graph_step_reports_a_failed_index(tmp_path: Path) -> None:
    result = _run_graph_step(tmp_path, INDEX_STATUS="2")

    assert result.returncode == 2


def test_reusable_workflow_passes_the_wiki_key_only_for_story_pages() -> None:
    workflow = _load(WORKFLOW_PATH)
    build = workflow["jobs"]["build"]
    publish = next(step for step in build["steps"] if step.get("id") == "publish")

    assert "secrets.wiki_api_key" in publish["env"]["CODENIB_ACTION_WIKI_KEY"]
    assert "inputs.wiki == 'story'" in publish["env"]["CODENIB_ACTION_WIKI_KEY"]
    assert "CODENIB_ACTION_WIKI_KEY" in publish["with"]["wiki-api-key-env"]
    assert publish["with"]["wiki"] == "${{ inputs.wiki }}"
    assert workflow["on"]["workflow_call"]["inputs"]["wiki"]["default"] == "story"
    assert "wiki_api_key" in workflow["on"]["workflow_call"]["secrets"]
    assert "inputs.wiki == 'story'" in build["timeout-minutes"]


def test_publish_action_keeps_what_the_marketplace_action_uses() -> None:
    action = _load(ACTION_PATH)

    assert WIKI_ACTION_INPUTS <= set(action["inputs"])
    assert "site-path" in action["outputs"]
