# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""JSON-RPC LSP reference provider for static-provider validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import unquote, urlparse

from codeminer.agent.lsp_provider import (
    CAPABILITY_DEFINITION,
    CAPABILITY_REFERENCES,
    JSON_RPC_LSP_PROVIDER,
    normalize_lsp_capability,
)
from codeminer.graph.incremental.lsp_client import LSPClient
from codeminer.languages import normalize_graph_language
from codeminer.types import QueriedNode

ClientFactory = Callable[..., Any]


class LiveLSPReferenceProvider:
    """Callable reference provider backed by a live JSON-RPC language server."""

    provider = JSON_RPC_LSP_PROVIDER

    def __init__(
        self,
        *,
        project_root: str | Path,
        language: str,
        command: Optional[Sequence[str]] = None,
        client_factory: ClientFactory = LSPClient,
        skip_probe: bool = False,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.language = normalize_graph_language(language) or language
        self.command = list(command) if command is not None else None
        self.client_factory = client_factory
        self.skip_probe = bool(skip_probe)
        self._client: Any = None

    def __enter__(self) -> "LiveLSPReferenceProvider":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> None:
        """Start the language server if it is not already running."""

        if self._client is not None:
            return
        command = self.command or LSPClient.get_lsp_command(self.language)
        if not command:
            raise RuntimeError(f"no LSP command registered for {self.language!r}")
        client = self.client_factory(command, str(self.project_root), self.language)
        start = getattr(client, "start", None)
        if callable(start):
            start(skip_probe=self.skip_probe)
        self._client = client

    def close(self) -> None:
        """Stop the language server if this provider started one."""

        if self._client is None:
            return
        shutdown = getattr(self._client, "shutdown", None)
        if callable(shutdown):
            shutdown()
        self._client = None

    def __call__(self, capability: str, arguments: Mapping[str, Any]) -> Any:
        """Serve one graph-facing LSP request for provider comparison."""

        normalized = normalize_lsp_capability(capability)
        if normalized == CAPABILITY_DEFINITION:
            return self.definition(**dict(arguments))
        if normalized == CAPABILITY_REFERENCES:
            return self.references(**dict(arguments))
        return {"error": f"unsupported live LSP capability: {capability}"}

    def definition(
        self,
        *,
        file_path: Optional[str] = None,
        line: Optional[int] = None,
        character: Optional[int] = None,
        symbol: Optional[str] = None,
        top_k: int = 8,
    ) -> list[QueriedNode] | dict[str, str]:
        """Run ``textDocument/definition`` against the live language server."""

        del symbol  # Live LSP definition is position-based.
        if not file_path or line is None:
            return {"error": "live LSP definition requires file_path and line"}
        client = self._ensure_client()
        locations = client.definition(
            str(self._abs_path(file_path)),
            int(line),
            int(character or 0),
        )
        nodes = lsp_locations_to_nodes(
            locations,
            project_root=self.project_root,
            relation="json-rpc definition",
            node_type="definition",
        )
        return nodes[: max(1, int(top_k or 8))]

    def references(
        self,
        *,
        file_path: Optional[str] = None,
        line: Optional[int] = None,
        character: Optional[int] = None,
        symbol: Optional[str] = None,
        include_declaration: bool = True,
        top_k: int = 40,
    ) -> list[QueriedNode] | dict[str, str]:
        """Run ``textDocument/references`` against the live language server."""

        del symbol  # Live LSP references is position-based.
        if not file_path or line is None:
            return {"error": "live LSP references requires file_path and line"}
        client = self._ensure_client()
        locations = client.references(
            str(self._abs_path(file_path)),
            int(line),
            int(character or 0),
            include_declaration=bool(include_declaration),
        )
        nodes = lsp_locations_to_nodes(
            locations,
            project_root=self.project_root,
            relation="json-rpc reference",
            node_type="reference",
        )
        return nodes[: max(1, int(top_k or 40))]

    def _ensure_client(self) -> Any:
        self.start()
        return self._client

    def _abs_path(self, file_path: str) -> Path:
        path = Path(file_path)
        if path.is_absolute():
            return path
        return self.project_root / path


def lsp_locations_to_nodes(
    locations: Any,
    *,
    project_root: str | Path,
    relation: str,
    node_type: str,
) -> list[QueriedNode]:
    """Normalize LSP ``Location``/``LocationLink`` objects to compact nodes."""

    if locations is None:
        return []
    raw_locations = [locations] if isinstance(locations, Mapping) else list(locations)
    root = Path(project_root).resolve()
    nodes: list[QueriedNode] = []
    for index, location in enumerate(raw_locations, 1):
        if not isinstance(location, Mapping):
            continue
        uri = str(location.get("targetUri") or location.get("uri") or "")
        range_data = (
            location.get("targetSelectionRange")
            or location.get("range")
            or location.get("targetRange")
            or {}
        )
        rel_path = _uri_to_relpath(uri, root)
        if rel_path is None:
            continue
        start_line = _line(range_data, "start")
        end_line = _line(range_data, "end", default=start_line)
        display_line = start_line + 1 if start_line is not None else "?"
        node_name = f"{rel_path}:{display_line}"
        nodes.append(
            QueriedNode(
                node_name=node_name,
                type=node_type,
                file=rel_path,
                node_id=f"{rel_path}:{display_line}:{node_type}:{index}",
                start_line=start_line,
                end_line=end_line,
                score=1.0,
                content=relation,
            )
        )
    return nodes


def compare_static_to_live_lsp_provider(
    requests: Iterable[Any],
    *,
    graph: Any,
    project_root: str | Path,
    language: str,
    command: Optional[Sequence[str]] = None,
    client_factory: ClientFactory = LSPClient,
    skip_probe: bool = False,
    fingerprint_fn: Optional[Callable[[Sequence[Any]], str]] = None,
) -> list[Any]:
    """Compare static graph LSP results with a live JSON-RPC LSP server."""

    from .lsp_provider_validation import (  # local import avoids a cycle
        compare_static_lsp_provider,
        fingerprint_lsp_start_locations,
    )

    with LiveLSPReferenceProvider(
        project_root=project_root,
        language=language,
        command=command,
        client_factory=client_factory,
        skip_probe=skip_probe,
    ) as live_provider:
        return compare_static_lsp_provider(
            requests,
            graph=graph,
            reference_provider=live_provider,
            reference_provider_name=JSON_RPC_LSP_PROVIDER,
            fingerprint_fn=fingerprint_fn or fingerprint_lsp_start_locations,
        )


def _uri_to_relpath(uri: str, project_root: Path) -> Optional[str]:
    if not uri:
        return None
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    path = Path(unquote(parsed.path)).resolve()
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def _line(
    range_data: Mapping[str, Any],
    key: str,
    *,
    default: Optional[int] = None,
) -> Optional[int]:
    position = range_data.get(key) if isinstance(range_data, Mapping) else None
    if not isinstance(position, Mapping):
        return default
    value = position.get("line")
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "LiveLSPReferenceProvider",
    "compare_static_to_live_lsp_provider",
    "lsp_locations_to_nodes",
]
