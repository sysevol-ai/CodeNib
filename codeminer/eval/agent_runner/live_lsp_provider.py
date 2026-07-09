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
_NATIVE_JSON_RPC_CAPABILITIES = frozenset(
    {CAPABILITY_DEFINITION, CAPABILITY_REFERENCES}
)


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
        self.effective_command: Optional[list[str]] = None
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
        self.effective_command = list(command)
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
        _raise_on_client_failure(client, CAPABILITY_DEFINITION, locations)
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
        _raise_on_client_failure(client, CAPABILITY_REFERENCES, locations)
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


def _raise_on_client_failure(client: Any, capability: str, result: Any) -> None:
    """Keep transport failures distinct from valid empty LSP responses."""

    if result:
        return
    error = getattr(client, "_last_error", None)
    if not isinstance(error, Mapping):
        return
    code = error.get("code")
    if code in (None, -2):
        # LSP permits a null result when no location exists at the position.
        return
    message = str(error.get("message") or "unknown live LSP failure")
    raise RuntimeError(f"live LSP {capability} failed ({code}): {message}")


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
    fingerprint_selector: Optional[
        Callable[[Any], Callable[[Sequence[Any]], str]]
    ] = None,
) -> list[Any]:
    """Compare static graph LSP results with a live JSON-RPC LSP server."""

    from .lsp_provider_validation import (  # local import avoids a cycle
        compare_static_lsp_provider,
        default_lsp_provider_fingerprint,
        fingerprint_lsp_start_locations,
    )

    request_list = _validate_native_lsp_requests(requests)
    selected_fingerprint_selector = None
    if fingerprint_fn is None:
        selected_fingerprint_selector = (
            fingerprint_selector or default_lsp_provider_fingerprint
        )
    with LiveLSPReferenceProvider(
        project_root=project_root,
        language=language,
        command=command,
        client_factory=client_factory,
        skip_probe=skip_probe,
    ) as live_provider:
        return compare_static_lsp_provider(
            request_list,
            graph=graph,
            reference_provider=live_provider,
            reference_provider_name=JSON_RPC_LSP_PROVIDER,
            fingerprint_fn=fingerprint_fn or fingerprint_lsp_start_locations,
            fingerprint_selector=selected_fingerprint_selector,
        )


def _validate_native_lsp_requests(requests: Iterable[Any]) -> list[Any]:
    request_list = list(requests)
    unsupported: list[str] = []
    for index, request in enumerate(request_list, 1):
        capability = _request_capability(request)
        if capability not in _NATIVE_JSON_RPC_CAPABILITIES:
            request_id = _request_id(request) or f"#{index}"
            unsupported.append(f"{request_id}:{capability or '<missing>'}")
    if unsupported:
        allowed = ", ".join(sorted(_NATIVE_JSON_RPC_CAPABILITIES))
        raise ValueError(
            "live JSON-RPC LSP validation supports only native capabilities "
            f"({allowed}); unsupported requests: {', '.join(unsupported)}"
        )
    return request_list


def _request_capability(request: Any) -> str:
    if hasattr(request, "normalized_capability"):
        return str(request.normalized_capability)
    if isinstance(request, Mapping):
        return normalize_lsp_capability(
            str(request.get("capability") or request.get("lsp_method") or "")
        )
    return ""


def _request_id(request: Any) -> Optional[str]:
    if hasattr(request, "request_id"):
        value = request.request_id
        return str(value) if value is not None else None
    if isinstance(request, Mapping):
        value = request.get("request_id")
        return str(value) if value is not None else None
    return None


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
