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
    incremental_backend: Optional[str] = None
    incremental_patcher: Optional[str] = None
    lsp_language_id: Optional[str] = None
    lsp_command: Tuple[str, ...] = ()
    lsp_command_env: Optional[str] = None
    lsp_command_factory: Optional[str] = None
    core_decoder: bool = False
    core_decoder_aliases: Tuple[str, ...] = ()


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
        key="java",
        display_name="Java",
        chunker_language="java",
        chunker_aliases=("java",),
        chunker_class="codeminer.code_chunking.java_chunker:JavaCodeChunker",
        chunk_extensions=(".java",),
        gt_language="java",
        gt_extensions=(".java",),
        agent_languages=("java",),
        agent_aliases=(("java", "java"),),
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
        agent_languages=("ruby",),
        agent_aliases=(("ruby", "ruby"), ("rb", "ruby")),
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


__all__ = [
    "ExtensionKind",
    "LanguageSpec",
    "LANGUAGE_SPECS",
    "SPECS_BY_KEY",
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
    "lsp_command_for_language",
    "lsp_language_id_for_language",
    "normalize_agent_language",
    "normalize_chunker_language",
    "normalize_graph_language",
    "normalize_language",
    "supported_agent_languages",
]
