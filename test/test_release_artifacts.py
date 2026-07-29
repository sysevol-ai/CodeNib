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
    assert expected_tag(version) == "v0.1.0"
    validate_tag("v0.1.0", version)


def test_release_tag_must_match_project_version() -> None:
    with pytest.raises(ReleaseValidationError, match="does not match"):
        validate_tag("v0.2.0", "0.1.0")


def test_packaged_readme_requires_the_arxiv_citation() -> None:
    citation = """
## Citation
https://arxiv.org/abs/2607.25431
@misc{yu2026codenibmultiviewdataserving,
"""
    validate_readme_citation(citation)

    with pytest.raises(ReleaseValidationError, match="citation markers"):
        validate_readme_citation("# CodeNib\n")


def test_registry_publishers_use_separate_workflows() -> None:
    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"

    def load(name: str) -> dict[str, object]:
        with (workflows / name).open(encoding="utf-8") as handle:
            return yaml.load(handle, Loader=yaml.BaseLoader)

    production = load("release.yml")
    test = load("release-test.yml")
    verification = load("release-verify.yml")

    verification_job = {
        "name": "Verify release artifacts",
        "uses": "./.github/workflows/release-verify.yml",
        "permissions": {"contents": "read"},
    }
    assert production["jobs"]["verify"] == verification_job
    assert test["jobs"]["verify"] == verification_job

    assert set(production["jobs"]) == {
        "verify",
        "publish-pypi",
        "github-release",
    }
    assert production["jobs"]["publish-pypi"]["environment"]["name"] == "pypi"

    assert set(test["jobs"]) == {"verify", "publish-testpypi"}
    test_publisher = test["jobs"]["publish-testpypi"]
    assert test_publisher["environment"]["name"] == "testpypi"
    assert (
        test_publisher["steps"][-1]["with"]["repository-url"]
        == "https://test.pypi.org/legacy/"
    )

    assert set(verification["on"]) == {"workflow_call"}
    assert not any(name.startswith("publish-") for name in verification["jobs"])
