# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for registry-driven GraphPatcher routing."""

from __future__ import annotations

import pytest

from codeminer.graph.incremental.graph_patcher import GraphPatcher


@pytest.mark.parametrize(
    ("language", "module_suffix", "class_name"),
    [
        ("python", "patcher_python", "PatcherPython"),
        ("py", "patcher_python", "PatcherPython"),
        ("go", "patcher_go", "PatcherGo"),
        ("golang", "patcher_go", "PatcherGo"),
        ("rust", "patcher_rust", "PatcherRust"),
        ("rs", "patcher_rust", "PatcherRust"),
        ("cpp", "patcher_cpp", "PatcherCpp"),
        ("c", "patcher_cpp", "PatcherCpp"),
        ("typescript", "patcher_ts", "PatcherTS"),
        ("javascript", "patcher_ts", "PatcherTS"),
        ("js", "patcher_ts", "PatcherTS"),
    ],
)
def test_graph_patcher_routes_through_language_registry(
    tmp_path, language, module_suffix, class_name
):
    patcher = GraphPatcher(str(tmp_path), None, language)

    assert patcher._impl.__class__.__name__ == class_name
    assert patcher._impl.__class__.__module__.endswith(module_suffix)


def test_graph_patcher_rejects_unregistered_language(tmp_path):
    with pytest.raises(ValueError, match="Unsupported language: java"):
        GraphPatcher(str(tmp_path), None, "java")
