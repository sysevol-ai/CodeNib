# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the central language metadata registry."""

from codeminer.languages import (
    chunker_class_path,
    chunker_class_paths,
    chunker_languages,
    core_decoder_languages,
    extension_to_language_map,
    get_chunker_spec,
    graph_cold_start_backend,
    graph_decoder_path,
    graph_decoder_paths,
    graph_extensions_by_language,
    graph_indexer_path,
    graph_indexer_paths,
    incremental_patcher_path,
    incremental_patcher_paths,
    language_capability_rows,
    lsp_command_for_language,
    lsp_language_id_for_language,
    normalize_agent_language,
    normalize_chunker_language,
    normalize_graph_language,
    supported_agent_languages,
)


def test_c_family_surface_normalization_is_explicit():
    assert normalize_chunker_language("c") is None
    assert normalize_chunker_language("c++") == "cpp"
    assert normalize_chunker_language("c#") == "csharp"
    assert normalize_chunker_language("cs") == "csharp"
    assert normalize_graph_language("c") == "cpp"
    assert normalize_graph_language("c++") == "cpp"
    assert normalize_graph_language("c#") is None
    assert normalize_agent_language("c") == "c"
    assert normalize_agent_language("c++") == "cpp"
    assert normalize_agent_language("c#") == "csharp"
    assert normalize_agent_language("cs") == "csharp"


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
    assert ext_map[".cs"] == "csharp"
    assert ext_map[".java"] == "java"
    assert ext_map[".rb"] == "ruby"
    assert ext_map[".php"] == "php"
    assert ext_map[".kt"] == "kotlin"
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
        "csharp",
        "java",
        "ruby",
        "php",
        "kotlin",
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


def test_language_capability_rows_track_parity_applicability():
    rows = {row.key: row for row in language_capability_rows()}

    assert rows["python"].graph_backend == "scip"
    assert rows["python"].core_decoder is True
    assert rows["python"].core_parity == "covered"

    assert rows["cpp"].graph_backend == "clangd"
    assert rows["cpp"].core_decoder is False
    assert rows["cpp"].core_parity == "n/a-no-core-decoder"

    assert rows["javascript"].graph_backend == "scip"
    assert rows["javascript"].core_decoder is True
    assert rows["javascript"].core_parity == "covered"

    assert rows["java"].graph_backend == "lsp"
    assert rows["java"].incremental_backend is None
    assert rows["java"].lsp is True
    assert rows["java"].core_decoder is False
    assert rows["java"].core_parity == "n/a-no-core-decoder"

    for language in ("csharp", "ruby", "php", "kotlin"):
        assert rows[language].chunker is True
        assert rows[language].ground_truth is True
        assert rows[language].agent is True
        assert rows[language].graph_backend is None
        assert rows[language].incremental_backend is None
        assert rows[language].core_decoder is False
        assert rows[language].core_parity == "n/a-tree-sitter-only"


def test_chunker_languages_are_current_supported_repo_chunkers():
    assert chunker_languages() == (
        "python",
        "go",
        "rust",
        "cpp",
        "csharp",
        "java",
        "ruby",
        "php",
        "kotlin",
        "javascript",
        "typescript",
    )


def test_chunker_class_paths_follow_chunker_language_aliases():
    paths = chunker_class_paths()

    assert paths["python"].endswith("python_chunker:PythonCodeChunker")
    assert paths["go"].endswith("go_chunker:GoCodeChunker")
    assert paths["golang"] == paths["go"]
    assert paths["rust"].endswith("rust_chunker:RustCodeChunker")
    assert paths["cpp"].endswith("cpp_chunker:CppCodeChunker")
    assert paths["c++"] == paths["cpp"]
    assert paths["csharp"].endswith("csharp_chunker:CSharpCodeChunker")
    assert paths["c#"] == paths["csharp"]
    assert paths["cs"] == paths["csharp"]
    assert paths["java"].endswith("java_chunker:JavaCodeChunker")
    assert paths["ruby"].endswith("ruby_chunker:RubyCodeChunker")
    assert paths["rb"] == paths["ruby"]
    assert paths["php"].endswith("php_chunker:PhpCodeChunker")
    assert paths["kotlin"].endswith("kotlin_chunker:KotlinCodeChunker")
    assert paths["kt"] == paths["kotlin"]
    assert paths["javascript"].endswith("js_chunker:JsTsCodeChunker")
    assert paths["js"] == paths["javascript"]
    assert paths["typescript"].endswith("js_chunker:JsTsCodeChunker")
    assert paths["ts"] == paths["typescript"]

    assert chunker_class_path("py") is None
    assert chunker_class_path("js") == paths["javascript"]
    assert chunker_class_path("c#") == paths["csharp"]
    assert chunker_class_path("java") == paths["java"]
    assert chunker_class_path("rb") == paths["ruby"]
    assert chunker_class_path("php") == paths["php"]
    assert chunker_class_path("kt") == paths["kotlin"]

    js_spec = get_chunker_spec("javascript")
    ts_spec = get_chunker_spec("typescript")
    assert js_spec is not None and js_spec.chunker_pass_language is True
    assert ts_spec is not None and ts_spec.chunker_pass_language is True


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
    assert incremental_patcher_path("csharp") is None
    assert incremental_patcher_path("java") is None
    assert incremental_patcher_path("ruby") is None
    assert incremental_patcher_path("php") is None
    assert incremental_patcher_path("kotlin") is None


def test_graph_indexer_and_decoder_paths_follow_graph_language_aliases():
    indexers = graph_indexer_paths()
    decoders = graph_decoder_paths()

    assert indexers["python"].endswith("scip_indexer_python:SCIPPythonIndexer")
    assert decoders["python"].endswith("scip_decode_python:SCIPPythonGraphDecoder")
    assert indexers["py"] == indexers["python"]
    assert decoders["py"] == decoders["python"]

    assert indexers["go"].endswith("scip_indexer_go:SCIPGoIndexer")
    assert decoders["go"].endswith("scip_decode_go:SCIPGoGraphDecoder")
    assert indexers["golang"] == indexers["go"]
    assert decoders["golang"] == decoders["go"]

    assert indexers["cpp"].endswith("clangd_indexer:ClangdIndexer")
    assert decoders["cpp"].endswith("clangd_decode:ClangdGraphDecoder")
    assert indexers["c"] == indexers["cpp"]
    assert decoders["c"] == decoders["cpp"]

    assert indexers["ts"].endswith("scip_indexer_ts:SCIPTypeScriptIndexer")
    assert decoders["ts"].endswith("scip_decode_ts:SCIPTypeScriptGraphDecoder")
    assert graph_indexer_path("javascript") == indexers["ts"]
    assert graph_decoder_path("typescript") == decoders["ts"]

    assert indexers["java"].endswith("lsp_indexer:GenericLSPIndexer")
    assert decoders["java"].endswith("lsp_graph_decode:GenericLSPGraphDecoder")
    assert graph_indexer_path("java") == indexers["java"]
    assert graph_decoder_path("java") == decoders["java"]

    assert graph_cold_start_backend("cpp") == "clangd"
    assert graph_cold_start_backend("python") == "scip"
    assert graph_cold_start_backend("java") == "lsp"
    assert graph_indexer_path("csharp") is None
    assert graph_decoder_path("csharp") is None
    assert graph_indexer_path("ruby") is None
    assert graph_decoder_path("ruby") is None
    assert graph_indexer_path("php") is None
    assert graph_decoder_path("php") is None
    assert graph_indexer_path("kotlin") is None
    assert graph_decoder_path("kotlin") is None


def test_lsp_metadata_follows_graph_language_aliases(monkeypatch):
    assert lsp_language_id_for_language("python") == "python"
    assert lsp_command_for_language("python") == ["basedpyright-langserver", "--stdio"]

    monkeypatch.setenv("CODEMINER_PYTHON_LSP_CMD", "ty server")
    assert lsp_command_for_language("py") == ["ty", "server"]

    assert lsp_language_id_for_language("go") == "go"
    assert lsp_command_for_language("golang") == ["gopls", "serve"]

    assert lsp_language_id_for_language("cpp") == "cpp"
    assert lsp_command_for_language("c++") == ["clangd"]

    assert lsp_language_id_for_language("javascript") == "typescript"
    assert lsp_command_for_language("ts") == ["typescript-language-server", "--stdio"]

    assert lsp_language_id_for_language("java") == "java"
    assert lsp_command_for_language("java") == ["jdtls"]
    monkeypatch.setenv("CODEMINER_JAVA_LSP_CMD", "jdtls --stdio")
    assert lsp_command_for_language("java") == ["jdtls", "--stdio"]

    assert lsp_language_id_for_language("csharp") is None
    assert lsp_command_for_language("csharp") is None
    assert lsp_language_id_for_language("ruby") is None
    assert lsp_command_for_language("ruby") is None
    assert lsp_language_id_for_language("php") is None
    assert lsp_command_for_language("php") is None
    assert lsp_language_id_for_language("kotlin") is None
    assert lsp_command_for_language("kotlin") is None
