# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from scripts.smoke_release_upgrade import _assert_builder_contract


def test_upgrade_smoke_accepts_current_builder_identity_with_build_metadata():
    expected = {
        "builder_schema": 8,
        "repository_filter_policy": 3,
        "languages": ["python"],
    }
    actual = {
        **expected,
        "artifact_file_fingerprints": {"documents.json": {"sha256": "abc"}},
        "chunk_count": 1,
    }

    _assert_builder_contract(actual, expected)


def test_upgrade_smoke_rejects_stale_builder_contract():
    with pytest.raises(RuntimeError, match="unexpected BM25 builder contract"):
        _assert_builder_contract(
            {"builder_schema": 7, "repository_filter_policy": 3},
            {"builder_schema": 8, "repository_filter_policy": 3},
        )
