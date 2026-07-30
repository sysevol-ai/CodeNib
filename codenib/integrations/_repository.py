# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only adapter over manifest-backed repository views.

External agent integrations need different names and output formats, but they
must not acquire their own repository state.  This module centralizes the
small shared boundary: safe source reads, deterministic symbol resolution,
containment queries, and bounded graph traversal over one ``ServerContext``.
"""

from __future__ import annotations

import fnmatch
import math
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Mapping

from ..types import (
    EDGE_TYPE_CONTAIN,
    NODE_TYPE_CLASS,
    NODE_TYPE_DIRECTORY,
    NODE_TYPE_FILE,
    NODE_TYPE_METHOD,
    is_symbol_node,
)

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_EMPTY_QUALIFIED_NAMES = frozenset({"", ".", "/"})


class IntegrationCapabilityError(RuntimeError):
    """A required manifest view is missing, stale, or failed to load."""


class RepositoryPathError(ValueError):
    """A requested path is invalid or escapes the manifest repository."""


@dataclass(frozen=True, slots=True)
class RepositoryEntity:
    """A stable adapter-local view of one graph vertex.

    Lines remain 0-based internally.  Integrations convert them exactly once
    while formatting results for their external agent contract.
    """

    vertex_id: int
    canonical_name: str
    display_name: str
    kind: str
    file_path: str
    qualified_name: str
    simple_name: str
    start_line: int | None
    end_line: int | None
    parent_name: str | None = None
    class_name: str | None = None

    @property
    def line_count(self) -> int | None:
        if self.start_line is None or self.end_line is None:
            return None
        return self.end_line - self.start_line + 1

    @property
    def locagent_id(self) -> str:
        if self.kind in {NODE_TYPE_FILE, NODE_TYPE_DIRECTORY}:
            return self.file_path or self.canonical_name
        if self.file_path and self.qualified_name:
            return f"{self.file_path}:{self.qualified_name}"
        return self.display_name or self.canonical_name

    @property
    def orcaloca_id(self) -> str:
        if self.kind in {NODE_TYPE_FILE, NODE_TYPE_DIRECTORY}:
            return self.file_path or self.canonical_name
        parts = [self.file_path]
        if self.class_name:
            parts.append(self.class_name)
        parts.append(self.simple_name or self.qualified_name)
        return "::".join(part for part in parts if part)


@dataclass(frozen=True, slots=True)
class RepositoryRelation:
    """One typed, directed graph relation."""

    source: RepositoryEntity
    target: RepositoryEntity
    edge_type: str


def _valid_line(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _strip_call_suffix(value: str) -> str:
    """Normalize display-only ``()`` markers without changing identities."""

    return re.sub(r"\(\)(?=\.|$)", "", value.strip())


def _simple_name(value: str) -> str:
    normalized = _strip_call_suffix(value)
    return normalized.replace("::", ".").rsplit(".", 1)[-1]


def _looks_like_file_prefix(value: str) -> bool:
    return "/" in value or "\\" in value or "." in value.rsplit("/", 1)[-1]


class RepositoryAdapter:
    """Read-only repository facts backed by one ``ServerContext``."""

    def __init__(self, context: Any, *, require_graph: bool = True) -> None:
        self.context = context
        manifest = getattr(context, "manifest", None)
        if manifest is None:
            raise IntegrationCapabilityError("ServerContext has no manifest")

        raw_root = str(getattr(manifest, "repo_path", "") or "").strip()
        if not raw_root:
            raise IntegrationCapabilityError("manifest has no repository path")
        self.repo_root = Path(raw_root).expanduser().resolve()
        if not self.repo_root.is_dir():
            raise IntegrationCapabilityError(
                f"manifest repository is not available: {self.repo_root}"
            )

        self.graph = getattr(context, "symbol_graph", None)
        if require_graph and self.graph is None:
            self.require_view("symbol_graph", "symbol_graph")

        self._entities: tuple[RepositoryEntity, ...] = ()
        self._entity_by_vertex: dict[int, RepositoryEntity] = {}
        self._entity_by_canonical: dict[str, RepositoryEntity] = {}
        self._aliases: dict[str, list[RepositoryEntity]] = {}
        self._aliases_lower: dict[str, list[RepositoryEntity]] = {}
        self._files: tuple[str, ...] = ()
        if self.graph is not None:
            self._build_entity_index()

    @classmethod
    def from_manifest(
        cls, manifest_path: str | Path, *, require_graph: bool = True
    ) -> "RepositoryAdapter":
        """Load one ``ServerContext`` and bind an adapter to it."""

        from ..mcp.context import ServerContext

        return cls(ServerContext.load(manifest_path), require_graph=require_graph)

    @property
    def entities(self) -> tuple[RepositoryEntity, ...]:
        return self._entities

    @property
    def symbols(self) -> tuple[RepositoryEntity, ...]:
        return tuple(entity for entity in self._entities if is_symbol_node(entity.kind))

    @property
    def files(self) -> tuple[str, ...]:
        return self._files

    def require_view(self, index_name: str, attribute: str) -> Any:
        """Return a loaded view or raise with manifest-aware diagnostics."""

        view = getattr(self.context, attribute, None)
        if view is not None:
            return view

        manifest = self.context.manifest
        entry = getattr(manifest, "indexes", {}).get(index_name)
        errors = getattr(self.context, "errors", {})
        if index_name in errors:
            detail = f"failed to load: {errors[index_name]}"
        elif entry is None:
            detail = "no manifest entry"
        elif not manifest.index_is_current(index_name):
            detail = f"manifest status is {entry.status!r} or source identity is stale"
        else:
            detail = "manifest entry did not produce a runtime view"
        raise IntegrationCapabilityError(
            f"required {index_name!r} view is unavailable ({detail})"
        )

    # ------------------------------------------------------------------
    # Source paths and reads
    # ------------------------------------------------------------------

    def normalize_path(self, value: str) -> str:
        """Return a safe repository-relative POSIX path."""

        raw = str(value or "").strip().replace("\\", "/")
        while raw.startswith("./"):
            raw = raw[2:]
        if not raw or raw in {".", "/"}:
            return "."
        if raw.startswith("/") or _WINDOWS_DRIVE.match(raw):
            raise RepositoryPathError(
                f"absolute repository path is not allowed: {value}"
            )

        pure = PurePosixPath(raw)
        if ".." in pure.parts:
            raise RepositoryPathError(
                f"repository path traversal is not allowed: {value}"
            )

        normalized = pure.as_posix()
        candidate = (self.repo_root / normalized).resolve()
        if not candidate.is_relative_to(self.repo_root):
            raise RepositoryPathError(f"repository path escapes the root: {value}")
        return normalized

    def source_path(self, value: str, *, require_file: bool = True) -> Path:
        normalized = self.normalize_path(value)
        if normalized == ".":
            path = self.repo_root
        else:
            path = (self.repo_root / normalized).resolve()
        if not path.is_relative_to(self.repo_root):
            raise RepositoryPathError(f"repository path escapes the root: {value}")
        if require_file and not path.is_file():
            raise RepositoryPathError(f"repository file does not exist: {normalized}")
        return path

    def source_lines(self, file_path: str) -> list[str]:
        path = self.source_path(file_path)
        return path.read_text(encoding="utf-8", errors="replace").splitlines()

    def read_range(
        self,
        file_path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> tuple[str, int, int]:
        """Read an inclusive 0-based range and return its actual bounds."""

        lines = self.source_lines(file_path)
        if not lines:
            return "", 0, 0
        start = 0 if start_line is None else max(0, int(start_line))
        end = len(lines) - 1 if end_line is None else min(len(lines) - 1, int(end_line))
        if end < start:
            return "", start, start
        return "\n".join(lines[start : end + 1]), start, end

    def read_entity(self, entity: RepositoryEntity) -> tuple[str, int, int]:
        if not entity.file_path:
            raise RepositoryPathError(
                f"entity has no repository source path: {entity.locagent_id}"
            )
        if entity.kind == NODE_TYPE_FILE:
            return self.read_range(entity.file_path)
        if entity.start_line is None or entity.end_line is None:
            raise RepositoryPathError(
                f"entity has no source range: {entity.locagent_id}"
            )
        return self.read_range(
            entity.file_path,
            start_line=entity.start_line,
            end_line=entity.end_line,
        )

    @staticmethod
    def numbered_source(content: str, start_line: int = 0) -> str:
        lines = content.splitlines()
        if not lines:
            return "```\n```"
        last = start_line + len(lines)
        width = len(str(last))
        body = "\n".join(
            f"{str(start_line + offset + 1).rjust(width)} | {line}"
            for offset, line in enumerate(lines)
        )
        return f"```\n{body}\n```"

    # ------------------------------------------------------------------
    # File and entity resolution
    # ------------------------------------------------------------------

    def match_files(self, pattern: str | None) -> list[str]:
        if not pattern:
            return list(self._files)
        raw = str(pattern).strip().replace("\\", "/")
        if not any(char in raw for char in "*?[]"):
            try:
                normalized = self.normalize_path(raw)
            except RepositoryPathError:
                return []
            return [normalized] if normalized in self._files else []

        patterns = [raw]
        if raw.startswith("**/"):
            patterns.append(raw[3:])
        matches = [
            file_path
            for file_path in self._files
            if any(
                fnmatch.fnmatchcase(file_path, candidate)
                or PurePosixPath(file_path).match(candidate)
                for candidate in patterns
            )
        ]
        return sorted(dict.fromkeys(matches))

    def find_files(self, query: str) -> list[str]:
        raw = str(query or "").strip().replace("\\", "/").strip("/")
        if not raw:
            return []
        try:
            normalized = self.normalize_path(raw)
        except RepositoryPathError:
            return []
        exact = [path for path in self._files if path == normalized]
        if exact:
            return exact
        return sorted(
            path
            for path in self._files
            if PurePosixPath(path).name == PurePosixPath(normalized).name
            or path.endswith("/" + normalized)
        )

    def entity_for_file(self, file_path: str) -> RepositoryEntity | None:
        candidates = self.find_entities(file_path, kinds={NODE_TYPE_FILE})
        return candidates[0] if len(candidates) == 1 else None

    def find_entities(
        self,
        query: str,
        *,
        file_path: str | None = None,
        kinds: Iterable[str] | None = None,
        class_name: str | None = None,
        fuzzy: bool = False,
    ) -> list[RepositoryEntity]:
        raw = str(query or "").strip().strip("`'\"").strip()
        if not raw:
            return []

        requested_file = file_path
        query_key = self._normalize_alias(raw)
        candidates = list(self._aliases.get(query_key, ()))

        if not candidates and "::" in raw:
            parts = [part for part in raw.split("::") if part]
            if parts and self.find_files(parts[0]):
                requested_file = requested_file or parts[0]
                query_key = self._normalize_alias(".".join(parts[1:]))
                candidates = list(self._aliases.get(query_key, ()))

        if not candidates and ":" in raw:
            prefix, suffix = raw.split(":", 1)
            if _looks_like_file_prefix(prefix):
                requested_file = requested_file or prefix
                candidates = list(self._aliases.get(self._normalize_alias(suffix), ()))

        if not candidates and fuzzy:
            lowered = self._normalize_alias(raw).lower()
            for alias, values in self._aliases_lower.items():
                if lowered and lowered in alias:
                    candidates.extend(values)

        allowed_kinds = set(kinds or ())
        normalized_file: str | None = None
        if requested_file:
            files = self.find_files(requested_file)
            if len(files) == 1:
                normalized_file = files[0]
            else:
                normalized_file = str(requested_file).replace("\\", "/").strip("/")

        filtered: list[RepositoryEntity] = []
        seen: set[int] = set()
        for entity in candidates:
            if entity.vertex_id in seen:
                continue
            if allowed_kinds and entity.kind not in allowed_kinds:
                continue
            if normalized_file and entity.file_path != normalized_file:
                continue
            if class_name and (entity.class_name or "") != class_name:
                continue
            seen.add(entity.vertex_id)
            filtered.append(entity)
        return sorted(filtered, key=self._entity_sort_key)

    def containing_entity(self, file_path: str, line: int) -> RepositoryEntity | None:
        """Return the narrowest symbol containing a 0-based source line."""

        files = self.find_files(file_path)
        if len(files) != 1:
            return None
        matches = [
            entity
            for entity in self.symbols
            if entity.file_path == files[0]
            and entity.start_line is not None
            and entity.end_line is not None
            and entity.start_line <= line <= entity.end_line
        ]
        if not matches:
            return None
        return min(
            matches,
            key=lambda entity: (
                (entity.end_line or 0) - (entity.start_line or 0),
                -(entity.start_line or 0),
                entity.locagent_id,
            ),
        )

    def children(
        self,
        entity: RepositoryEntity,
        *,
        kinds: Iterable[str] | None = None,
    ) -> list[RepositoryEntity]:
        allowed = set(kinds or ())
        relations = self.adjacent(
            entity, direction="downstream", edge_types={EDGE_TYPE_CONTAIN}
        )
        children = [relation.target for relation in relations]
        if allowed:
            children = [child for child in children if child.kind in allowed]
        return sorted(children, key=self._entity_sort_key)

    # ------------------------------------------------------------------
    # Graph operations
    # ------------------------------------------------------------------

    def adjacent(
        self,
        entity: RepositoryEntity,
        *,
        direction: str,
        edge_types: Iterable[str] | None = None,
    ) -> list[RepositoryRelation]:
        if self.graph is None:
            self.require_view("symbol_graph", "symbol_graph")
        selected = set(edge_types or ())
        modes: tuple[str, ...]
        if direction == "downstream":
            modes = ("out",)
        elif direction == "upstream":
            modes = ("in",)
        elif direction == "both":
            modes = ("out", "in")
        else:
            raise ValueError(
                "direction must be one of 'downstream', 'upstream', or 'both'"
            )

        relations: list[RepositoryRelation] = []
        seen: set[tuple[int, int, str]] = set()
        for mode in modes:
            for edge_id in self.graph.graph.incident(entity.vertex_id, mode=mode):
                edge = self.graph.graph.es[edge_id]
                edge_type = str(edge.attributes().get("type") or "")
                if selected and edge_type not in selected:
                    continue
                source = self._entity_by_vertex.get(edge.source)
                target = self._entity_by_vertex.get(edge.target)
                if source is None or target is None:
                    continue
                key = (source.vertex_id, target.vertex_id, edge_type)
                if key in seen:
                    continue
                seen.add(key)
                relations.append(
                    RepositoryRelation(
                        source=source,
                        target=target,
                        edge_type=edge_type,
                    )
                )
        return sorted(
            relations,
            key=lambda relation: (
                relation.edge_type,
                relation.source.locagent_id,
                relation.target.locagent_id,
            ),
        )

    def traverse(
        self,
        start: RepositoryEntity,
        *,
        direction: str = "downstream",
        depth: int = 2,
        edge_types: Iterable[str] | None = None,
        entity_kinds: Iterable[str] | None = None,
        max_nodes: int = 80,
    ) -> list[tuple[int, RepositoryRelation, RepositoryEntity]]:
        if depth < -1:
            raise ValueError("depth must be -1 or a non-negative integer")
        depth_limit = 20 if depth == -1 else depth
        if depth_limit == 0 or max_nodes <= 1:
            return []

        allowed_kinds = set(entity_kinds or ())
        queue: deque[tuple[RepositoryEntity, int]] = deque([(start, 0)])
        expanded = {start.vertex_id}
        output: list[tuple[int, RepositoryRelation, RepositoryEntity]] = []

        while queue and len(expanded) < max_nodes:
            current, level = queue.popleft()
            if level >= depth_limit:
                continue
            for relation in self.adjacent(
                current, direction=direction, edge_types=edge_types
            ):
                neighbor = (
                    relation.target
                    if relation.source.vertex_id == current.vertex_id
                    else relation.source
                )
                if allowed_kinds and neighbor.kind not in allowed_kinds:
                    continue
                if neighbor.vertex_id in expanded:
                    continue
                output.append((level + 1, relation, neighbor))
                expanded.add(neighbor.vertex_id)
                queue.append((neighbor, level + 1))
                if len(expanded) >= max_nodes:
                    break
        return output

    def distance(self, first: str, second: str) -> int:
        """Return an undirected graph hop distance, or ``-1`` if unresolved."""

        left = self.find_entities(first)
        right = self.find_entities(second)
        if len(left) != 1 or len(right) != 1:
            return -1
        distance = self.graph.graph.distances(
            [left[0].vertex_id], [right[0].vertex_id], mode="all"
        )[0][0]
        if isinstance(distance, float) and math.isinf(distance):
            return -1
        return int(distance)

    def file_tree(self, name: str | None = None, max_depth: int | None = None) -> str:
        """Render a deterministic tree from files represented in the graph."""

        prefix = str(name or "").strip().replace("\\", "/").strip("/")
        files = list(self._files)
        if prefix:
            exact_files = self.find_files(prefix)
            if exact_files:
                files = exact_files
                prefix = str(PurePosixPath(exact_files[0]).parent)
                if prefix == ".":
                    prefix = ""
            else:
                files = [
                    file_path
                    for file_path in files
                    if file_path == prefix or file_path.startswith(prefix + "/")
                ]
        if not files:
            return f"Cannot find the file tree rooted at {name}"

        root: dict[str, Any] = {}
        for file_path in files:
            parts = PurePosixPath(file_path).parts
            if prefix:
                prefix_parts = PurePosixPath(prefix).parts
                if tuple(parts[: len(prefix_parts)]) == prefix_parts:
                    parts = parts[len(prefix_parts) :]
            node = root
            for part in parts:
                node = node.setdefault(part, {})

        limit = (
            100 if max_depth is None or int(max_depth) == -1 else max(0, int(max_depth))
        )
        lines = [prefix or "/"]

        def render(tree: Mapping[str, Any], indent: str, level: int) -> None:
            if level >= limit:
                return
            items = sorted(tree.items(), key=lambda item: (bool(item[1]), item[0]))
            for index, (label, children) in enumerate(items):
                last = index == len(items) - 1
                lines.append(f"{indent}{'`-- ' if last else '|-- '}{label}")
                render(
                    children,
                    indent + ("    " if last else "|   "),
                    level + 1,
                )

        render(root, "", 0)
        return "\n".join(lines)

    def skeleton(self, entity: RepositoryEntity) -> str:
        """Render a language-neutral signature skeleton from graph ranges."""

        if entity.kind == NODE_TYPE_FILE:
            children = self.children(entity)
        else:
            children = self.children(entity)

        records = [entity] if entity.kind != NODE_TYPE_FILE else []
        records.extend(children)
        signatures: list[str] = []
        for record in records:
            if not record.file_path or record.start_line is None:
                continue
            content, _, _ = self.read_range(
                record.file_path, record.start_line, record.start_line
            )
            signature = content.strip()
            if not signature:
                signature = record.qualified_name or record.simple_name
            if record is not entity:
                signature = "    " + signature
            signatures.append(signature)
        return "\n".join(signatures)

    # ------------------------------------------------------------------
    # Index construction
    # ------------------------------------------------------------------

    def _build_entity_index(self) -> None:
        raw: dict[int, dict[str, Any]] = {}
        parent_by_vertex: dict[int, int] = {}
        graph = self.graph.graph

        for vertex_id in range(graph.vcount()):
            attrs = graph.vs[vertex_id].attributes()
            canonical = str(attrs.get("name") or "")
            kind = str(attrs.get("type") or "")
            file_path = self._graph_file_path(canonical, kind, attrs.get("file"))
            display = str(attrs.get("unified_name") or canonical)
            qualified = self._qualified_name(display, canonical, file_path, kind)
            raw[vertex_id] = {
                "canonical": canonical,
                "kind": kind,
                "file_path": file_path,
                "display": display,
                "qualified": qualified,
                "start_line": _valid_line(attrs.get("start_line")),
                "end_line": _valid_line(attrs.get("end_line")),
            }

        for edge in graph.es:
            if edge.attributes().get("type") == EDGE_TYPE_CONTAIN:
                parent_by_vertex.setdefault(edge.target, edge.source)

        entities: list[RepositoryEntity] = []
        for vertex_id, item in raw.items():
            parent_id = parent_by_vertex.get(vertex_id)
            parent = raw.get(parent_id) if parent_id is not None else None
            class_name: str | None = None
            if parent and parent["kind"] == NODE_TYPE_CLASS:
                class_name = _simple_name(parent["qualified"])
            elif item["kind"] == NODE_TYPE_METHOD:
                parts = _strip_call_suffix(item["qualified"]).split(".")
                if len(parts) > 1:
                    class_name = parts[-2]

            entity = RepositoryEntity(
                vertex_id=vertex_id,
                canonical_name=item["canonical"],
                display_name=item["display"],
                kind=item["kind"],
                file_path=item["file_path"],
                qualified_name=item["qualified"],
                simple_name=_simple_name(item["qualified"] or item["canonical"]),
                start_line=item["start_line"],
                end_line=item["end_line"],
                parent_name=parent["canonical"] if parent else None,
                class_name=class_name,
            )
            entities.append(entity)

        self._entities = tuple(sorted(entities, key=self._entity_sort_key))
        self._entity_by_vertex = {entity.vertex_id: entity for entity in self._entities}
        self._entity_by_canonical = {
            entity.canonical_name: entity for entity in self._entities
        }

        aliases: dict[str, list[RepositoryEntity]] = defaultdict(list)
        aliases_lower: dict[str, list[RepositoryEntity]] = defaultdict(list)
        files: set[str] = set()
        for entity in self._entities:
            if entity.file_path:
                files.add(entity.file_path)
            for alias in self._entity_aliases(entity):
                key = self._normalize_alias(alias)
                if not key:
                    continue
                aliases[key].append(entity)
                aliases_lower[key.lower()].append(entity)
        self._aliases = dict(aliases)
        self._aliases_lower = dict(aliases_lower)
        self._files = tuple(sorted(files))

    def _graph_file_path(self, canonical: str, kind: str, value: Any) -> str:
        raw = str(value or "")
        if kind == NODE_TYPE_FILE:
            raw = canonical
        if not raw and ":" in canonical:
            prefix = canonical.split(":", 1)[0]
            if _looks_like_file_prefix(prefix):
                raw = prefix
        raw = raw.replace("\\", "/").strip()
        if not raw:
            return ""
        path = Path(raw)
        if path.is_absolute():
            try:
                raw = path.resolve().relative_to(self.repo_root).as_posix()
            except ValueError:
                return ""
        while raw.startswith("./"):
            raw = raw[2:]
        if raw.startswith("/") or ".." in PurePosixPath(raw).parts:
            return ""
        return PurePosixPath(raw).as_posix()

    @staticmethod
    def _qualified_name(display: str, canonical: str, file_path: str, kind: str) -> str:
        if kind in {NODE_TYPE_FILE, NODE_TYPE_DIRECTORY}:
            return ""
        for candidate in (display, canonical):
            value = str(candidate or "").strip()
            if not value:
                continue
            if file_path and value.startswith(file_path + ":"):
                value = value[len(file_path) + 1 :]
            elif ":" in value:
                prefix, suffix = value.split(":", 1)
                if _looks_like_file_prefix(prefix):
                    value = suffix
            value = _strip_call_suffix(value).strip(".:")
            if value not in _EMPTY_QUALIFIED_NAMES:
                return value
        return ""

    @staticmethod
    def _normalize_alias(value: str) -> str:
        normalized = str(value or "").strip().strip("`'\"")
        normalized = normalized.replace("\\", "/").strip()
        return _strip_call_suffix(normalized).rstrip(".")

    @staticmethod
    def _entity_sort_key(entity: RepositoryEntity) -> tuple[Any, ...]:
        return (
            entity.file_path,
            entity.start_line if entity.start_line is not None else 1 << 30,
            entity.end_line if entity.end_line is not None else 1 << 30,
            entity.locagent_id,
        )

    @staticmethod
    def _entity_aliases(entity: RepositoryEntity) -> Iterator[str]:
        yield entity.canonical_name
        yield entity.display_name
        yield entity.locagent_id
        yield entity.orcaloca_id
        yield entity.qualified_name
        yield entity.simple_name
        if entity.file_path:
            yield entity.file_path
        if entity.file_path and entity.qualified_name:
            yield f"{entity.file_path}::{entity.qualified_name.replace('.', '::')}"
            yield f"{entity.file_path}::{entity.simple_name}"


__all__ = [
    "IntegrationCapabilityError",
    "RepositoryAdapter",
    "RepositoryEntity",
    "RepositoryPathError",
    "RepositoryRelation",
]
