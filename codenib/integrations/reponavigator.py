# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""RepoNavigator's published ``jump`` tool over CodeNib repository views."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping

from ..agent.lsp_provider import JSON_RPC_LSP_PROVIDER, StaticLSPProvider
from ._repository import (
    IntegrationCapabilityError,
    RepositoryAdapter,
    RepositoryPathError,
)

REPONAVIGATOR_PAPER_REVISION = "arXiv:2512.20957v6"

_RUNTIME_VIEWS = frozenset({"symbol_graph"})
_TOOL_NAME = "jump"
_TRUNCATION_MARKER = "\n...[truncated RepoNavigator definition]"
_GRAPH_BEHAVIOR_CONTRACTS = frozenset(
    {"static_graph_lsp_v1", "static_symbol_graph_position_lsp_v1"}
)
_SCIP_OCCURRENCE_BEHAVIOR_CONTRACT = "static_scip_occurrence_lsp_v1"
_NATIVE_OCCURRENCE_BEHAVIOR_CONTRACT = "static_native_occurrence_lsp_v1"

_REPONAVIGATOR_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": _TOOL_NAME,
            "description": (
                "Jump to the definition of one symbol occurrence in a repository "
                "file and return its definition source code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": (
                            "Repository-relative path where the symbol is referenced, "
                            "not the path where it is defined."
                        ),
                    },
                    "symbol": {
                        "type": "string",
                        "description": (
                            "Exact referenced symbol text to resolve; for an attribute "
                            "such as object.method, pass method."
                        ),
                    },
                    "index": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": (
                            "Zero-based occurrence index used when the symbol appears "
                            "more than once in the file."
                        ),
                    },
                },
                "required": ["file_path", "symbol"],
                "additionalProperties": False,
            },
        },
    },
)


def get_reponavigator_tool_schemas() -> list[dict[str, Any]]:
    """Return an independent schema for RepoNavigator's single published tool."""

    return copy.deepcopy(list(_REPONAVIGATOR_TOOL_SCHEMAS))


@dataclass(frozen=True, slots=True)
class RepoNavigatorDefinitionSignal:
    """Definition backend selected for the RepoNavigator application adapter."""

    backend: str
    provider: str
    behavior_contract: str
    position_granularity: str
    position_encoding: str
    semantic: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "provider": self.provider,
            "behavior_contract": self.behavior_contract,
            "position_granularity": self.position_granularity,
            "position_encoding": self.position_encoding,
            "semantic": self.semantic,
            "detail": self.detail,
        }


class RepoNavigatorRepositoryProvider:
    """Serve the paper-specified ``jump`` contract from one CodeNib manifest.

    The paper supplies ``file_path``, ``symbol``, and an optional occurrence
    ``index`` instead of an LSP position.  This adapter resolves that occurrence
    locally, delegates definition lookup to CodeNib's LSP-shaped provider, and
    returns the containing definition source.  It does not implement the
    RepoNavigator policy or reinforcement-learning pipeline.
    """

    def __init__(
        self,
        context: Any,
        *,
        lsp_provider: Any | None = None,
        occurrence_index: Any | None = None,
        allow_graph_fallback: bool = False,
        max_source_lines: int = 400,
        max_source_chars: int = 32_000,
    ) -> None:
        if not isinstance(allow_graph_fallback, bool):
            raise TypeError("allow_graph_fallback must be a boolean")
        if occurrence_index is not None and (
            not callable(getattr(occurrence_index, "occurrence_at", None))
            or not callable(getattr(occurrence_index, "definitions", None))
        ):
            raise IntegrationCapabilityError(
                "RepoNavigator occurrence_index must expose occurrence_at and "
                "definitions"
            )
        if (
            isinstance(max_source_lines, bool)
            or not isinstance(max_source_lines, int)
            or max_source_lines < 1
        ):
            raise ValueError("max_source_lines must be positive")
        if (
            isinstance(max_source_chars, bool)
            or not isinstance(max_source_chars, int)
            or max_source_chars < len(_TRUNCATION_MARKER)
        ):
            raise ValueError(
                "max_source_chars must be large enough for the truncation marker"
            )
        self.repository = RepositoryAdapter(context, require_graph=True)
        context_provider = getattr(context, "lsp_provider", None)
        if lsp_provider is not None:
            self.lsp_provider = lsp_provider
        elif occurrence_index is not None and (
            context_provider is None or isinstance(context_provider, StaticLSPProvider)
        ):
            # ServerContext attaches a graph-only StaticLSPProvider while it
            # loads the symbol view.  A persisted occurrence index discovered
            # beside that view is strictly more precise, so rebind the static
            # provider without discarding its snapshot/backend provenance.
            self.lsp_provider = StaticLSPProvider(
                self.repository.graph,
                snapshot_id=getattr(context_provider, "snapshot_id", None),
                occurrence_index=occurrence_index,
                backend=getattr(context_provider, "backend", None),
                fallback_reason=getattr(context_provider, "fallback_reason", None),
            )
        elif context_provider is not None:
            self.lsp_provider = context_provider
        else:
            self.lsp_provider = StaticLSPProvider(
                self.repository.graph,
                occurrence_index=occurrence_index,
            )
        if not callable(getattr(self.lsp_provider, "definition", None)):
            raise IntegrationCapabilityError(
                "RepoNavigator requires a callable definition provider"
            )
        self._configured_definition_signal = _inspect_definition_signal(
            self.lsp_provider
        )
        if not self._configured_definition_signal.semantic and not allow_graph_fallback:
            raise IntegrationCapabilityError(
                "RepoNavigator requires a semantic definition signal backed by "
                "SCIP occurrences or a language server; only the symbol-graph "
                "fallback is available. Pass allow_graph_fallback=True to opt "
                "into degraded behavior."
            )
        if not self._configured_definition_signal.semantic and isinstance(
            self.lsp_provider, StaticLSPProvider
        ):
            # An empty occurrence adapter cannot select a textual occurrence,
            # and authoritative-empty native adapters also suppress the static
            # provider's graph fallback.  Once degraded behavior is explicitly
            # allowed, bind the graph-only provider that the signal advertises.
            graph_provider = StaticLSPProvider(
                self.repository.graph,
                snapshot_id=getattr(self.lsp_provider, "snapshot_id", None),
                backend=getattr(self.lsp_provider, "backend", None),
                fallback_reason=getattr(self.lsp_provider, "fallback_reason", None),
            )
            # StaticLSPProvider normally discovers an occurrence index attached
            # to the graph.  This branch is explicitly graph-only, including
            # for in-memory graphs that still carry the rejected empty index.
            graph_provider.occurrence_index = None
            graph_provider.position_encoding = None
            self.lsp_provider = graph_provider
        self._signal_lock = RLock()
        self._definition_signal = self._configured_definition_signal
        self.allow_graph_fallback = bool(allow_graph_fallback)
        self.max_source_lines = max_source_lines
        self.max_source_chars = max_source_chars

    @classmethod
    def from_manifest(
        cls,
        manifest_path: str | Path,
        **kwargs: Any,
    ) -> "RepoNavigatorRepositoryProvider":
        from ..mcp.context import ServerContext

        context = ServerContext.load(manifest_path, views=_RUNTIME_VIEWS)
        if (
            kwargs.get("lsp_provider") is None
            and "occurrence_index" not in kwargs
            and (
                getattr(context, "lsp_provider", None) is None
                or isinstance(context.lsp_provider, StaticLSPProvider)
            )
        ):
            graph = getattr(context, "symbol_graph", None)
            occurrence_index = getattr(graph, "lsp_occurrence_index", None)
            if occurrence_index is not None:
                kwargs["occurrence_index"] = occurrence_index
        return cls(context, **kwargs)

    @property
    def context(self) -> Any:
        return self.repository.context

    @property
    def definition_signal(self) -> RepoNavigatorDefinitionSignal:
        with self._signal_lock:
            return self._definition_signal

    def signal_metadata(self) -> dict[str, Any]:
        """Return JSON-safe evidence for the configured or latest provider call."""

        return self.definition_signal.to_dict()

    def _record_definition_signal(
        self,
        signal: RepoNavigatorDefinitionSignal,
    ) -> None:
        with self._signal_lock:
            self._definition_signal = signal

    def bindings(self) -> dict[str, Callable[..., str]]:
        """Return the one callable exposed to a RepoNavigator-style runtime."""

        return {_TOOL_NAME: self.jump}

    def dispatch(
        self,
        name: str,
        arguments: Mapping[str, Any] | str | None = None,
    ) -> str:
        """Dispatch one JSON or mapping tool call without evaluating source."""

        if isinstance(arguments, str):
            try:
                decoded = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "RepoNavigator tool arguments must be valid JSON"
                ) from exc
            if not isinstance(decoded, Mapping):
                raise ValueError(
                    "RepoNavigator tool arguments must decode to an object"
                )
            parsed = dict(decoded)
        else:
            parsed = dict(arguments or {})

        if str(name).lower() != _TOOL_NAME:
            raise KeyError(
                f"unknown RepoNavigator tool {name!r}; supported: {_TOOL_NAME}"
            )
        return self.jump(**parsed)

    def jump(self, file_path: str, symbol: str, index: int = 0) -> str:
        """Return one definition observation for a symbol occurrence."""

        configured_signal = self._configured_definition_signal
        if isinstance(file_path, str) and "\0" in file_path:
            return _jump_error("file_path must not contain NUL characters")
        raw_symbol = str(symbol or "").strip()
        if not raw_symbol or "\n" in raw_symbol or "\r" in raw_symbol:
            return _jump_error("symbol must be non-empty single-line text")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            return _jump_error("index must be a non-negative integer")

        try:
            normalized_path = self.repository.normalize_path(file_path)
            source_lines = self.repository.source_lines(normalized_path)
        except (OSError, RepositoryPathError, ValueError) as exc:
            return _jump_error(str(exc))

        occurrences = _encoded_symbol_occurrences(
            source_lines,
            raw_symbol,
            encoding=configured_signal.position_encoding,
        )
        occurrence_index = getattr(self.lsp_provider, "occurrence_index", None)
        occurrence_at = getattr(occurrence_index, "occurrence_at", None)
        if callable(occurrence_at):
            occurrences = [
                (line, character)
                for line, character in occurrences
                if occurrence_at(normalized_path, line, character) is not None
            ]
        if not occurrences:
            return _jump_error(
                f"symbol {raw_symbol!r} has no resolvable occurrence in "
                f"{normalized_path}"
            )
        if index >= len(occurrences):
            return _jump_error(
                f"symbol {raw_symbol!r} has {len(occurrences)} resolvable "
                f"occurrence(s) in {normalized_path}; index {index} is out of range"
            )

        line, character = occurrences[index]
        try:
            raw_definitions = self.lsp_provider.definition(
                file_path=normalized_path,
                line=line,
                character=character,
                top_k=1,
            )
            call_signal = _definition_result_signal(
                raw_definitions,
                configured=configured_signal,
            )
            self._record_definition_signal(call_signal)
            if not call_signal.semantic and not self.allow_graph_fallback:
                return _jump_error(
                    "definition resolution degraded to the symbol graph; pass "
                    "allow_graph_fallback=True to permit this call"
                )
            definitions = _definition_nodes(raw_definitions)
        except Exception as exc:  # noqa: BLE001 - external provider boundary
            return _jump_error(str(exc) or "definition provider failed")
        if not definitions:
            return _jump_error(
                f"definition provider returned no result for {raw_symbol!r} at "
                f"{normalized_path}:{line + 1}"
            )

        try:
            content, definition_path = self._definition_source(definitions[0])
        except (OSError, RepositoryPathError, TypeError, ValueError) as exc:
            return _jump_error(str(exc))
        if not content:
            return _jump_error(
                f"definition source is empty for {raw_symbol!r} in {normalized_path}"
            )
        bounded = _bounded_source(
            content,
            max_lines=self.max_source_lines,
            max_chars=self.max_source_chars,
        )
        return _jump_observation(raw_symbol, bounded, definition_path)

    def _definition_source(self, node: Any) -> tuple[str, str]:
        file_path = _node_value(node, "file") or _node_value(node, "file_path")
        start_line = _node_value(node, "start_line")
        if not file_path:
            raise ValueError("definition result has no repository source path")
        if isinstance(start_line, bool) or not isinstance(start_line, int):
            raise ValueError("definition result has no valid source line")

        normalized_path = self.repository.normalize_path(str(file_path))
        entity = self.repository.containing_entity(normalized_path, start_line)
        if entity is not None:
            content, _, _ = self.repository.read_entity(entity)
            return content, normalized_path
        content, _, _ = self.repository.read_range(
            normalized_path,
            start_line=start_line,
            end_line=start_line,
        )
        return content, normalized_path


def _inspect_definition_signal(provider: Any) -> RepoNavigatorDefinitionSignal:
    if isinstance(provider, StaticLSPProvider):
        decision = provider.can_serve("definition")
        if decision.status != "ok":
            raise IntegrationCapabilityError(
                "RepoNavigator definition provider is unavailable: "
                f"{decision.fallback_reason or decision.status}"
            )
        occurrence_index = getattr(provider, "occurrence_index", None)
        occurrence_signal_ready = _occurrence_signal_ready(occurrence_index)
        if occurrence_signal_ready:
            behavior_contract = str(
                decision.behavior_contract or _SCIP_OCCURRENCE_BEHAVIOR_CONTRACT
            )
            is_native = behavior_contract == _NATIVE_OCCURRENCE_BEHAVIOR_CONTRACT
            return RepoNavigatorDefinitionSignal(
                backend="native_occurrence" if is_native else "scip_occurrence",
                provider=decision.provider,
                behavior_contract=behavior_contract,
                position_granularity=decision.position_granularity,
                position_encoding=_normalize_position_encoding(
                    getattr(occurrence_index, "position_encoding", None),
                    default="utf-16" if is_native else "utf-8",
                ),
                semantic=True,
                detail=(
                    "character-level definition resolution from the native "
                    "occurrence index"
                    if is_native
                    else (
                        "character-level definition resolution from the persisted "
                        "SCIP occurrence index"
                    )
                ),
            )
        empty_kind = (
            "native"
            if str(decision.behavior_contract) == _NATIVE_OCCURRENCE_BEHAVIOR_CONTRACT
            else "SCIP"
        )
        fallback_detail = (
            f"{empty_kind} occurrence index is empty; using line/graph "
            "definition approximation"
            if occurrence_index is not None
            else (
                "line/graph definition approximation; no persisted SCIP "
                "occurrence index or live language server"
            )
        )
        return RepoNavigatorDefinitionSignal(
            backend="symbol_graph_fallback",
            provider=decision.provider,
            behavior_contract="static_graph_lsp_v1",
            position_granularity="line",
            position_encoding="utf-32",
            semantic=False,
            detail=fallback_detail,
        )

    provider_name = str(
        getattr(provider, "provider", None) or "external_definition_provider"
    )
    if provider_name == JSON_RPC_LSP_PROVIDER:
        return RepoNavigatorDefinitionSignal(
            backend="live_lsp",
            provider=provider_name,
            behavior_contract="json_rpc_lsp_v1",
            position_granularity="character",
            position_encoding=_normalize_position_encoding(
                getattr(provider, "position_encoding", None),
                default="utf-16",
            ),
            semantic=True,
            detail="character-level definition resolution from a live language server",
        )

    return RepoNavigatorDefinitionSignal(
        backend="external_definition_provider",
        provider=provider_name,
        behavior_contract=str(
            getattr(provider, "behavior_contract", None)
            or "caller_attested_definition_v1"
        ),
        position_granularity=str(
            getattr(provider, "position_granularity", None) or "character"
        ),
        position_encoding=_normalize_position_encoding(
            getattr(provider, "position_encoding", None),
            default="utf-32",
        ),
        semantic=True,
        detail="caller-injected definition provider; fidelity is caller-attested",
    )


def _occurrence_signal_ready(occurrence_index: Any | None) -> bool:
    if occurrence_index is None:
        return False
    occurrence_count = getattr(occurrence_index, "occurrence_count", None)
    if occurrence_count is not None:
        if isinstance(occurrence_count, bool) or not isinstance(occurrence_count, int):
            return False
        return occurrence_count > 0
    occurrence_rows = getattr(occurrence_index, "occurrences", None)
    return occurrence_rows is None or bool(occurrence_rows)


def _definition_result_signal(
    result: Any,
    *,
    configured: RepoNavigatorDefinitionSignal,
) -> RepoNavigatorDefinitionSignal:
    metadata_method = getattr(result, "provider_metadata_dict", None)
    metadata: Mapping[str, Any] | None = None
    if callable(metadata_method):
        candidate = metadata_method()
        if isinstance(candidate, Mapping):
            metadata = candidate
    if metadata is None:
        candidate = getattr(result, "lsp_provider_metadata", None)
        to_dict = getattr(candidate, "to_dict", None)
        if callable(to_dict):
            candidate = to_dict()
        if isinstance(candidate, Mapping):
            metadata = candidate
    if metadata is None:
        return configured

    behavior_contract = str(
        metadata.get("behavior_contract") or configured.behavior_contract
    )
    provider = str(metadata.get("provider") or configured.provider)
    position_granularity = str(
        metadata.get("position_granularity") or configured.position_granularity
    )
    position_encoding = _normalize_position_encoding(
        metadata.get("position_encoding"),
        default=configured.position_encoding,
    )
    if behavior_contract in _GRAPH_BEHAVIOR_CONTRACTS:
        fallback_reason = str(metadata.get("fallback_reason") or "").strip()
        detail = "definition call used the symbol-graph fallback"
        if fallback_reason:
            detail += f" ({fallback_reason})"
        return RepoNavigatorDefinitionSignal(
            backend="symbol_graph_fallback",
            provider=provider,
            behavior_contract=behavior_contract,
            position_granularity=position_granularity,
            position_encoding=position_encoding,
            semantic=False,
            detail=detail,
        )
    if behavior_contract in {
        _SCIP_OCCURRENCE_BEHAVIOR_CONTRACT,
        _NATIVE_OCCURRENCE_BEHAVIOR_CONTRACT,
    }:
        is_native = behavior_contract == _NATIVE_OCCURRENCE_BEHAVIOR_CONTRACT
        return RepoNavigatorDefinitionSignal(
            backend="native_occurrence" if is_native else "scip_occurrence",
            provider=provider,
            behavior_contract=behavior_contract,
            position_granularity=position_granularity,
            position_encoding=position_encoding,
            semantic=True,
            detail=(
                "character-level definition resolution from the native occurrence "
                "index"
                if is_native
                else (
                    "character-level definition resolution from the persisted SCIP "
                    "occurrence index"
                )
            ),
        )
    return RepoNavigatorDefinitionSignal(
        backend=configured.backend,
        provider=provider,
        behavior_contract=behavior_contract,
        position_granularity=position_granularity,
        position_encoding=position_encoding,
        semantic=configured.semantic,
        detail=configured.detail,
    )


def _normalize_position_encoding(value: Any, *, default: str) -> str:
    compact = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    if "UTF8" in compact:
        return "utf-8"
    if "UTF16" in compact:
        return "utf-16"
    if "UTF32" in compact:
        return "utf-32"
    return default


def _encoded_symbol_occurrences(
    lines: list[str],
    symbol: str,
    *,
    encoding: str,
) -> list[tuple[int, int]]:
    return [
        (line, _position_character(lines[line], character, encoding=encoding))
        for line, character in _symbol_occurrences(lines, symbol)
    ]


def _position_character(source_line: str, character: int, *, encoding: str) -> int:
    prefix = source_line[:character]
    if encoding == "utf-8":
        return len(prefix.encode("utf-8"))
    if encoding == "utf-16":
        return len(prefix.encode("utf-16-le")) // 2
    return len(prefix)


def _definition_nodes(result: Any) -> list[Any]:
    if result is None:
        return []
    if isinstance(result, Mapping):
        error = result.get("error")
        if error:
            raise ValueError(str(error))
        if (result.get("file") or result.get("file_path")) and result.get(
            "start_line"
        ) is not None:
            return [result]
        raise ValueError("definition provider returned an invalid object")
    if isinstance(result, (str, bytes)):
        raise ValueError("definition provider returned text instead of locations")
    try:
        return list(result)
    except TypeError as exc:
        raise ValueError("definition provider returned a non-iterable result") from exc


def _symbol_occurrences(lines: list[str], symbol: str) -> list[tuple[int, int]]:
    escaped = re.escape(symbol)
    left = r"(?<![\w$])" if _identifier_edge(symbol[0]) else ""
    right = r"(?![\w$])" if _identifier_edge(symbol[-1]) else ""
    pattern = re.compile(f"{left}{escaped}{right}")

    occurrences: list[tuple[int, int]] = []
    for line_number, source_line in enumerate(lines):
        for match in pattern.finditer(source_line):
            # Query the final character so dotted names resolve their invoked
            # member rather than the receiver at the start of the expression.
            occurrences.append((line_number, match.end() - 1))
    return occurrences


def _identifier_edge(character: str) -> bool:
    return character == "$" or character == "_" or character.isalnum()


def _node_value(node: Any, key: str) -> Any:
    if isinstance(node, Mapping):
        return node.get(key)
    return getattr(node, key, None)


def _bounded_source(content: str, *, max_lines: int, max_chars: int) -> str:
    lines = content.splitlines()
    truncated = len(lines) > max_lines
    bounded = "\n".join(lines[:max_lines]) if truncated else content
    if len(bounded) > max_chars:
        truncated = True
    if not truncated:
        return bounded

    available = max_chars - len(_TRUNCATION_MARKER)
    return bounded[:available].rstrip() + _TRUNCATION_MARKER


def _jump_observation(symbol: str, content: str, file_path: str) -> str:
    quoted_symbol = json.dumps(symbol, ensure_ascii=False)
    return (
        f"The definition of symbol {quoted_symbol} is:\n"
        f"{content}\n\n"
        f"It is defined in: {file_path}"
    )


def _jump_error(message: str) -> str:
    detail = str(message or "unknown definition error").strip()
    return f"Jump failed: {detail}"


__all__ = [
    "REPONAVIGATOR_PAPER_REVISION",
    "RepoNavigatorDefinitionSignal",
    "RepoNavigatorRepositoryProvider",
    "get_reponavigator_tool_schemas",
]
