# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import smoke_release_services
from scripts.verify_release_artifacts import (
    ReleaseValidationError,
    expected_tag,
    project_identity,
    validate_readme_citation,
    validate_readme_mcp_ownership,
    validate_tag,
)


def test_release_service_smoke_accepts_authenticated_source_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calculator.py").write_text("VALUE = 1\n", encoding="utf-8")
    git_commands: list[tuple[str, ...]] = []

    def record_git(command, *, cwd, env=None):
        assert cwd == repo
        git_commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(smoke_release_services, "_run", record_git)
    monkeypatch.setattr(
        smoke_release_services.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            2,
            "",
            "error: repository source content does not match the manifest\n",
        ),
    )

    smoke_release_services._assert_stale_snapshot_rejected(
        tmp_path,
        repo,
        executable="codenib",
        env={},
    )

    assert git_commands == [
        ("git", "add", "calculator.py"),
        ("git", "commit", "--quiet", "-m", "advance fixture"),
    ]


def test_project_identity_and_tag_match_release_metadata() -> None:
    root = Path(__file__).resolve().parents[1]
    name, version = project_identity(root / "pyproject.toml")

    assert name == "codenib"
    assert expected_tag(version) == "v0.2.1"
    validate_tag("v0.2.1", version)


def test_release_tag_must_match_project_version() -> None:
    with pytest.raises(ReleaseValidationError, match="does not match"):
        validate_tag("v0.1.0", "0.2.0")


def test_packaged_readme_requires_the_arxiv_citation() -> None:
    citation = """
## Citation
https://arxiv.org/abs/2607.25431
@misc{yu2026codenibmultiviewdataserving,
"""
    validate_readme_citation(citation)

    with pytest.raises(ReleaseValidationError, match="citation markers"):
        validate_readme_citation("# CodeNib\n")


def test_packaged_readme_requires_mcp_registry_ownership() -> None:
    validate_readme_mcp_ownership("<!-- mcp-name: ai.codenib/codenib -->")

    with pytest.raises(ReleaseValidationError, match="MCP ownership marker"):
        validate_readme_mcp_ownership("# CodeNib\n")


def test_stable_release_notes_describe_the_codegraph_product_path() -> None:
    root = Path(__file__).resolve().parents[1]
    notes = (root / "docs" / "releases" / "0.2.1.md").read_text(encoding="utf-8")

    assert '"codenib[graph,mcp]==0.2.1"' in notes
    assert "codenib codegraph init" in notes
    assert "explore_context" in notes
    assert "dependency_subgraph" in notes
    assert "Codex" in notes
    assert "Claude Code" in notes
    assert "test-files.pythonhosted.org" not in notes
    assert "--extra-index-url" not in notes
    assert "--index-url" not in notes


@pytest.mark.parametrize(
    "relative_path",
    (
        "README.md",
        "docs/agent_integrations.md",
        "docs/codegraph.md",
        "docs/index.md",
        "docs/mcp.md",
        "docs/quickstart.md",
        "docs/scip_index.md",
        "docs/web_demo.md",
    ),
)
def test_public_install_commands_select_the_current_stable_release(
    relative_path: str,
) -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / relative_path).read_text(encoding="utf-8")
    install_lines = [
        line.strip()
        for line in text.splitlines()
        if line.lstrip().startswith(("pip install ", "python -m pip install "))
        and "codenib" in line
        and " -e " not in line
    ]

    assert install_lines
    assert "CODENIB_ALPHA_WHEEL=" not in text
    assert all("==0.2.1" in line for line in install_lines)


def test_registry_publishers_use_separate_workflows() -> None:
    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"

    def load(name: str) -> dict[str, object]:
        with (workflows / name).open(encoding="utf-8") as handle:
            return yaml.load(handle, Loader=yaml.BaseLoader)

    def publisher_step(job: dict[str, object]) -> dict[str, object]:
        return next(
            step
            for step in job["steps"]
            if step.get("uses", "").startswith("pypa/gh-action-pypi-publish@")
        )

    production = load("release.yml")
    test = load("release-test.yml")
    verification = load("release-verify.yml")

    production_verification_job = {
        "name": "Verify release artifacts",
        "uses": "./.github/workflows/release-verify.yml",
        "with": {"full": "${{ github.event_name != 'pull_request' }}"},
        "permissions": {"contents": "read"},
    }
    test_verification_job = {
        "name": "Verify release artifacts",
        "uses": "./.github/workflows/release-verify.yml",
        "permissions": {"contents": "read"},
    }
    assert production["jobs"]["verify"] == production_verification_job
    assert test["jobs"]["verify"] == test_verification_job
    assert production["concurrency"]["cancel-in-progress"] == (
        "${{ github.ref_type != 'tag' }}"
    )

    assert set(production["jobs"]) == {
        "verify",
        "registry-auth-preflight",
        "publish-pypi",
        "verify-pypi-install",
        "publish-mcp-registry",
        "github-release",
    }
    registry_preflight = production["jobs"]["registry-auth-preflight"]
    assert registry_preflight["if"] == (
        "github.ref_type == 'tag' || github.event_name == 'workflow_dispatch'"
    )
    assert registry_preflight["needs"] == "verify"
    assert registry_preflight["runs-on"] == "ubuntu-latest"
    assert registry_preflight["environment"]["name"] == "mcp-registry-publish"
    preflight_steps = {step["name"]: step for step in registry_preflight["steps"]}
    preflight_download = preflight_steps["Install verified MCP publisher"]["run"]
    assert "sha256sum --check" in preflight_download
    assert "--connect-timeout 10 --max-time 90" in preflight_download
    assert "login dns" in preflight_steps["Verify branded namespace ownership"]["run"]
    production_publisher = production["jobs"]["publish-pypi"]
    assert production_publisher["needs"] == "registry-auth-preflight"
    assert production_publisher["environment"]["name"] == "pypi"
    assert production_publisher["permissions"] == {"id-token": "write"}
    assert "password" not in publisher_step(production_publisher).get("with", {})
    assert "PYPI_API_TOKEN" not in str(production_publisher)
    pypi_install = production["jobs"]["verify-pypi-install"]
    assert pypi_install["needs"] == "publish-pypi"
    assert pypi_install["runs-on"] == "ubuntu-latest"
    pypi_install_steps = {step["name"]: step for step in pypi_install["steps"]}
    public_download = pypi_install_steps["Download and match the published wheel"][
        "run"
    ]
    assert '"codenib==$VERSION"' in public_download
    assert "sha256sum" in public_download
    assert (
        "scripts/smoke_release_services.py"
        in pypi_install_steps["Exercise public Wiki and MCP services"]["run"]
    )
    registry_publisher = production["jobs"]["publish-mcp-registry"]
    assert registry_publisher["needs"] == "verify-pypi-install"
    assert registry_publisher["runs-on"] == "ubuntu-latest"
    assert registry_publisher["environment"]["name"] == "mcp-registry-publish"
    assert registry_publisher["permissions"] == {"contents": "read"}
    registry_steps = {step["name"]: step for step in registry_publisher["steps"]}
    assert "--check-pypi" in registry_steps["Wait for the exact PyPI package"]["run"]
    assert (
        "sha256sum --check" in registry_steps["Install verified MCP publisher"]["run"]
    )
    assert (
        "--connect-timeout 10 --max-time 90"
        in registry_steps["Install verified MCP publisher"]["run"]
    )
    assert registry_steps["Authenticate branded namespace"]["env"] == {
        "MCP_PRIVATE_KEY": "${{ secrets.MCP_PRIVATE_KEY }}"
    }
    assert "--check-registry" in registry_steps["Verify Registry discovery"]["run"]
    assert production["jobs"]["github-release"]["needs"] == [
        "publish-pypi",
        "publish-mcp-registry",
    ]
    release_steps = {
        step["name"]: step for step in production["jobs"]["github-release"]["steps"]
    }
    assert release_steps["Resolve release channel"]["id"] == "channel"
    create_release = release_steps["Create release"]["run"]
    assert "RELEASE_FLAGS=(--latest)" in create_release
    assert "RELEASE_FLAGS=(--prerelease)" in create_release
    assert '"${RELEASE_FLAGS[@]}"' in create_release

    assert set(test["jobs"]) == {
        "verify",
        "publish-testpypi",
        "verify-testpypi-install",
    }
    test_publisher = test["jobs"]["publish-testpypi"]
    assert test_publisher["environment"]["name"] == "testpypi"
    assert (
        publisher_step(test_publisher)["with"]["repository-url"]
        == "https://test.pypi.org/legacy/"
    )
    test_install = test["jobs"]["verify-testpypi-install"]
    assert test_install["needs"] == "publish-testpypi"
    assert test_install["runs-on"] == "ubuntu-latest"
    assert test_install["permissions"] == {"contents": "read"}
    step_names = [step["name"] for step in test_install["steps"]]
    assert step_names.index("Resolve release version") < step_names.index(
        "Download and verify the TestPyPI wheel"
    )
    assert step_names.index("Download and verify the TestPyPI wheel") < (
        step_names.index("Install the registry wheel")
    )
    install_steps = {step["name"]: step for step in test_install["steps"]}
    assert install_steps["Resolve release version"]["id"] == "version"
    download = install_steps["Download and verify the TestPyPI wheel"]["run"]
    assert "--index-url https://test.pypi.org/simple/" in download
    assert "--no-deps" in download
    assert "hashlib.sha256" in download
    exercise = install_steps["Exercise the installed CLI"]["run"]
    assert "doctor --require core --require wiki" in exercise
    assert "scripts/smoke_release_install.py" in exercise

    assert set(verification["on"]) == {"workflow_call"}
    assert verification["on"]["workflow_call"]["inputs"]["full"] == {
        "description": "Run the cross-version and installed-service matrix.",
        "type": "boolean",
        "default": "true",
    }
    verification_steps = {
        step["name"]: step for step in verification["jobs"]["build"]["steps"]
    }
    assert (
        "--connect-timeout 10 --max-time 90"
        in verification_steps["Validate MCP Registry metadata"]["run"]
    )
    for job_name in (
        "install-smoke",
        "upgrade-smoke",
        "service-smoke",
        "agent-smoke",
        "graph-smoke",
    ):
        assert verification["jobs"][job_name]["if"] == "inputs.full"
    assert not any(name.startswith("publish-") for name in verification["jobs"])
