# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Central language metadata registry.

This module is intentionally declarative.  The existing language support
surfaces do not all use the same canonical key yet: for example agent compile
keeps ``c`` distinct from ``cpp``, while graph indexing routes ``c`` through
the C/C++ backend.  ``LanguageSpec`` records those per-surface choices in one
place so new language work can add metadata without editing every router by
hand.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from importlib import import_module
from typing import Dict, FrozenSet, Iterable, Literal, Optional, Tuple

ExtensionKind = Literal["chunker", "gt", "graph"]
ScipColdStartStatus = Literal["active", "candidate", "deferred"]


@dataclass(frozen=True, slots=True)
class ScipColdStartOption:
    """SCIP cold-start backend status for one language.

    ``active`` means the language's current cold-start graph backend is SCIP.
    ``candidate`` means a known SCIP indexer exists, but CodeMiner has not yet
    promoted it to the primary backend because smoke, backend-alignment, decoder,
    and parity gates are still open.
    """

    tool: str
    status: ScipColdStartStatus
    command: Tuple[str, ...] = ()
    command_env: Optional[str] = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    """Declarative metadata for one CodeMiner language family."""

    key: str
    display_name: str
    aliases: Tuple[str, ...] = ()
    chunker_language: Optional[str] = None
    chunker_aliases: Tuple[str, ...] = ()
    chunker_class: Optional[str] = None
    chunker_pass_language: bool = False
    chunk_extensions: Tuple[str, ...] = ()
    gt_language: Optional[str] = None
    gt_extensions: Tuple[str, ...] = ()
    graph_language: Optional[str] = None
    graph_aliases: Tuple[str, ...] = ()
    graph_extensions: Tuple[str, ...] = ()
    agent_languages: Tuple[str, ...] = ()
    agent_aliases: Tuple[Tuple[str, str], ...] = ()
    cold_start_backend: Optional[str] = None
    graph_indexer: Optional[str] = None
    graph_decoder: Optional[str] = None
    scip_cold_start: Optional[ScipColdStartOption] = None
    scip_candidate_indexer: Optional[str] = None
    incremental_backend: Optional[str] = None
    incremental_patcher: Optional[str] = None
    lsp_language_id: Optional[str] = None
    lsp_command: Tuple[str, ...] = ()
    lsp_command_env: Optional[str] = None
    lsp_command_factory: Optional[str] = None
    core_decoder: bool = False
    core_decoder_aliases: Tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LanguageCapability:
    """Derived support matrix row for one registered language."""

    key: str
    display_name: str
    chunker: bool
    ground_truth: bool
    agent: bool
    graph_backend: Optional[str]
    scip_cold_start: str
    incremental_backend: Optional[str]
    lsp: bool
    core_decoder: bool
    core_parity: str


LANGUAGE_SPECS: Tuple[LanguageSpec, ...] = (
    LanguageSpec(
        key="python",
        display_name="Python",
        aliases=("py", "python3"),
        chunker_language="python",
        chunker_aliases=("python",),
        chunker_class="codeminer.code_chunking.python_chunker:PythonCodeChunker",
        chunk_extensions=(".py", ".pyx", ".pyi"),
        gt_language="python",
        gt_extensions=(".py",),
        graph_language="python",
        graph_aliases=("python", "py"),
        graph_extensions=(".py",),
        agent_languages=("python",),
        agent_aliases=(("python", "python"), ("py", "python"), ("python3", "python")),
        cold_start_backend="scip",
        graph_indexer="codeminer.scip_interface.scip_indexer_python:SCIPPythonIndexer",
        graph_decoder="codeminer.scip_interface.scip_decode_python:SCIPPythonGraphDecoder",
        scip_cold_start=ScipColdStartOption(
            tool="scip-python",
            status="active",
            command=("scip-python", "index"),
            note="Primary cold-start backend with serial/core parity coverage.",
        ),
        incremental_backend="lsp",
        incremental_patcher="codeminer.graph.incremental.patcher_python:PatcherPython",
        lsp_language_id="python",
        lsp_command=("basedpyright-langserver", "--stdio"),
        lsp_command_env="CODEMINER_PYTHON_LSP_CMD",
        core_decoder=True,
    ),
    LanguageSpec(
        key="go",
        display_name="Go",
        aliases=("golang",),
        chunker_language="go",
        chunker_aliases=("go", "golang"),
        chunker_class="codeminer.code_chunking.go_chunker:GoCodeChunker",
        chunk_extensions=(".go",),
        gt_language="go",
        gt_extensions=(".go",),
        graph_language="go",
        graph_aliases=("go", "golang"),
        graph_extensions=(".go",),
        agent_languages=("go",),
        agent_aliases=(("go", "go"), ("golang", "go")),
        cold_start_backend="scip",
        graph_indexer="codeminer.scip_interface.scip_indexer_go:SCIPGoIndexer",
        graph_decoder="codeminer.scip_interface.scip_decode_go:SCIPGoGraphDecoder",
        scip_cold_start=ScipColdStartOption(
            tool="scip-go",
            status="active",
            command=("scip-go",),
            note="Primary cold-start backend with serial/core parity coverage.",
        ),
        incremental_backend="lsp",
        incremental_patcher="codeminer.graph.incremental.patcher_go:PatcherGo",
        lsp_language_id="go",
        lsp_command=("gopls", "serve"),
        core_decoder=True,
    ),
    LanguageSpec(
        key="rust",
        display_name="Rust",
        aliases=("rs",),
        chunker_language="rust",
        chunker_aliases=("rust",),
        chunker_class="codeminer.code_chunking.rust_chunker:RustCodeChunker",
        chunk_extensions=(".rs",),
        gt_language="rust",
        gt_extensions=(".rs",),
        graph_language="rust",
        graph_aliases=("rust", "rs"),
        graph_extensions=(".rs",),
        agent_languages=("rust",),
        agent_aliases=(("rust", "rust"), ("rs", "rust")),
        cold_start_backend="scip",
        graph_indexer="codeminer.scip_interface.scip_indexer_rust:SCIPRustIndexer",
        graph_decoder="codeminer.scip_interface.scip_decode_rust:SCIPRustGraphDecoder",
        scip_cold_start=ScipColdStartOption(
            tool="rust-analyzer scip",
            status="active",
            command=("rust-analyzer", "scip"),
            note="Primary cold-start backend with serial/core parity coverage.",
        ),
        incremental_backend="lsp",
        incremental_patcher="codeminer.graph.incremental.patcher_rust:PatcherRust",
        lsp_language_id="rust",
        lsp_command_factory="codeminer.scip_interface.rust_analyzer:rust_analyzer_command",
        core_decoder=True,
    ),
    LanguageSpec(
        key="cpp",
        display_name="C/C++",
        aliases=("c", "c++", "cxx"),
        chunker_language="cpp",
        chunker_aliases=("cpp", "c++", "cxx"),
        chunker_class="codeminer.code_chunking.cpp_chunker:CppCodeChunker",
        chunk_extensions=(".cpp", ".cxx", ".cc", ".c", ".hpp", ".h", ".hxx"),
        gt_language="cpp",
        gt_extensions=(".c", ".cpp", ".cc", ".cxx", ".h", ".hpp"),
        graph_language="cpp",
        graph_aliases=("cpp", "c++", "c"),
        graph_extensions=(".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx"),
        agent_languages=("cpp", "c"),
        agent_aliases=(("cpp", "cpp"), ("c++", "cpp"), ("cxx", "cpp"), ("c", "c")),
        cold_start_backend="clangd",
        graph_indexer="codeminer.ls_index.clangd_indexer:ClangdIndexer",
        graph_decoder="codeminer.ls_index.clangd_decode:ClangdGraphDecoder",
        incremental_backend="clangd",
        incremental_patcher="codeminer.graph.incremental.patcher_cpp:PatcherCpp",
        lsp_language_id="cpp",
        lsp_command=("clangd",),
    ),
    LanguageSpec(
        key="csharp",
        display_name="C#",
        aliases=("c#", "cs"),
        chunker_language="csharp",
        chunker_aliases=("csharp", "c#", "cs"),
        chunker_class="codeminer.code_chunking.csharp_chunker:CSharpCodeChunker",
        chunk_extensions=(".cs",),
        gt_language="csharp",
        gt_extensions=(".cs",),
        graph_language="csharp",
        graph_aliases=("csharp", "c#", "cs"),
        graph_extensions=(".cs",),
        agent_languages=("csharp",),
        agent_aliases=(("csharp", "csharp"), ("c#", "csharp"), ("cs", "csharp")),
        cold_start_backend="scip",
        graph_indexer="codeminer.scip_interface.scip_indexer_csharp:SCIPCSharpIndexer",
        graph_decoder="codeminer.scip_interface.scip_decode_csharp:SCIPCSharpGraphDecoder",
        scip_cold_start=ScipColdStartOption(
            tool="scip-dotnet",
            status="active",
            command=("scip-dotnet",),
            command_env="CODEMINER_CSHARP_SCIP_CMD",
            note=(
                "Primary C# cold-start backend after generated and real-repo "
                "csharp-ls alignment gates; use graph_route='lsp' for csharp-ls."
            ),
        ),
        scip_candidate_indexer="codeminer.scip_interface.scip_indexer_csharp:SCIPCSharpIndexer",
        lsp_language_id="csharp",
        lsp_command=("csharp-ls",),
        lsp_command_env="CODEMINER_CSHARP_LSP_CMD",
    ),
    LanguageSpec(
        key="java",
        display_name="Java",
        chunker_language="java",
        chunker_aliases=("java",),
        chunker_class="codeminer.code_chunking.java_chunker:JavaCodeChunker",
        chunk_extensions=(".java",),
        gt_language="java",
        gt_extensions=(".java",),
        graph_language="java",
        graph_aliases=("java",),
        graph_extensions=(".java",),
        agent_languages=("java",),
        agent_aliases=(("java", "java"),),
        cold_start_backend="scip",
        graph_indexer="codeminer.scip_interface.scip_indexer_java:SCIPJavaIndexer",
        graph_decoder="codeminer.scip_interface.scip_decode_java:SCIPJavaGraphDecoder",
        scip_cold_start=ScipColdStartOption(
            tool="scip-java",
            status="active",
            command=("scip-java", "index"),
            command_env="CODEMINER_JAVA_SCIP_CMD",
            note=(
                "Primary Java cold-start backend after generated and real-repo "
                "JDT LS alignment gates; use graph_route='lsp' for JDT LS."
            ),
        ),
        scip_candidate_indexer="codeminer.scip_interface.scip_indexer_java:SCIPJavaIndexer",
        lsp_language_id="java",
        lsp_command=("jdtls",),
        lsp_command_env="CODEMINER_JAVA_LSP_CMD",
    ),
    LanguageSpec(
        key="ruby",
        display_name="Ruby",
        aliases=("rb",),
        chunker_language="ruby",
        chunker_aliases=("ruby", "rb"),
        chunker_class="codeminer.code_chunking.ruby_chunker:RubyCodeChunker",
        chunk_extensions=(".rb",),
        gt_language="ruby",
        gt_extensions=(".rb",),
        graph_language="ruby",
        graph_aliases=("ruby", "rb"),
        graph_extensions=(".rb",),
        agent_languages=("ruby",),
        agent_aliases=(("ruby", "ruby"), ("rb", "ruby")),
        cold_start_backend="scip",
        graph_indexer="codeminer.scip_interface.scip_indexer_ruby:RubyHybridIndexer",
        graph_decoder="codeminer.scip_interface.scip_decode_ruby:SCIPRubyGraphDecoder",
        scip_cold_start=ScipColdStartOption(
            tool="scip-ruby",
            status="active",
            command=("bundle", "exec", "scip-ruby"),
            command_env="CODEMINER_RUBY_SCIP_CMD",
            note=(
                "Primary Ruby cold-start route for prepared Bundler projects; "
                "active route falls back to ruby-lsp for loose or unprepared "
                "Ruby projects."
            ),
        ),
        scip_candidate_indexer="codeminer.scip_interface.scip_indexer_ruby:SCIPRubyIndexer",
        lsp_language_id="ruby",
        lsp_command=("ruby-lsp",),
        lsp_command_env="CODEMINER_RUBY_LSP_CMD",
        core_decoder=True,
        core_decoder_aliases=("rb",),
    ),
    LanguageSpec(
        key="php",
        display_name="PHP",
        chunker_language="php",
        chunker_aliases=("php",),
        chunker_class="codeminer.code_chunking.php_chunker:PhpCodeChunker",
        chunk_extensions=(".php", ".phtml"),
        gt_language="php",
        gt_extensions=(".php", ".phtml"),
        graph_language="php",
        graph_aliases=("php",),
        graph_extensions=(".php", ".phtml"),
        agent_languages=("php",),
        agent_aliases=(("php", "php"),),
        cold_start_backend="scip",
        graph_indexer="codeminer.scip_interface.scip_indexer_php:PHPHybridIndexer",
        graph_decoder="codeminer.scip_interface.scip_decode_php:SCIPPHPGraphDecoder",
        scip_cold_start=ScipColdStartOption(
            tool="scip-php",
            status="active",
            command=("vendor/bin/scip-php",),
            command_env="CODEMINER_PHP_SCIP_CMD",
            note=(
                "Primary PHP cold-start backend for Composer projects after "
                "generated and real-repo Intelephense alignment gates; active "
                "route falls back to LSP for loose/non-Composer PHP."
            ),
        ),
        scip_candidate_indexer="codeminer.scip_interface.scip_indexer_php:SCIPPHPIndexer",
        lsp_language_id="php",
        lsp_command=("intelephense", "--stdio"),
        lsp_command_env="CODEMINER_PHP_LSP_CMD",
    ),
    LanguageSpec(
        key="kotlin",
        display_name="Kotlin",
        aliases=("kt",),
        chunker_language="kotlin",
        chunker_aliases=("kotlin", "kt"),
        chunker_class="codeminer.code_chunking.kotlin_chunker:KotlinCodeChunker",
        chunk_extensions=(".kt", ".kts"),
        gt_language="kotlin",
        gt_extensions=(".kt", ".kts"),
        graph_language="kotlin",
        graph_aliases=("kotlin", "kt"),
        graph_extensions=(".kt", ".kts"),
        agent_languages=("kotlin",),
        agent_aliases=(("kotlin", "kotlin"), ("kt", "kotlin")),
        cold_start_backend="scip",
        graph_indexer="codeminer.scip_interface.scip_indexer_java:SCIPKotlinIndexer",
        graph_decoder="codeminer.scip_interface.scip_decode_java:SCIPKotlinGraphDecoder",
        scip_cold_start=ScipColdStartOption(
            tool="scip-java",
            status="active",
            command=("scip-java", "index"),
            command_env="CODEMINER_KOTLIN_SCIP_CMD",
            note=(
                "Primary Kotlin cold-start backend after generated and "
                "KotlinPoet 2.2.0 LSP alignment gates; use graph_route='lsp' "
                "for Kotlin LSP regression checks."
            ),
        ),
        scip_candidate_indexer="codeminer.scip_interface.scip_indexer_java:SCIPKotlinIndexer",
        lsp_language_id="kotlin",
        lsp_command=("kotlin-language-server", "--stdio"),
        lsp_command_env="CODEMINER_KOTLIN_LSP_CMD",
    ),
    LanguageSpec(
        key="swift",
        display_name="Swift",
        chunker_language="swift",
        chunker_aliases=("swift",),
        chunker_class="codeminer.code_chunking.swift_chunker:SwiftCodeChunker",
        chunk_extensions=(".swift",),
        gt_language="swift",
        gt_extensions=(".swift",),
        agent_languages=("swift",),
        agent_aliases=(("swift", "swift"),),
    ),
    LanguageSpec(
        key="scala",
        display_name="Scala",
        chunker_language="scala",
        chunker_aliases=("scala",),
        chunker_class="codeminer.code_chunking.scala_chunker:ScalaCodeChunker",
        chunk_extensions=(".scala", ".sc"),
        gt_language="scala",
        gt_extensions=(".scala", ".sc"),
        graph_language="scala",
        graph_aliases=("scala",),
        graph_extensions=(".scala", ".sc"),
        agent_languages=("scala",),
        agent_aliases=(("scala", "scala"),),
        cold_start_backend="scip",
        graph_indexer="codeminer.scip_interface.scip_indexer_java:SCIPScalaIndexer",
        graph_decoder="codeminer.scip_interface.scip_decode_java:SCIPJavaGraphDecoder",
        scip_cold_start=ScipColdStartOption(
            tool="scip-java",
            status="active",
            command=("scip-java", "index"),
            command_env="CODEMINER_SCALA_SCIP_CMD",
            note=(
                "Primary Scala 2.x cold-start backend after generated Gradle "
                "and real SBT scip-java smoke gates; no LSP baseline is "
                "registered for Scala."
            ),
        ),
        scip_candidate_indexer="codeminer.scip_interface.scip_indexer_java:SCIPScalaIndexer",
    ),
    LanguageSpec(
        key="lua",
        display_name="Lua",
        aliases=("luau",),
        chunker_language="lua",
        chunker_aliases=("lua", "luau"),
        chunker_class="codeminer.code_chunking.lua_chunker:LuaCodeChunker",
        chunk_extensions=(".lua", ".luau"),
        gt_language="lua",
        gt_extensions=(".lua", ".luau"),
        agent_languages=("lua",),
        agent_aliases=(("lua", "lua"), ("luau", "lua")),
    ),
    LanguageSpec(
        key="javascript",
        display_name="JavaScript",
        aliases=("js", "jsx"),
        chunker_language="javascript",
        chunker_aliases=("javascript", "js"),
        chunker_class="codeminer.code_chunking.js_chunker:JsTsCodeChunker",
        chunker_pass_language=True,
        chunk_extensions=(".js", ".jsx", ".mjs"),
        gt_language="javascript",
        gt_extensions=(".js", ".jsx"),
        graph_language="ts",
        graph_aliases=("javascript", "js"),
        graph_extensions=(".js", ".jsx"),
        agent_languages=("javascript",),
        agent_aliases=(
            ("javascript", "javascript"),
            ("js", "javascript"),
            ("jsx", "javascript"),
        ),
        cold_start_backend="scip",
        graph_indexer="codeminer.scip_interface.scip_indexer_ts:SCIPTypeScriptIndexer",
        graph_decoder="codeminer.scip_interface.scip_decode_ts:SCIPTypeScriptGraphDecoder",
        scip_cold_start=ScipColdStartOption(
            tool="scip-typescript",
            status="active",
            command=("scip-typescript", "index"),
            note=(
                "Shared JavaScript/TypeScript cold-start backend with parity "
                "via TypeScript core decoder."
            ),
        ),
        incremental_backend="lsp",
        incremental_patcher="codeminer.graph.incremental.patcher_ts:PatcherTS",
        lsp_language_id="typescript",
        lsp_command=("typescript-language-server", "--stdio"),
    ),
    LanguageSpec(
        key="typescript",
        display_name="TypeScript",
        aliases=("ts", "tsx"),
        chunker_language="typescript",
        chunker_aliases=("typescript", "ts"),
        chunker_class="codeminer.code_chunking.js_chunker:JsTsCodeChunker",
        chunker_pass_language=True,
        chunk_extensions=(".ts", ".tsx", ".mts", ".cts"),
        gt_language="typescript",
        gt_extensions=(".ts", ".tsx"),
        graph_language="ts",
        graph_aliases=("typescript", "ts"),
        graph_extensions=(".ts", ".tsx"),
        agent_languages=("typescript",),
        agent_aliases=(
            ("typescript", "typescript"),
            ("ts", "typescript"),
            ("tsx", "typescript"),
        ),
        cold_start_backend="scip",
        graph_indexer="codeminer.scip_interface.scip_indexer_ts:SCIPTypeScriptIndexer",
        graph_decoder="codeminer.scip_interface.scip_decode_ts:SCIPTypeScriptGraphDecoder",
        scip_cold_start=ScipColdStartOption(
            tool="scip-typescript",
            status="active",
            command=("scip-typescript", "index"),
            note="Primary cold-start backend with serial/core parity coverage.",
        ),
        incremental_backend="lsp",
        incremental_patcher="codeminer.graph.incremental.patcher_ts:PatcherTS",
        lsp_language_id="typescript",
        lsp_command=("typescript-language-server", "--stdio"),
        core_decoder=True,
        core_decoder_aliases=("ts", "js"),
    ),
)


def _norm(value: str) -> str:
    return value.strip().lower()


def _unique(values: Iterable[str]) -> Tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _language_attr(kind: ExtensionKind) -> str:
    return "chunker_language" if kind == "chunker" else f"{kind}_language"


def _extensions_attr(kind: ExtensionKind) -> str:
    return "chunk_extensions" if kind == "chunker" else f"{kind}_extensions"


SPECS_BY_KEY: Dict[str, LanguageSpec] = {spec.key: spec for spec in LANGUAGE_SPECS}

_GENERAL_ALIASES: Dict[str, str] = {}
for _spec in LANGUAGE_SPECS:
    _GENERAL_ALIASES[_spec.key] = _spec.key
    for _alias in _spec.aliases:
        _GENERAL_ALIASES[_alias] = _spec.key

_CHUNKER_ALIASES: Dict[str, str] = {}
for _spec in LANGUAGE_SPECS:
    if _spec.chunker_language:
        for _alias in _spec.chunker_aliases:
            _CHUNKER_ALIASES[_alias] = _spec.chunker_language

_AGENT_ALIASES: Dict[str, str] = {}
for _spec in LANGUAGE_SPECS:
    for _alias, _target in _spec.agent_aliases:
        _AGENT_ALIASES[_alias] = _target


def get_language_spec(language: str) -> Optional[LanguageSpec]:
    """Return the spec for a language key or general alias."""

    key = _GENERAL_ALIASES.get(_norm(language))
    if key is None:
        return None
    return SPECS_BY_KEY[key]


def normalize_language(language: str) -> Optional[str]:
    """Normalize a raw language string to a registry key."""

    spec = get_language_spec(language)
    return spec.key if spec else None


def normalize_chunker_language(language: str) -> Optional[str]:
    """Normalize a raw language string for ``create_chunker``."""

    return _CHUNKER_ALIASES.get(_norm(language))


def get_chunker_spec(language: str) -> Optional[LanguageSpec]:
    """Return the LanguageSpec used by the chunker factory."""

    chunker_language = normalize_chunker_language(language)
    if chunker_language is None:
        return None
    for spec in LANGUAGE_SPECS:
        if spec.chunker_language == chunker_language:
            return spec
    return None


def normalize_graph_language(language: str) -> Optional[str]:
    """Normalize a raw language string to the graph/LS backend key."""

    key = graph_language_aliases().get(_norm(language))
    return key


def normalize_agent_language(language: str) -> Optional[str]:
    """Normalize a raw language string for agent compile scenarios."""

    return _AGENT_ALIASES.get(_norm(language))


def supported_agent_languages() -> FrozenSet[str]:
    """Return language scenario keys accepted by agent compile."""

    return frozenset(lang for spec in LANGUAGE_SPECS for lang in spec.agent_languages)


def chunker_language_aliases() -> Dict[str, str]:
    """Return raw chunker aliases mapped to chunker language keys."""

    return dict(_CHUNKER_ALIASES)


def chunker_languages() -> Tuple[str, ...]:
    """Return canonical language keys accepted by repository chunking."""

    return _unique(
        spec.chunker_language for spec in LANGUAGE_SPECS if spec.chunker_language
    )


def chunker_class_paths(include_aliases: bool = True) -> Dict[str, str]:
    """Return chunker language keys mapped to chunker class paths."""

    by_language: Dict[str, str] = {}
    for spec in LANGUAGE_SPECS:
        if spec.chunker_language and spec.chunker_class:
            by_language[spec.chunker_language] = spec.chunker_class

    if not include_aliases:
        return dict(by_language)

    result = dict(by_language)
    for alias, language in chunker_language_aliases().items():
        if language in by_language:
            result[alias] = by_language[language]
    return result


def chunker_class_path(language: str) -> Optional[str]:
    """Return the chunker class path for a raw chunker language."""

    spec = get_chunker_spec(language)
    if spec is None:
        return None
    return spec.chunker_class


def graph_language_aliases() -> Dict[str, str]:
    """Return raw LS/graph aliases mapped to backend language keys."""

    aliases: Dict[str, str] = {}
    for spec in LANGUAGE_SPECS:
        if not spec.graph_language:
            continue
        aliases[spec.graph_language] = spec.graph_language
        for alias in spec.graph_aliases:
            aliases[alias] = spec.graph_language
    return aliases


def agent_language_aliases() -> Dict[str, str]:
    """Return raw agent language aliases mapped to scenario keys."""

    return dict(_AGENT_ALIASES)


def extension_to_language_map(kind: ExtensionKind) -> Dict[str, str]:
    """Return an extension-to-language map for the requested surface."""

    result: Dict[str, str] = {}
    for spec in LANGUAGE_SPECS:
        language = getattr(spec, _language_attr(kind))
        extensions = getattr(spec, _extensions_attr(kind))
        if not language:
            continue
        for ext in extensions:
            result[ext] = language
    return result


def extensions_for_language(language: str, kind: ExtensionKind) -> set[str]:
    """Return extensions for a language on the requested surface."""

    if kind == "graph":
        graph_key = normalize_graph_language(language)
        if graph_key is None:
            return set()
        return graph_extensions_by_language().get(graph_key, set())

    spec = get_language_spec(language)
    if spec is None:
        return set()
    return set(getattr(spec, _extensions_attr(kind)))


def graph_extensions_by_language(include_aliases: bool = True) -> Dict[str, set[str]]:
    """Return graph backend language keys mapped to accepted extensions."""

    by_backend: Dict[str, set[str]] = {}
    for spec in LANGUAGE_SPECS:
        if not spec.graph_language:
            continue
        by_backend.setdefault(spec.graph_language, set()).update(spec.graph_extensions)

    if not include_aliases:
        return {
            language: set(extensions) for language, extensions in by_backend.items()
        }

    result = {language: set(extensions) for language, extensions in by_backend.items()}
    aliases = graph_language_aliases()
    for alias, graph_language in aliases.items():
        if graph_language in by_backend:
            result[alias] = set(by_backend[graph_language])
    return result


def _graph_surface_values(attr: str, include_aliases: bool = True) -> Dict[str, str]:
    by_backend: Dict[str, str] = {}
    for spec in LANGUAGE_SPECS:
        if spec.graph_language:
            value = getattr(spec, attr)
            if value:
                by_backend[spec.graph_language] = value

    if not include_aliases:
        return dict(by_backend)

    result = dict(by_backend)
    aliases = graph_language_aliases()
    for alias, graph_language in aliases.items():
        if graph_language in by_backend:
            result[alias] = by_backend[graph_language]
    return result


def graph_indexer_paths(include_aliases: bool = True) -> Dict[str, str]:
    """Return graph backend language keys mapped to cold-start indexer paths."""

    return _graph_surface_values("graph_indexer", include_aliases=include_aliases)


def graph_decoder_paths(include_aliases: bool = True) -> Dict[str, str]:
    """Return graph backend language keys mapped to graph decoder paths."""

    return _graph_surface_values("graph_decoder", include_aliases=include_aliases)


def graph_cold_start_backends(include_aliases: bool = True) -> Dict[str, str]:
    """Return graph backend language keys mapped to cold-start backend names."""

    return _graph_surface_values("cold_start_backend", include_aliases=include_aliases)


def graph_indexer_path(language: str) -> Optional[str]:
    """Return the cold-start indexer class path for a raw graph language."""

    graph_language = normalize_graph_language(language)
    if graph_language is None:
        return None
    return graph_indexer_paths(include_aliases=False).get(graph_language)


def graph_decoder_path(language: str) -> Optional[str]:
    """Return the graph decoder class path for a raw graph language."""

    graph_language = normalize_graph_language(language)
    if graph_language is None:
        return None
    return graph_decoder_paths(include_aliases=False).get(graph_language)


def graph_cold_start_backend(language: str) -> Optional[str]:
    """Return the cold-start backend name for a raw graph language."""

    graph_language = normalize_graph_language(language)
    if graph_language is None:
        return None
    return graph_cold_start_backends(include_aliases=False).get(graph_language)


def scip_cold_start_options(
    include_aliases: bool = True,
) -> Dict[str, ScipColdStartOption]:
    """Return languages mapped to configured SCIP cold-start options.

    Candidate entries are intentionally returned even when a language's active
    graph backend is still LSP or tree-sitter-only. They are planning metadata,
    not an enabled router path.
    """

    result: Dict[str, ScipColdStartOption] = {}
    for spec in LANGUAGE_SPECS:
        option = spec.scip_cold_start
        if option is None:
            continue
        result[spec.key] = option
        if include_aliases:
            for alias in spec.aliases:
                result[alias] = option
            if spec.graph_language:
                result[spec.graph_language] = option
                for alias in spec.graph_aliases:
                    result[alias] = option
    return result


def scip_cold_start_option(language: str) -> Optional[ScipColdStartOption]:
    """Return SCIP cold-start metadata for a language key or alias."""

    return scip_cold_start_options(include_aliases=True).get(_norm(language))


def scip_candidate_indexer_paths(include_aliases: bool = True) -> Dict[str, str]:
    """Return languages mapped to explicit candidate SCIP indexer class paths.

    These paths are opt-in only. They do not change a language's active graph
    backend and are used by smoke/profiling flows that intentionally evaluate a
    candidate SCIP cold-start route.
    """

    result: Dict[str, str] = {}
    for spec in LANGUAGE_SPECS:
        path = spec.scip_candidate_indexer
        if not path:
            continue
        result[spec.key] = path
        if include_aliases:
            for alias in spec.aliases:
                result[alias] = path
            if spec.graph_language:
                result[spec.graph_language] = path
                for alias in spec.graph_aliases:
                    result[alias] = path
    return result


def scip_candidate_indexer_path(language: str) -> Optional[str]:
    """Return an opt-in candidate SCIP indexer path for a language or alias."""

    return scip_candidate_indexer_paths(include_aliases=True).get(_norm(language))


def scip_cold_start_command_for_language(language: str) -> Optional[list[str]]:
    """Return the configured SCIP cold-start command for a language or alias."""

    option = scip_cold_start_option(language)
    if option is None:
        return None

    if option.command_env:
        override = os.environ.get(option.command_env)
        if override:
            return shlex.split(override)
    if option.command:
        return list(option.command)
    return [option.tool] if option.tool else None


def lsp_language_id_for_language(language: str) -> Optional[str]:
    """Return the LSP language id for a raw graph language."""

    graph_language = normalize_graph_language(language)
    if graph_language is None:
        return None
    return _graph_surface_values("lsp_language_id", include_aliases=False).get(
        graph_language
    )


def _call_no_arg_factory(path: str) -> list[str]:
    module_name, separator, func_name = path.partition(":")
    if not separator or not module_name or not func_name:
        raise ValueError(f"Invalid factory path: {path!r}")
    module = import_module(module_name)
    func = getattr(module, func_name)
    result = func()
    return list(result)


def lsp_command_for_language(language: str) -> Optional[list[str]]:
    """Return the configured LSP command for a raw graph language."""

    graph_language = normalize_graph_language(language)
    if graph_language is None:
        return None

    spec = next(
        (spec for spec in LANGUAGE_SPECS if spec.graph_language == graph_language),
        None,
    )
    if spec is None:
        return None

    if spec.lsp_command_env:
        override = os.environ.get(spec.lsp_command_env)
        if override:
            return shlex.split(override)
    if spec.lsp_command_factory:
        return _call_no_arg_factory(spec.lsp_command_factory)
    if spec.lsp_command:
        return list(spec.lsp_command)
    return None


def incremental_patcher_paths(include_aliases: bool = True) -> Dict[str, str]:
    """Return graph backend language keys mapped to incremental patcher paths."""

    return _graph_surface_values("incremental_patcher", include_aliases=include_aliases)


def incremental_patcher_path(language: str) -> Optional[str]:
    """Return the incremental patcher class path for a raw graph language."""

    graph_language = normalize_graph_language(language)
    if graph_language is None:
        return None
    return incremental_patcher_paths(include_aliases=False).get(graph_language)


def core_decoder_languages(include_aliases: bool = True) -> Tuple[str, ...]:
    """Return languages accepted by the optional C++ core decoder."""

    languages: list[str] = []
    for spec in LANGUAGE_SPECS:
        if spec.core_decoder:
            languages.append(spec.key)
            if include_aliases:
                languages.extend(spec.core_decoder_aliases)
    return _unique(languages)


def language_capability_rows() -> Tuple[LanguageCapability, ...]:
    """Return a registry-derived language support matrix.

    The matrix intentionally distinguishes tree-sitter-only languages from
    graph-capable languages with no core decoder.  This makes serial-only/core
    parity coverage explicit instead of relying on skipped tests as a signal.
    """

    core_graph_languages = {
        spec.graph_language
        for spec in LANGUAGE_SPECS
        if spec.core_decoder and spec.graph_language
    }
    rows: list[LanguageCapability] = []
    for spec in LANGUAGE_SPECS:
        has_graph = bool(
            spec.graph_language and spec.graph_indexer and spec.graph_decoder
        )
        has_core = bool(
            spec.core_decoder
            or (spec.graph_language and spec.graph_language in core_graph_languages)
        )
        if has_core:
            core_parity = "covered"
        elif has_graph:
            core_parity = "n/a-no-core-decoder"
        else:
            core_parity = "n/a-tree-sitter-only"

        rows.append(
            LanguageCapability(
                key=spec.key,
                display_name=spec.display_name,
                chunker=bool(spec.chunker_language and spec.chunker_class),
                ground_truth=bool(spec.gt_language and spec.gt_extensions),
                agent=bool(spec.agent_languages),
                graph_backend=spec.cold_start_backend if has_graph else None,
                scip_cold_start=(
                    spec.scip_cold_start.status if spec.scip_cold_start else "none"
                ),
                incremental_backend=(
                    spec.incremental_backend if spec.incremental_patcher else None
                ),
                lsp=bool(
                    spec.lsp_language_id
                    and (
                        spec.lsp_command
                        or spec.lsp_command_env
                        or spec.lsp_command_factory
                    )
                ),
                core_decoder=has_core,
                core_parity=core_parity,
            )
        )
    return tuple(rows)


__all__ = [
    "ExtensionKind",
    "LanguageCapability",
    "LanguageSpec",
    "LANGUAGE_SPECS",
    "SPECS_BY_KEY",
    "ScipColdStartOption",
    "ScipColdStartStatus",
    "agent_language_aliases",
    "chunker_language_aliases",
    "chunker_languages",
    "chunker_class_path",
    "chunker_class_paths",
    "core_decoder_languages",
    "extension_to_language_map",
    "extensions_for_language",
    "get_chunker_spec",
    "get_language_spec",
    "graph_cold_start_backend",
    "graph_cold_start_backends",
    "graph_decoder_path",
    "graph_decoder_paths",
    "graph_extensions_by_language",
    "graph_indexer_path",
    "graph_indexer_paths",
    "graph_language_aliases",
    "incremental_patcher_path",
    "incremental_patcher_paths",
    "language_capability_rows",
    "lsp_command_for_language",
    "lsp_language_id_for_language",
    "normalize_agent_language",
    "normalize_chunker_language",
    "normalize_graph_language",
    "normalize_language",
    "scip_candidate_indexer_path",
    "scip_candidate_indexer_paths",
    "scip_cold_start_option",
    "scip_cold_start_command_for_language",
    "scip_cold_start_options",
    "supported_agent_languages",
]
