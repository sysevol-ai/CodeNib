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

from dataclasses import dataclass
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
    chunk_extensions: Tuple[str, ...] = ()
    gt_language: Optional[str] = None
    gt_extensions: Tuple[str, ...] = ()
    graph_language: Optional[str] = None
    graph_aliases: Tuple[str, ...] = ()
    graph_extensions: Tuple[str, ...] = ()
    agent_languages: Tuple[str, ...] = ()
    agent_aliases: Tuple[Tuple[str, str], ...] = ()
    cold_start_backend: Optional[str] = None
    incremental_backend: Optional[str] = None
    core_decoder: bool = False
    core_decoder_aliases: Tuple[str, ...] = ()


LANGUAGE_SPECS: Tuple[LanguageSpec, ...] = (
    LanguageSpec(
        key="python",
        display_name="Python",
        aliases=("py", "python3"),
        chunker_language="python",
        chunker_aliases=("python",),
        chunk_extensions=(".py", ".pyx", ".pyi"),
        gt_language="python",
        gt_extensions=(".py",),
        graph_language="python",
        graph_aliases=("python", "py"),
        graph_extensions=(".py",),
        agent_languages=("python",),
        agent_aliases=(("python", "python"), ("py", "python"), ("python3", "python")),
        cold_start_backend="scip",
        incremental_backend="lsp",
        core_decoder=True,
    ),
    LanguageSpec(
        key="go",
        display_name="Go",
        aliases=("golang",),
        chunker_language="go",
        chunker_aliases=("go", "golang"),
        chunk_extensions=(".go",),
        gt_language="go",
        gt_extensions=(".go",),
        graph_language="go",
        graph_aliases=("go", "golang"),
        graph_extensions=(".go",),
        agent_languages=("go",),
        agent_aliases=(("go", "go"), ("golang", "go")),
        cold_start_backend="scip",
        incremental_backend="lsp",
        core_decoder=True,
    ),
    LanguageSpec(
        key="rust",
        display_name="Rust",
        aliases=("rs",),
        chunker_language="rust",
        chunker_aliases=("rust",),
        chunk_extensions=(".rs",),
        gt_language="rust",
        gt_extensions=(".rs",),
        graph_language="rust",
        graph_aliases=("rust", "rs"),
        graph_extensions=(".rs",),
        agent_languages=("rust",),
        agent_aliases=(("rust", "rust"), ("rs", "rust")),
        cold_start_backend="scip",
        incremental_backend="lsp",
        core_decoder=True,
    ),
    LanguageSpec(
        key="cpp",
        display_name="C/C++",
        aliases=("c", "c++", "cxx"),
        chunker_language="cpp",
        chunker_aliases=("cpp", "c++", "cxx"),
        chunk_extensions=(".cpp", ".cxx", ".cc", ".c", ".hpp", ".h", ".hxx"),
        gt_language="cpp",
        gt_extensions=(".c", ".cpp", ".cc", ".cxx", ".h", ".hpp"),
        graph_language="cpp",
        graph_aliases=("cpp", "c++", "c"),
        graph_extensions=(".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx"),
        agent_languages=("cpp", "c"),
        agent_aliases=(("cpp", "cpp"), ("c++", "cpp"), ("cxx", "cpp"), ("c", "c")),
        cold_start_backend="clangd",
        incremental_backend="clangd",
    ),
    LanguageSpec(
        key="javascript",
        display_name="JavaScript",
        aliases=("js", "jsx"),
        chunker_language="javascript",
        chunker_aliases=("javascript", "js"),
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
        incremental_backend="lsp",
    ),
    LanguageSpec(
        key="typescript",
        display_name="TypeScript",
        aliases=("ts", "tsx"),
        chunker_language="typescript",
        chunker_aliases=("typescript", "ts"),
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
        incremental_backend="lsp",
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
        language = getattr(spec, f"{kind}_language")
        extensions = getattr(spec, f"{kind}_extensions")
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
    return set(getattr(spec, f"{kind}_extensions"))


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
    "core_decoder_languages",
    "extension_to_language_map",
    "extensions_for_language",
    "get_language_spec",
    "graph_extensions_by_language",
    "graph_language_aliases",
    "normalize_agent_language",
    "normalize_chunker_language",
    "normalize_graph_language",
    "normalize_language",
    "supported_agent_languages",
]
