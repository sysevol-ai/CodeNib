# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the central language metadata registry."""

from codeminer.languages import (
    chunker_languages,
    core_decoder_languages,
    extension_to_language_map,
    graph_extensions_by_language,
    incremental_patcher_path,
    incremental_patcher_paths,
    normalize_agent_language,
    normalize_chunker_language,
    normalize_graph_language,
    supported_agent_languages,
)


def test_c_family_surface_normalization_is_explicit():
    assert normalize_chunker_language("c") is None
    assert normalize_chunker_language("c++") == "cpp"
    assert normalize_graph_language("c") == "cpp"
    assert normalize_graph_language("c++") == "cpp"
    assert normalize_agent_language("c") == "c"
    assert normalize_agent_language("c++") == "cpp"


def test_javascript_typescript_share_graph_backend_but_keep_agent_keys():
    assert normalize_chunker_language("js") == "javascript"
    assert normalize_chunker_language("ts") == "typescript"
    assert normalize_graph_language("javascript") == "ts"
    assert normalize_graph_language("typescript") == "ts"
    assert normalize_agent_language("jsx") == "javascript"
    assert normalize_agent_language("tsx") == "typescript"

    graph_extensions = graph_extensions_by_language()
    assert graph_extensions["ts"] == {".ts", ".tsx", ".js", ".jsx"}
    assert graph_extensions["javascript"] == graph_extensions["ts"]


def test_gt_extensions_preserve_current_supported_set():
    ext_map = extension_to_language_map("gt")
    assert ext_map[".py"] == "python"
    assert ext_map[".c"] == "cpp"
    assert ext_map[".tsx"] == "typescript"
    assert ext_map[".jsx"] == "javascript"

    assert ".mjs" not in ext_map
    assert ".mts" not in ext_map
    assert ".cts" not in ext_map


def test_agent_supported_languages_match_existing_scenarios():
    assert supported_agent_languages() == {
        "python",
        "go",
        "rust",
        "cpp",
        "c",
        "typescript",
        "javascript",
    }


def test_core_decoder_languages_only_include_supported_core_aliases():
    assert core_decoder_languages() == (
        "python",
        "go",
        "rust",
        "typescript",
        "ts",
        "js",
    )


def test_chunker_languages_are_current_supported_repo_chunkers():
    assert chunker_languages() == (
        "python",
        "go",
        "rust",
        "cpp",
        "javascript",
        "typescript",
    )


def test_incremental_patcher_paths_follow_graph_language_aliases():
    paths = incremental_patcher_paths()

    assert paths["python"].endswith("patcher_python:PatcherPython")
    assert paths["py"] == paths["python"]
    assert paths["go"].endswith("patcher_go:PatcherGo")
    assert paths["golang"] == paths["go"]
    assert paths["rust"].endswith("patcher_rust:PatcherRust")
    assert paths["rs"] == paths["rust"]
    assert paths["cpp"].endswith("patcher_cpp:PatcherCpp")
    assert paths["c"] == paths["cpp"]
    assert paths["ts"].endswith("patcher_ts:PatcherTS")
    assert paths["javascript"] == paths["ts"]
    assert paths["typescript"] == paths["ts"]

    assert incremental_patcher_path("js") == paths["ts"]
    assert incremental_patcher_path("jsx") is None
    assert incremental_patcher_path("java") is None
