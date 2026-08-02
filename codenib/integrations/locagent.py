# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""LocAgent/OpenHands-compatible tools over CodeNib repository views."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_IMPORT,
    EDGE_TYPE_REFERENCE,
    EDGE_TYPE_TYPE_USE,
    NODE_TYPE_CLASS,
    NODE_TYPE_DIRECTORY,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NODE_TYPE_SYMBOL,
)
from ._repository import RepositoryAdapter, RepositoryEntity, RepositoryPathError

LOCAGENT_REVISION = "ef170542a5cca88a1bd8463335ec43de222ed5f9"
OPENHANDS_LOCAGENT_REVISION = "efe287ce3402706a171b3a5fb40f15914e98ef20"

_TOOL_NAMES = (
    "search_code_snippets",
    "get_entity_contents",
    "explore_tree_structure",
)
_RUNTIME_VIEWS = frozenset({"symbol_graph", "bm25"})

_RELATION_TO_EDGE = {
    "contains": EDGE_TYPE_CONTAIN,
    "imports": EDGE_TYPE_IMPORT,
    "invokes": EDGE_TYPE_REFERENCE,
    "inherits": EDGE_TYPE_TYPE_USE,
}
_EDGE_TO_RELATION = {value: key for key, value in _RELATION_TO_EDGE.items()}
_BROADER_RELATIONS = {
    "invokes": "CodeNib reference edges are a conservative superset of calls",
    "inherits": "CodeNib type-use edges are a conservative superset of inheritance",
}
_ENTITY_TYPES = {
    "directory": {NODE_TYPE_DIRECTORY},
    "file": {NODE_TYPE_FILE},
    "class": {NODE_TYPE_CLASS},
    "function": {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD, NODE_TYPE_SYMBOL},
}

_SEARCH_DESCRIPTION = """Search repository code by entity name, keyword, file
pattern, or 1-based line number. Entity names use
`file_path:QualifiedName`; files use repository-relative paths."""

_ENTITY_DESCRIPTION = """Return source for repository entities. Functions and
classes use `file_path:QualifiedName`; files use repository-relative paths."""

_TREE_DESCRIPTION = """Traverse a pre-built repository graph upstream,
downstream, or in both directions. Traversal can be filtered by entity and
dependency types."""

_LOCAGENT_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": "search_code_snippets",
            "description": _SEARCH_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "search_terms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names, keywords, or code fragments to search.",
                    },
                    "line_nums": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "1-based source lines in one concrete file.",
                    },
                    "file_path_or_pattern": {
                        "type": "string",
                        "description": "Repository-relative file path or glob.",
                        "default": "**/*.py",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_entity_contents",
            "description": _ENTITY_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Repository-relative file or entity identifiers.",
                    }
                },
                "required": ["entity_names"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explore_tree_structure",
            "description": _TREE_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "start_entities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Repository entities from which traversal starts.",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["upstream", "downstream", "both"],
                        "default": "downstream",
                    },
                    "traversal_depth": {
                        "type": "integer",
                        "default": 2,
                    },
                    "entity_type_filter": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "default": None,
                    },
                    "dependency_type_filter": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "default": None,
                    },
                },
                "required": ["start_entities"],
            },
        },
    },
)


def get_locagent_tool_schemas() -> list[dict[str, Any]]:
    """Return dependency-free OpenAI schemas for the pinned LocAgent tools."""

    return copy.deepcopy(list(_LOCAGENT_TOOL_SCHEMAS))


class LocAgentToolProvider:
    """Serve LocAgent's three repository tools from one ``ServerContext``."""

    def __init__(
        self,
        context: Any,
        *,
        max_results: int = 5,
        context_lines: int = 20,
        max_source_lines: int = 220,
        max_output_chars: int = 48_000,
        traversal_max_nodes: int = 80,
    ) -> None:
        self.repository = RepositoryAdapter(context, require_graph=True)
        self.max_results = max(1, int(max_results))
        self.context_lines = max(0, int(context_lines))
        self.max_source_lines = max(20, int(max_source_lines))
        self.max_output_chars = max(2_000, int(max_output_chars))
        self.traversal_max_nodes = max(2, int(traversal_max_nodes))

    @classmethod
    def from_manifest(
        cls, manifest_path: str | Path, **kwargs: Any
    ) -> "LocAgentToolProvider":
        from ..mcp.context import ServerContext

        return cls(ServerContext.load(manifest_path, views=_RUNTIME_VIEWS), **kwargs)

    @property
    def context(self) -> Any:
        return self.repository.context

    def bindings(self) -> dict[str, Callable[..., str]]:
        """Return functions suitable for an OpenHands-style runtime export."""

        return {
            "search_code_snippets": self.search_code_snippets,
            "get_entity_contents": self.get_entity_contents,
            "explore_tree_structure": self.explore_tree_structure,
        }

    def dispatch(
        self, name: str, arguments: Mapping[str, Any] | str | None = None
    ) -> str:
        """Execute a function call directly, without IPython string evaluation."""

        if isinstance(arguments, str):
            try:
                decoded = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "LocAgent tool arguments must be a valid JSON object"
                ) from exc
            if not isinstance(decoded, Mapping):
                raise ValueError("LocAgent tool arguments must decode to a JSON object")
            parsed = dict(decoded)
        else:
            parsed = dict(arguments or {})
        function = self.bindings().get(name)
        if function is None:
            supported = ", ".join(_TOOL_NAMES)
            raise KeyError(f"unknown LocAgent tool {name!r}; supported: {supported}")
        return function(**parsed)

    def search_code_snippets(
        self,
        search_terms: Sequence[str] | None = None,
        line_nums: Sequence[int] | int | None = None,
        file_path_or_pattern: str | None = "**/*.py",
    ) -> str:
        """Search entities, indexed code, or 1-based lines."""

        terms = self._coerce_strings(search_terms)
        lines = [line_nums] if isinstance(line_nums, int) else list(line_nums or ())
        if not terms and not lines:
            return (
                "No search requested. Provide at least one search term or 1-based "
                "line number."
            )

        matched_files = self.repository.match_files(file_path_or_pattern)
        prefix = ""
        if file_path_or_pattern and not matched_files:
            matched_files = list(self.repository.files)
            prefix = (
                f"No files matched pattern {file_path_or_pattern!r}; "
                "searching all indexed files.\n\n"
            )
        file_filter = set(matched_files)

        sections: list[str] = []
        for term in terms:
            sections.append(self._search_term(term, file_filter))
        if lines:
            sections.append(
                self._search_lines(lines, file_path_or_pattern, matched_files)
            )
        return self._bounded(
            prefix + "\n\n".join(section for section in sections if section)
        )

    def get_entity_contents(self, entity_names: Sequence[str]) -> str:
        """Return deterministic source for exact or disambiguated entities."""

        sections: list[str] = []
        for raw_name in self._coerce_strings(entity_names):
            heading = (
                f"## Searching for entity `{raw_name}`...\n" "### Search Result:\n"
            )
            files = self.repository.find_files(raw_name)
            entities = self.repository.find_entities(raw_name)
            if len(files) == 1:
                file_entity = self.repository.entity_for_file(files[0])
                if file_entity is not None:
                    sections.append(
                        heading + self._format_entity(file_entity, complete=True)
                    )
                    continue
            if len(entities) == 1:
                sections.append(
                    heading + self._format_entity(entities[0], complete=True)
                )
            elif len(entities) > 1:
                sections.append(
                    heading + self._format_disambiguation(raw_name, entities)
                )
            else:
                sections.append(
                    heading
                    + "Invalid name.\n"
                    + 'Hint: use "file_path:QualifiedName" or a repository-relative file.'
                )
        if not sections:
            return "No entity names were provided."
        return self._bounded("\n\n".join(sections))

    def explore_tree_structure(
        self,
        start_entities: Sequence[str],
        direction: str = "downstream",
        traversal_depth: int = 2,
        entity_type_filter: Sequence[str] | None = None,
        dependency_type_filter: Sequence[str] | None = None,
    ) -> str:
        """Traverse CodeNib graph facts under LocAgent relation names."""

        if direction not in {"upstream", "downstream", "both"}:
            raise ValueError(
                "direction must be one of 'upstream', 'downstream', or 'both'"
            )
        depth = int(traversal_depth)
        if depth < -1:
            raise ValueError("traversal_depth must be -1 or non-negative")

        entity_kinds: set[str] = set()
        for entity_type in entity_type_filter or ():
            if entity_type not in _ENTITY_TYPES:
                supported = ", ".join(sorted(_ENTITY_TYPES))
                raise ValueError(
                    f"unknown entity type {entity_type!r}; supported: {supported}"
                )
            entity_kinds.update(_ENTITY_TYPES[entity_type])

        requested_relations = list(dependency_type_filter or _RELATION_TO_EDGE)
        invalid_relations = sorted(set(requested_relations) - set(_RELATION_TO_EDGE))
        if invalid_relations:
            supported = ", ".join(sorted(_RELATION_TO_EDGE))
            raise ValueError(
                f"unknown dependency types {invalid_relations}; supported: {supported}"
            )
        edge_types = {_RELATION_TO_EDGE[name] for name in requested_relations}

        sections: list[str] = []
        broader_used: set[str] = set()
        for raw_start in self._coerce_strings(start_entities):
            if raw_start == "/":
                sections.append(self.repository.file_tree(max_depth=depth))
                continue
            candidates = self.repository.find_entities(raw_start)
            if len(candidates) != 1:
                if candidates:
                    sections.append(self._format_disambiguation(raw_start, candidates))
                else:
                    sections.append(f"Invalid start entity `{raw_start}`.")
                continue
            start = candidates[0]
            rows = self.repository.traverse(
                start,
                direction=direction,
                depth=depth,
                edge_types=edge_types,
                entity_kinds=entity_kinds,
                max_nodes=self.traversal_max_nodes,
            )
            rendered = [f"{start.locagent_id} [{self._external_kind(start.kind)}]"]
            for level, relation, neighbor in rows:
                relation_name = _EDGE_TO_RELATION.get(
                    relation.edge_type, relation.edge_type
                )
                if relation_name in _BROADER_RELATIONS:
                    broader_used.add(relation_name)
                if relation.source.vertex_id == neighbor.vertex_id:
                    relation_text = f"<- {relation_name} -"
                else:
                    relation_text = f"{relation_name} ->"
                rendered.append(
                    f"{'  ' * level}- {relation_text} "
                    f"{neighbor.locagent_id} [{self._external_kind(neighbor.kind)}]"
                )
            if not rows:
                rendered.append("  (no matching relations)")
            sections.append("\n".join(rendered))

        if broader_used:
            notes = "; ".join(
                f"{name}: {_BROADER_RELATIONS[name]}" for name in sorted(broader_used)
            )
            sections.append(f"Relation fidelity: {notes}.")
        if not sections:
            return "No start entities were provided."
        return self._bounded("\n\n".join(sections))

    def explore_graph_structure(
        self,
        start_entities: Sequence[str],
        direction: str = "downstream",
        traversal_depth: int = 1,
        entity_type_filter: Sequence[str] | None = None,
        dependency_type_filter: Sequence[str] | None = None,
    ) -> str:
        """Compatibility wrapper for LocAgent's non-function-calling path."""

        return self.explore_tree_structure(
            start_entities=start_entities,
            direction=direction,
            traversal_depth=traversal_depth,
            entity_type_filter=entity_type_filter,
            dependency_type_filter=dependency_type_filter,
        )

    # ------------------------------------------------------------------
    # Formatting and search helpers
    # ------------------------------------------------------------------

    def _search_term(self, term: str, file_filter: set[str]) -> str:
        heading = f"## Searching for term {term!r}...\n### Search Result:\n"
        files = [
            path for path in self.repository.find_files(term) if path in file_filter
        ]
        if len(files) == 1:
            entity = self.repository.entity_for_file(files[0])
            if entity:
                return heading + self._format_entity(entity, complete=False)

        candidates = [
            entity
            for entity in self.repository.find_entities(term)
            if not entity.file_path or entity.file_path in file_filter
        ]
        if candidates:
            rendered = [
                self._format_entity(entity, complete=len(candidates) == 1)
                for entity in candidates[: self.max_results]
            ]
            if len(candidates) > self.max_results:
                rendered.append(
                    f"... {len(candidates) - self.max_results} additional matches omitted."
                )
            return heading + "\n\n".join(rendered)

        bm25 = self.repository.require_view("bm25", "bm25")
        nodes = bm25.search(
            query=term,
            top_k=max(self.max_results * 4, self.max_results),
            return_code_content=True,
            wrap_with_ln=False,
            filter_test=False,
        )
        selected: list[RepositoryEntity] = []
        seen: set[int] = set()
        for node in nodes:
            node_file = str(getattr(node, "file", "") or "")
            if file_filter and node_file not in file_filter:
                continue
            entity_matches = self.repository.find_entities(
                str(getattr(node, "node_name", "") or ""),
                file_path=node_file or None,
            )
            if not entity_matches and node_file:
                line = getattr(node, "start_line", None)
                if isinstance(line, int) and not isinstance(line, bool):
                    containing = self.repository.containing_entity(node_file, line)
                    entity_matches = [containing] if containing else []
            for entity in entity_matches:
                if entity and entity.vertex_id not in seen:
                    seen.add(entity.vertex_id)
                    selected.append(entity)
                    break
            if len(selected) >= self.max_results:
                break
        if not selected:
            return heading + "No locations found."
        return heading + "\n\n".join(
            self._format_entity(entity, complete=False) for entity in selected
        )

    def _search_lines(
        self,
        line_nums: Sequence[int],
        file_path_or_pattern: str | None,
        matched_files: Sequence[str],
    ) -> str:
        if not file_path_or_pattern or any(
            char in file_path_or_pattern for char in "*?[]"
        ):
            return (
                "## Searching by line...\n### Search Result:\n"
                "Line lookup requires one concrete repository-relative file path."
            )
        files = self.repository.find_files(file_path_or_pattern)
        if len(files) != 1:
            return (
                "## Searching by line...\n### Search Result:\n"
                f"Cannot resolve one file from {file_path_or_pattern!r}."
            )
        file_path = files[0]
        if matched_files and file_path not in set(matched_files):
            return (
                "## Searching by line...\n### Search Result:\n"
                f"File {file_path!r} is outside the requested filter."
            )

        entities: list[RepositoryEntity] = []
        uncovered: list[int] = []
        seen: set[int] = set()
        for value in line_nums:
            if isinstance(value, bool) or int(value) < 1:
                raise ValueError("line numbers must be positive 1-based integers")
            line = int(value) - 1
            entity = self.repository.containing_entity(file_path, line)
            if entity is None:
                uncovered.append(line)
            elif entity.vertex_id not in seen:
                seen.add(entity.vertex_id)
                entities.append(entity)

        rendered = [
            "## Searching by line...\n### Search Result:",
            *[self._format_entity(entity, complete=True) for entity in entities],
        ]
        for line in self._merge_line_windows(uncovered):
            content, start, end = self.repository.read_range(
                file_path, line[0], line[1]
            )
            rendered.append(
                f"Found code snippet in file `{file_path}` (lines {start + 1}-{end + 1}).\n"
                + self.repository.numbered_source(content, start)
            )
        if len(rendered) == 1:
            rendered.append("No locations found.")
        return "\n\n".join(rendered)

    def _merge_line_windows(self, lines: Sequence[int]) -> list[tuple[int, int]]:
        intervals = sorted(
            (
                max(0, line - self.context_lines),
                line + self.context_lines,
            )
            for line in lines
        )
        merged: list[list[int]] = []
        for start, end in intervals:
            if not merged or start > merged[-1][1] + 1:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return [(start, end) for start, end in merged]

    def _format_entity(self, entity: RepositoryEntity, *, complete: bool) -> str:
        try:
            content, start, end = self.repository.read_entity(entity)
        except RepositoryPathError as exc:
            return (
                f"Found {self._external_kind(entity.kind)} "
                f"`{entity.locagent_id}`, but source is unavailable: {exc}."
            )
        line_count = end - start + 1
        kind = self._external_kind(entity.kind)
        header = (
            f"Found {kind} `{entity.locagent_id}` " f"(lines {start + 1}-{end + 1})."
        )
        if line_count > self.max_source_lines and not complete:
            skeleton = self.repository.skeleton(entity)
            return (
                f"{header}\n"
                f"Showing a graph-derived structure because the source has "
                f"{line_count} lines:\n```\n{skeleton}\n```\n"
                f"Hint: request `{entity.locagent_id}` directly for full source."
            )
        return f"{header}\n{self.repository.numbered_source(content, start)}"

    @staticmethod
    def _format_disambiguation(
        query: str, candidates: Sequence[RepositoryEntity]
    ) -> str:
        lines = [f"Multiple entities match `{query}`:"]
        for index, entity in enumerate(candidates, start=1):
            location = ""
            if entity.start_line is not None:
                location = f" at line {entity.start_line + 1}"
            lines.append(f"{index}. `{entity.locagent_id}`{location}")
        return "\n".join(lines)

    @staticmethod
    def _external_kind(kind: str) -> str:
        if kind in {NODE_TYPE_METHOD, NODE_TYPE_SYMBOL}:
            return "function"
        return kind or "entity"

    @staticmethod
    def _coerce_strings(values: Sequence[str] | str | None) -> list[str]:
        if values is None:
            return []
        if isinstance(values, str):
            values = [values]
        normalized = [str(value).strip().rstrip(".") for value in values]
        return [value for value in normalized if value]

    def _bounded(self, value: str) -> str:
        if len(value) <= self.max_output_chars:
            return value.strip()
        marker = "\n\n[Output truncated by CodeNib's LocAgent adapter budget.]"
        prefix = value[: self.max_output_chars - len(marker)].rstrip()
        if prefix.count("```") % 2:
            prefix += "\n```"
        return prefix + marker


def bind_locagent_tools(
    provider: LocAgentToolProvider,
) -> dict[str, Callable[..., str]]:
    """Return the three functions expected by an OpenHands runtime plugin."""

    return provider.bindings()


def dispatch_locagent_tool_call(
    provider: LocAgentToolProvider,
    name: str,
    arguments: Mapping[str, Any] | str | None = None,
) -> str:
    """Dispatch a pinned LocAgent/OpenHands tool call without code evaluation."""

    return provider.dispatch(name, arguments)


__all__ = [
    "LOCAGENT_REVISION",
    "OPENHANDS_LOCAGENT_REVISION",
    "LocAgentToolProvider",
    "bind_locagent_tools",
    "dispatch_locagent_tool_call",
    "get_locagent_tool_schemas",
]
