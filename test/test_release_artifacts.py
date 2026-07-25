# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.verify_release_artifacts import (
    ReleaseValidationError,
    expected_tag,
    project_identity,
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
