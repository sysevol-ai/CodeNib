# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit coverage for registry-driven core decoder language metadata."""

from __future__ import annotations

import pytest

from codeminer.languages import core_decoder_languages
from codeminer.scip_interface import scip_decode_core


def test_core_decoder_supported_languages_come_from_registry():
    assert scip_decode_core.SUPPORTED_LANGUAGES == core_decoder_languages(
        include_aliases=False
    )
    assert scip_decode_core.ACCEPTED_LANGUAGES == core_decoder_languages(
        include_aliases=True
    )
    assert scip_decode_core.ACCEPTED_LANGUAGES == (
        "python",
        "go",
        "rust",
        "ruby",
        "rb",
        "typescript",
        "ts",
        "js",
    )


def test_core_decoder_rejects_non_registry_language_before_availability_check(
    monkeypatch,
):
    def fail_available():
        raise AssertionError("_ensure_available should not run for invalid languages")

    monkeypatch.setattr(scip_decode_core, "_ensure_available", fail_available)

    with pytest.raises(ValueError, match="Unknown language 'java'"):
        scip_decode_core.SCIPDecoderCore(
            index_file_path="index.decoded",
            project_root=None,
            language="java",
        )
