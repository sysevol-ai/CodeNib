# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit coverage for registry-driven core decoder language metadata."""

from __future__ import annotations

import pytest

from codenib.languages import core_decoder_languages
from codenib.scip_interface import scip_decode_core


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


def test_cpp_core_decoder_registry_matches_python_registry():
    if scip_decode_core._cpp is None:
        pytest.skip("codenib_core pybind module not built")

    assert tuple(
        scip_decode_core._cpp.canonical_scip_decoder_languages()
    ) == core_decoder_languages(include_aliases=False)
    assert tuple(
        scip_decode_core._cpp.accepted_scip_decoder_languages()
    ) == core_decoder_languages(include_aliases=True)


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
