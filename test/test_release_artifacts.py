# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.verify_release_artifacts import (
    ReleaseValidationError,
    expected_tag,
    project_identity,
    validate_readme_citation,
    validate_tag,
)


def test_project_identity_and_tag_match_release_metadata() -> None:
    root = Path(__file__).resolve().parents[1]
    name, version = project_identity(root / "pyproject.toml")

    assert name == "codenib"
    assert expected_tag(version) == "v0.2.0a1"
    validate_tag("v0.2.0a1", version)


def test_release_tag_must_match_project_version() -> None:
    with pytest.raises(ReleaseValidationError, match="does not match"):
        validate_tag("v0.1.0", "0.2.0a1")


def test_packaged_readme_requires_the_arxiv_citation() -> None:
    citation = """
## Citation
https://arxiv.org/abs/2607.25431
@misc{yu2026codenibmultiviewdataserving,
"""
    validate_readme_citation(citation)

    with pytest.raises(ReleaseValidationError, match="citation markers"):
        validate_readme_citation("# CodeNib\n")


def test_alpha_release_notes_use_test_registry_and_pages_permissions() -> None:
    root = Path(__file__).resolve().parents[1]
    notes = (root / "docs" / "releases" / "0.2.0.md").read_text(encoding="utf-8")

    assert notes.count("--index-url https://test.pypi.org/simple/") == 2
    assert notes.count("--no-deps --only-binary=:all:") == 2
    assert "--extra-index-url" not in notes
    for permission in ("contents: read", "pages: write", "id-token: write"):
        assert permission in notes


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
        "publish-pypi",
        "github-release",
    }
    production_publisher = production["jobs"]["publish-pypi"]
    assert production_publisher["environment"]["name"] == "pypi"
    assert production_publisher["permissions"] == {"contents": "read"}
    assert publisher_step(production_publisher)["with"]["password"] == (
        "${{ secrets.PYPI_API_TOKEN }}"
    )

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
    for job_name in (
        "install-smoke",
        "service-smoke",
        "agent-smoke",
        "graph-smoke",
    ):
        assert verification["jobs"][job_name]["if"] == "inputs.full"
    assert not any(name.startswith("publish-") for name in verification["jobs"])
