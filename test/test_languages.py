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
    scip_candidate_indexer_path,
    scip_candidate_indexer_paths,
    scip_cold_start_command_for_language,
    scip_cold_start_option,
    scip_cold_start_options,
    supported_agent_languages,
)


def test_c_family_surface_normalization_is_explicit():
    assert normalize_chunker_language("c") is None
    assert normalize_chunker_language("c++") == "cpp"
    assert normalize_chunker_language("c#") == "csharp"
    assert normalize_chunker_language("cs") == "csharp"
    assert normalize_graph_language("c") == "cpp"
    assert normalize_graph_language("c++") == "cpp"
    assert normalize_graph_language("c#") == "csharp"
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
    assert ext_map[".swift"] == "swift"
    assert ext_map[".scala"] == "scala"
    assert ext_map[".sc"] == "scala"
    assert ext_map[".lua"] == "lua"
    assert ext_map[".luau"] == "lua"
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
        "swift",
        "scala",
        "lua",
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
    assert rows["python"].scip_cold_start == "active"
    assert rows["python"].core_decoder is True
    assert rows["python"].core_parity == "covered"

    assert rows["cpp"].graph_backend == "clangd"
    assert rows["cpp"].scip_cold_start == "none"
    assert rows["cpp"].core_decoder is False
    assert rows["cpp"].core_parity == "n/a-no-core-decoder"

    assert rows["javascript"].graph_backend == "scip"
    assert rows["javascript"].scip_cold_start == "active"
    assert rows["javascript"].core_decoder is True
    assert rows["javascript"].core_parity == "covered"

    assert rows["java"].graph_backend == "scip"
    assert rows["java"].scip_cold_start == "active"
    assert rows["java"].incremental_backend is None
    assert rows["java"].lsp is True
    assert rows["java"].core_decoder is False
    assert rows["java"].core_parity == "n/a-no-core-decoder"

    assert rows["csharp"].graph_backend == "scip"
    assert rows["csharp"].scip_cold_start == "active"
    assert rows["csharp"].incremental_backend is None
    assert rows["csharp"].lsp is True
    assert rows["csharp"].core_decoder is False
    assert rows["csharp"].core_parity == "n/a-no-core-decoder"

    assert rows["php"].graph_backend == "scip"
    assert rows["php"].scip_cold_start == "active"
    assert rows["php"].incremental_backend is None
    assert rows["php"].lsp is True
    assert rows["php"].core_decoder is False
    assert rows["php"].core_parity == "n/a-no-core-decoder"

    assert rows["scala"].graph_backend == "scip"
    assert rows["scala"].scip_cold_start == "active"
    assert rows["scala"].incremental_backend is None
    assert rows["scala"].lsp is False
    assert rows["scala"].core_decoder is False
    assert rows["scala"].core_parity == "n/a-no-core-decoder"

    assert rows["ruby"].chunker is True
    assert rows["ruby"].ground_truth is True
    assert rows["ruby"].agent is True
    assert rows["ruby"].graph_backend == "scip"
    assert rows["ruby"].scip_cold_start == "active"
    assert rows["ruby"].incremental_backend is None
    assert rows["ruby"].lsp is True
    assert rows["ruby"].core_decoder is False
    assert rows["ruby"].core_parity == "n/a-no-core-decoder"

    assert rows["kotlin"].chunker is True
    assert rows["kotlin"].ground_truth is True
    assert rows["kotlin"].agent is True
    assert rows["kotlin"].graph_backend == "lsp"
    assert rows["kotlin"].scip_cold_start == "candidate"
    assert rows["kotlin"].incremental_backend is None
    assert rows["kotlin"].core_decoder is False
    assert rows["kotlin"].core_parity == "n/a-no-core-decoder"

    for language in ("swift", "lua"):
        assert rows[language].chunker is True
        assert rows[language].ground_truth is True
        assert rows[language].agent is True
        assert rows[language].graph_backend is None
        assert rows[language].scip_cold_start == "none"
        assert rows[language].incremental_backend is None
        assert rows[language].core_decoder is False
        assert rows[language].core_parity == "n/a-tree-sitter-only"


def test_scip_cold_start_options_track_active_and_candidate_paths(monkeypatch):
    options = scip_cold_start_options()

    assert options["python"].status == "active"
    assert options["python"].tool == "scip-python"
    assert options["typescript"].status == "active"
    assert options["ts"].tool == "scip-typescript"

    assert options["java"].status == "active"
    assert options["java"].tool == "scip-java"
    assert options["kotlin"].tool == "scip-java"
    assert options["scala"].status == "active"
    assert options["scala"].tool == "scip-java"
    assert options["csharp"].tool == "scip-dotnet"
    assert options["c#"].tool == "scip-dotnet"
    assert options["ruby"].status == "active"
    assert options["ruby"].tool == "scip-ruby"
    assert options["php"].status == "active"
    assert options["php"].tool == "scip-php"

    assert scip_cold_start_option("kt").command_env == "CODEMINER_KOTLIN_SCIP_CMD"
    assert scip_cold_start_command_for_language("java") == ["scip-java", "index"]
    assert scip_cold_start_command_for_language("c#") == ["scip-dotnet"]
    monkeypatch.setenv("CODEMINER_CSHARP_SCIP_CMD", "dotnet tool run scip-dotnet")
    assert scip_cold_start_command_for_language("csharp") == [
        "dotnet",
        "tool",
        "run",
        "scip-dotnet",
    ]
    assert scip_cold_start_option("swift") is None
    assert scip_cold_start_command_for_language("swift") is None
    assert scip_cold_start_option("lua") is None


def test_scip_candidate_indexer_paths_are_opt_in_routes():
    paths = scip_candidate_indexer_paths()

    assert paths["java"].endswith("scip_indexer_java:SCIPJavaIndexer")
    assert paths["kotlin"].endswith("scip_indexer_java:SCIPKotlinIndexer")
    assert paths["kt"] == paths["kotlin"]
    assert paths["scala"].endswith("scip_indexer_java:SCIPScalaIndexer")
    assert paths["csharp"].endswith("scip_indexer_csharp:SCIPCSharpIndexer")
    assert paths["c#"] == paths["csharp"]
    assert paths["ruby"].endswith("scip_indexer_ruby:SCIPRubyIndexer")
    assert paths["rb"] == paths["ruby"]
    assert paths["php"].endswith("scip_indexer_php:SCIPPHPIndexer")

    assert scip_candidate_indexer_path("java") == paths["java"]
    assert scip_candidate_indexer_path("scala") == paths["scala"]
    assert scip_candidate_indexer_path("python") is None


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
        "swift",
        "scala",
        "lua",
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
    assert paths["swift"].endswith("swift_chunker:SwiftCodeChunker")
    assert paths["scala"].endswith("scala_chunker:ScalaCodeChunker")
    assert paths["lua"].endswith("lua_chunker:LuaCodeChunker")
    assert paths["luau"] == paths["lua"]
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
    assert chunker_class_path("swift") == paths["swift"]
    assert chunker_class_path("scala") == paths["scala"]
    assert chunker_class_path("luau") == paths["lua"]

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
    assert incremental_patcher_path("swift") is None
    assert incremental_patcher_path("scala") is None
    assert incremental_patcher_path("lua") is None


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

    assert indexers["java"].endswith("scip_indexer_java:SCIPJavaIndexer")
    assert decoders["java"].endswith("scip_decode_java:SCIPJavaGraphDecoder")
    assert graph_indexer_path("java") == indexers["java"]
    assert graph_decoder_path("java") == decoders["java"]

    assert graph_cold_start_backend("cpp") == "clangd"
    assert graph_cold_start_backend("python") == "scip"
    assert graph_cold_start_backend("java") == "scip"
    assert graph_indexer_path("csharp").endswith(
        "scip_indexer_csharp:SCIPCSharpIndexer"
    )
    assert graph_decoder_path("csharp").endswith(
        "scip_decode_csharp:SCIPCSharpGraphDecoder"
    )
    assert graph_cold_start_backend("csharp") == "scip"
    assert graph_indexer_path("php").endswith("scip_indexer_php:PHPHybridIndexer")
    assert graph_decoder_path("php").endswith("scip_decode_php:SCIPPHPGraphDecoder")
    assert graph_cold_start_backend("php") == "scip"
    assert graph_indexer_path("scala").endswith("scip_indexer_java:SCIPScalaIndexer")
    assert graph_decoder_path("scala").endswith("scip_decode_java:SCIPJavaGraphDecoder")
    assert graph_cold_start_backend("scala") == "scip"
    assert graph_indexer_path("ruby").endswith("scip_indexer_ruby:RubyHybridIndexer")
    assert graph_decoder_path("ruby").endswith("scip_decode_ruby:SCIPRubyGraphDecoder")
    assert graph_cold_start_backend("ruby") == "scip"

    for language in ("kotlin",):
        assert graph_indexer_path(language).endswith("lsp_indexer:GenericLSPIndexer")
        assert graph_decoder_path(language).endswith(
            "lsp_graph_decode:GenericLSPGraphDecoder"
        )
        assert graph_cold_start_backend(language) == "lsp"

    for language in ("swift", "lua"):
        assert graph_indexer_path(language) is None
        assert graph_decoder_path(language) is None
        assert graph_cold_start_backend(language) is None


def test_lsp_metadata_follows_graph_language_aliases(monkeypatch):
    assert lsp_language_id_for_language("python") == "python"
    assert lsp_command_for_language("python") == ["basedpyright-langserver", "--stdio"]
    assert lsp_language_id_for_language("c#") == "csharp"
    assert lsp_command_for_language("c#") == ["csharp-ls"]
    assert lsp_language_id_for_language("rb") == "ruby"
    assert lsp_command_for_language("rb") == ["ruby-lsp"]
    assert lsp_language_id_for_language("php") == "php"
    assert lsp_command_for_language("php") == ["intelephense", "--stdio"]
    assert lsp_language_id_for_language("kt") == "kotlin"
    assert lsp_command_for_language("kt") == ["kotlin-language-server", "--stdio"]

    monkeypatch.setenv("CODEMINER_PYTHON_LSP_CMD", "ty server")
    assert lsp_command_for_language("py") == ["ty", "server"]

    monkeypatch.setenv("CODEMINER_RUBY_LSP_CMD", "bundle exec ruby-lsp")
    assert lsp_command_for_language("ruby") == ["bundle", "exec", "ruby-lsp"]

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
