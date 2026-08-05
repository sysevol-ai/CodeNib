# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ACTION_PATH = ROOT / ".github" / "actions" / "publish" / "action.yml"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "codenib-pages.yml"
SMOKE_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "codenib-publish-smoke.yml"
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
            "INPUT_PRESET": "fast",
            "INPUT_PYTHON_VERSION": "3.12",
            "INPUT_REPOSITORY": "",
            "INPUT_REPOSITORY_PATH": str(ROOT),
            "INPUT_RETENTION_DAYS": "14",
            "INPUT_REVISION": "",
            "INPUT_SITE_OUTPUT": "",
            "INPUT_UPLOAD_CONTEXT": "true",
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
    token_expression = publish["env"]["GITHUB_TOKEN"]
    assert "inputs.preset == 'semantic'" in token_expression
    assert "embedding_provider == 'github_models'" in token_expression
    assert "github.token" in token_expression
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
    assert "embedding_provider=" in output
    assert "extras=" in output
    assert f"source_commit={source_commit}" in output


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
    assert "INPUT_PRESET" in script
    assert "INPUT_EMBEDDING_PROVIDER" in script
    assert "INPUT_EMBEDDING_ENDPOINT" in script
    assert "API_KEY" not in script
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
    build = workflow["jobs"]["build"]
    condition = build["if"]

    assert "pull_request_target" in condition
    assert "head.repo.full_name == github.repository" in condition
    assert build["permissions"] == {
        "contents": "read",
        "models": "read",
        "pages": "write",
    }
    exact_checkout = next(
        step
        for step in build["steps"]
        if step.get("name") == "Checkout the exact CodeNib workflow revision"
    )
    assert exact_checkout["with"]["repository"] == "${{ job.workflow_repository }}"
    assert exact_checkout["with"]["ref"] == "${{ job.workflow_sha }}"
    assert exact_checkout["with"]["persist-credentials"] == "false"
    secret_steps = [
        step for step in build["steps"] if "secrets.embedding_api_key" in str(step)
    ]
    assert [step["name"] for step in secret_steps] == [
        "Build Wiki and context artifact"
    ]
    publish = secret_steps[0]
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
    assert publish["with"]["upload-context"] == "false"
    assert publish["with"]["repository-path"] == "${{ steps.fixture.outputs.path }}"
    _assert_external_actions_are_sha_pinned(workflow)
