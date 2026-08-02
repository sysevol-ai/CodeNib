# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""OrcaLoca search-manager compatibility over CodeNib repository views.

The upstream search policy is coupled to several private ``SearchManager``
helpers in addition to its six tools.  This provider implements that pinned
duck-typed surface while keeping OrcaLoca's policy, LlamaIndex wrappers, and
queue/scoring types outside CodeNib.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple, Sequence

from ..types import (
    DEPENDENCY_EDGE_TYPES,
    NODE_TYPE_CLASS,
    NODE_TYPE_FIELD,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NODE_TYPE_SYMBOL,
)
from ._orcaloca_python import PythonOutline, PythonSymbol, parse_python_outline
from ._repository import RepositoryAdapter, RepositoryEntity, RepositoryPathError

ORCALOCA_REVISION = "37db289be2dc3b7432183fe08b3f06becce87c27"

ORCALOCA_TOOL_NAMES = (
    "search_file_contents",
    "search_source_code",
    "search_class",
    "search_method_in_class",
    "search_callable",
    "search_file_tree",
)

ORCALOCA_MANAGER_HOOKS = (
    "_get_class_methods",
    "_get_disambiguous_classes",
    "_get_disambiguous_files",
    "_get_disambiguous_methods",
    "_get_exact_loc",
    "_get_file_functions",
    "check_and_add_query",
    "get_distance_between_queries",
    "get_frame_from_history",
    "get_node_existence",
    "get_query_from_history",
)

ORCALOCA_HISTORY_FIELDS = (
    "search_action",
    "search_input",
    "search_query",
    "search_content",
    "query_type",
    "file_path",
    "is_skeleton",
)

_CALLABLE_KINDS = {
    NODE_TYPE_CLASS,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NODE_TYPE_SYMBOL,
    NODE_TYPE_FIELD,
}
_METHOD_KINDS = {NODE_TYPE_METHOD, NODE_TYPE_FUNCTION, NODE_TYPE_SYMBOL}
_NO_PATH = 999_999


class OrcaLocation(NamedTuple):
    """Dependency-free equivalent of OrcaLoca's ``Loc`` named tuple."""

    file_name: str
    node_name: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class OrcaLocationInfo:
    """Dependency-free equivalent of OrcaLoca's ``LocInfo`` wrapper."""

    loc: OrcaLocation
    type: str


class OrcaLocaSearchProvider:
    """Duck-typed replacement for OrcaLoca's pinned ``SearchManager``."""

    def __init__(
        self,
        context: Any,
        *,
        file_skeleton_threshold: int = 200,
        class_skeleton_threshold: int = 100,
        max_output_chars: int = 48_000,
    ) -> None:
        self.repository = RepositoryAdapter(context, require_graph=True)
        self.repo_path = str(self.repository.repo_root)
        self.file_skeleton_threshold = max(1, int(file_skeleton_threshold))
        self.class_skeleton_threshold = max(1, int(class_skeleton_threshold))
        self.max_output_chars = max(2_000, int(max_output_chars))
        self.history: list[dict[str, Any]] = []
        self.history_query_set: set[str] = set()
        self._python_outline_cache: dict[str, PythonOutline | None] = {}

    @classmethod
    def from_manifest(
        cls, manifest_path: str | Path, **kwargs: Any
    ) -> "OrcaLocaSearchProvider":
        from ..mcp.context import ServerContext

        return cls(ServerContext.load(manifest_path), **kwargs)

    @property
    def context(self) -> Any:
        return self.repository.context

    # ------------------------------------------------------------------
    # Upstream manager state and policy hooks
    # ------------------------------------------------------------------

    def add_result_to_history(self, new_row: Mapping[str, Any]) -> None:
        row = {field: new_row.get(field) for field in ORCALOCA_HISTORY_FIELDS}
        row["is_skeleton"] = bool(row["is_skeleton"])
        self.history.append(row)

    def get_frame_from_history(self, action: str, input: str) -> dict[str, Any] | None:
        for row in self.history:
            if row["search_action"] == action and row["search_input"] == input:
                return dict(row)
        return None

    def get_query_from_history(self, action: str, input: str) -> str | None:
        row = self.get_frame_from_history(action, input)
        if row is None:
            return None
        query = row.get("search_query")
        return str(query) if query is not None else None

    def check_and_add_query(self, query: str) -> bool:
        if query in self.history_query_set:
            return True
        if self.get_node_existence(query):
            self.history_query_set.add(query)
        return False

    def get_node_existence(self, query: str | None) -> bool:
        if not query:
            return False
        if len(self.repository.find_files(query)) == 1:
            return True
        return (
            len(
                [
                    entity
                    for entity in self.repository.find_entities(query)
                    if self._is_orcaloca_entity(entity)
                ]
            )
            == 1
        )

    def get_distance_between_queries(
        self, query1: str | None, query2: str | None
    ) -> int:
        if query1 is None or query2 is None:
            return -1
        if not self.get_node_existence(query1) or not self.get_node_existence(query2):
            return _NO_PATH
        distance = self.repository.distance(query1, query2)
        return distance if distance >= 0 else _NO_PATH

    def _get_exact_loc(self, query: str) -> OrcaLocationInfo | None:
        files = self.repository.find_files(query)
        if len(files) == 1 and query.replace("\\", "/").strip("/") == files[0]:
            entity = self.repository.entity_for_file(files[0])
            return self._location_info(entity) if entity else None
        entities = self.repository.find_entities(query)
        source_backed = [
            entity for entity in entities if self._is_orcaloca_entity(entity)
        ]
        if len(source_backed) != 1:
            return None
        return self._location_info(source_backed[0])

    def _get_dependency(self, query: str) -> list[OrcaLocation] | None:
        entities = self.repository.find_entities(query)
        entities = [entity for entity in entities if self._is_orcaloca_entity(entity)]
        if len(entities) != 1:
            return None
        entity = entities[0]
        locations: list[OrcaLocation] = []
        seen: set[str] = set()
        for relation in self.repository.adjacent(
            entity,
            direction="downstream",
            edge_types=DEPENDENCY_EDGE_TYPES,
        ):
            target = relation.target
            if target.canonical_name in seen or not self._is_orcaloca_entity(target):
                continue
            seen.add(target.canonical_name)
            locations.append(self._location(target))
        parent = self.repository.parent(entity)
        if (
            parent is not None
            and parent.kind == NODE_TYPE_CLASS
            and parent.canonical_name not in seen
            and self._is_orcaloca_entity(parent)
        ):
            locations.append(self._location(parent))
        return locations

    def _get_query_in_file(self, file_path: str, query: str) -> OrcaLocationInfo | None:
        entities = self.repository.find_entities(query, file_path=file_path)
        source_backed = [
            entity for entity in entities if self._is_orcaloca_entity(entity)
        ]
        if len(source_backed) != 1:
            return None
        return self._location_info(source_backed[0])

    def _search_file_tree(
        self, name: str | None = None, max_depth: int | None = None
    ) -> str:
        return self.repository.file_tree(name=name, max_depth=max_depth)

    def _search_source_code(self, file_path: str, source_code: str) -> str:
        files = self.repository.find_files(file_path)
        if len(files) != 1:
            return f"Cannot find the context of {source_code} in {file_path}"
        normalized_query = self._normalize_code(source_code)
        if not normalized_query:
            return f"Cannot find the context of {source_code} in {files[0]}"

        candidates: list[tuple[int, RepositoryEntity, str]] = []
        for entity in self.repository.entities_in_file(files[0], kinds=_CALLABLE_KINDS):
            if not self._is_orcaloca_entity(entity):
                continue
            content = self._source(entity)
            if normalized_query in self._normalize_code(content):
                candidates.append((entity.line_count or _NO_PATH, entity, content))
        if not candidates:
            return f"Cannot find the context of {source_code} in {files[0]}"
        _, _, content = min(
            candidates,
            key=lambda item: (
                item[0],
                item[1].start_line or 0,
                item[1].orcaloca_id,
            ),
        )
        return content

    def _dfs_get_file_skeleton(self, file_name: str) -> tuple[OrcaLocation, str] | None:
        files = self.repository.find_files(file_name)
        if len(files) != 1:
            return None
        entity = self.repository.entity_for_file(files[0])
        if entity is None:
            return None
        return self._location(entity), self._file_skeleton(entity)

    def _direct_get_file_skeleton(self, file_node_name: str) -> str | None:
        files = self.repository.find_files(file_node_name)
        if len(files) != 1:
            return None
        entity = self.repository.entity_for_file(files[0])
        return self._file_skeleton(entity) if entity else None

    def _search_callable_kg(self, callable: str) -> OrcaLocationInfo | None:
        entities = self.repository.find_entities(callable, kinds=_CALLABLE_KINDS)
        source_backed = [
            entity for entity in entities if self._is_orcaloca_entity(entity)
        ]
        if len(source_backed) != 1:
            return None
        return self._location_info(source_backed[0])

    def _dfs_get_class(self, class_name: str) -> tuple[OrcaLocation, str] | None:
        classes = self.repository.find_entities(class_name, kinds={NODE_TYPE_CLASS})
        source_backed = [
            entity for entity in classes if self._is_orcaloca_entity(entity)
        ]
        if len(source_backed) != 1:
            return None
        entity = source_backed[0]
        return self._location(entity), self._class_skeleton(entity)

    def _direct_get_class(self, class_node_name: str) -> str | None:
        classes = self.repository.find_entities(
            class_node_name, kinds={NODE_TYPE_CLASS}
        )
        classes = [entity for entity in classes if self._is_orcaloca_entity(entity)]
        if len(classes) != 1:
            return None
        return self._class_skeleton(classes[0])

    def _get_class_methods(self, class_node_name: str) -> tuple[list[str], list[str]]:
        classes = self.repository.find_entities(
            class_node_name, kinds={NODE_TYPE_CLASS}
        )
        classes = [entity for entity in classes if self._is_orcaloca_entity(entity)]
        if len(classes) != 1:
            return [], []
        methods = [
            child
            for child in self.repository.children(classes[0])
            if child.kind in _METHOD_KINDS
            and self._is_orcaloca_entity(child)
            and self._query_type(child) == "method"
        ]
        return (
            [method.orcaloca_id for method in methods],
            [self._source(method) for method in methods],
        )

    def _get_file_functions(self, file_node_name: str) -> tuple[list[str], list[str]]:
        files = self.repository.find_files(file_node_name)
        if len(files) != 1:
            return [], []
        file_entity = self.repository.entity_for_file(files[0])
        if file_entity is None:
            return [], []
        functions = []
        for child in self.repository.children(file_entity):
            query_type = self._query_type(child)
            if query_type == "function" and self._is_orcaloca_entity(child):
                functions.append(child)
            elif (
                query_type == "class"
                and self._is_orcaloca_entity(child)
                and self._line_span(child) <= self.class_skeleton_threshold
            ):
                functions.append(child)
        return (
            [function.orcaloca_id for function in functions],
            [self._source(function) for function in functions],
        )

    def _get_disambiguous_classes(self, class_name: str) -> list[str]:
        return sorted(
            {
                entity.file_path
                for entity in self.repository.find_entities(
                    class_name, kinds={NODE_TYPE_CLASS}
                )
                if entity.file_path and self._is_orcaloca_entity(entity)
            }
        )

    def _get_disambiguous_files(self, file_name: str) -> list[str]:
        return self.repository.find_files(file_name)

    def _get_disambiguous_query_type(self, query: str) -> str | None:
        entities = self.repository.find_entities(query, kinds=_CALLABLE_KINDS)
        entities = [entity for entity in entities if self._is_orcaloca_entity(entity)]
        if not entities:
            return None
        return self._query_type(entities[0])

    def _get_disambiguous_methods(
        self,
        method_name: str,
        class_name: str | None = None,
        file_path: str | None = None,
    ) -> tuple[list[str], list[str]]:
        methods = self.repository.find_entities(
            method_name,
            file_path=file_path,
            kinds=_METHOD_KINDS,
            class_name=class_name,
        )
        methods = [
            method
            for method in methods
            if self._is_orcaloca_entity(method) and self._query_type(method) == "method"
        ]
        return (
            [method.orcaloca_id for method in methods],
            [self._source(method) for method in methods],
        )

    def _get_bug_location(
        self, query: str, file_path: str | None = None
    ) -> dict[str, list[dict[str, str]]]:
        entities = self.repository.find_entities(
            query, file_path=file_path, kinds=_CALLABLE_KINDS
        )
        locations = []
        for entity in entities:
            if not self._is_orcaloca_entity(entity):
                continue
            query_type = self._query_type(entity)
            locations.append(
                {
                    "file_path": entity.file_path,
                    "class_name": (
                        entity.simple_name
                        if query_type == "class"
                        else entity.class_name or ""
                    ),
                    "method_name": (
                        entity.simple_name
                        if query_type in {"method", "function"}
                        else ""
                    ),
                }
            )
        return {"bug_locations": locations}

    # ------------------------------------------------------------------
    # Six functions exposed to OrcaLoca's agent
    # ------------------------------------------------------------------

    def get_search_functions(self) -> list[Callable[..., str]]:
        return [
            self.search_file_contents,
            self.search_source_code,
            self.search_class,
            self.search_method_in_class,
            self.search_callable,
            self.search_file_tree,
        ]

    def search_file_tree(
        self, name: str | None = None, max_depth: int | None = None
    ) -> str:
        """Return the repository file tree, optionally rooted at a directory."""

        content = self._search_file_tree(name, max_depth)
        self._record(
            action="search_file_tree",
            search_input=name,
            query=name,
            content=content,
            query_type="file_tree",
        )
        return self._bounded(content)

    def search_file_contents(
        self, file_name: str, directory_path: str | None = None
    ) -> str:
        """Return one file's content or a graph-derived large-file skeleton."""

        search_input = (
            f"{directory_path.rstrip('/')}/{file_name}" if directory_path else file_name
        )
        files = self.repository.find_files(search_input)
        if not directory_path:
            files = self.repository.find_files(file_name)
        if not files:
            return f"Cannot find the file {file_name}"
        if len(files) > 1:
            content = self._disambiguation(
                f"Multiple matches found for file {file_name}.", files=files
            )
            self._record(
                action="search_file_contents",
                search_input=search_input,
                query=file_name,
                content=content,
                query_type="disambiguation",
            )
            return content

        file_path = files[0]
        entity = self.repository.entity_for_file(file_path)
        if entity is None:
            return f"Cannot find the file {file_name}"
        source = self._source(entity)
        line_count = len(source.splitlines())
        is_skeleton = max(0, line_count - 1) > self.file_skeleton_threshold
        content = self._file_skeleton(entity) if is_skeleton else source
        self._record(
            action="search_file_contents",
            search_input=search_input,
            query=entity.orcaloca_id,
            content=content,
            query_type="file",
            file_path=file_path,
            is_skeleton=is_skeleton,
        )
        marker = "File Skeleton" if is_skeleton else "File Content"
        return self._bounded(f"File Path: {file_path} \n{marker}: \n{content}")

    def search_source_code(self, file_path: str, source_code: str) -> str:
        """Find the narrowest graph symbol containing a source fragment."""

        content = self._search_source_code(file_path, source_code)
        self._record(
            action="search_source_code",
            search_input=source_code,
            query=source_code,
            content=content,
            query_type="source_code",
            file_path=file_path,
        )
        return self._bounded(f"File Path: {file_path} \nCode Snippet: \n{content}")

    def search_class(self, class_name: str, file_path: str | None = None) -> str:
        """Return one class or ask the agent to disambiguate its file."""

        search_input = f"{file_path}::{class_name}" if file_path else class_name
        classes = self.repository.find_entities(
            class_name, file_path=file_path, kinds={NODE_TYPE_CLASS}
        )
        classes = [entity for entity in classes if self._is_orcaloca_entity(entity)]
        if not classes:
            suffix = f" in {file_path}" if file_path else ""
            return f"Cannot find the class {class_name}{suffix}"
        if len(classes) > 1:
            content = self._disambiguation(
                f"Multiple matched classes found about class: {class_name}.",
                entities=classes,
            )
            self._record(
                action="search_class",
                search_input=search_input,
                query=class_name,
                content=content,
                query_type="disambiguation",
            )
            return content

        entity = classes[0]
        is_skeleton = self._line_span(entity) > self.class_skeleton_threshold
        content = self._class_skeleton(entity) if is_skeleton else self._source(entity)
        self._record(
            action="search_class",
            search_input=search_input,
            query=entity.orcaloca_id,
            content=content,
            query_type="class",
            file_path=entity.file_path,
            is_skeleton=is_skeleton,
        )
        marker = "Class Skeleton" if is_skeleton else "Class Content"
        return self._bounded(f"File Path: {entity.file_path} \n{marker}: \n{content}")

    def search_method_in_class(
        self,
        class_name: str,
        method_name: str,
        file_path: str | None = None,
    ) -> str:
        """Return a method constrained by class and optional file."""

        search_input = (
            f"{file_path}::{class_name}::{method_name}"
            if file_path
            else f"{class_name}::{method_name}"
        )
        methods = self.repository.find_entities(
            method_name,
            file_path=file_path,
            kinds=_METHOD_KINDS,
            class_name=class_name,
        )
        methods = [
            entity
            for entity in methods
            if self._is_orcaloca_entity(entity) and self._query_type(entity) == "method"
        ]
        if not methods:
            suffix = f" in {file_path}" if file_path else ""
            return f"Cannot find the method {method_name} in {class_name}{suffix}"
        if len(methods) > 1:
            content = self._disambiguation(
                f"Multiple matched methods found about method: {method_name} "
                f"in class: {class_name}.",
                entities=methods,
            )
            self._record(
                action="search_method_in_class",
                search_input=search_input,
                query=method_name,
                content=content,
                query_type="disambiguation",
            )
            return content

        entity = methods[0]
        content = self._source(entity)
        self._record(
            action="search_method_in_class",
            search_input=search_input,
            query=entity.orcaloca_id,
            content=content,
            query_type="method",
            file_path=entity.file_path,
        )
        return self._bounded(
            f"File Path: {entity.file_path} \nMethod Content: \n{content}"
        )

    def search_callable(self, query_name: str, file_path: str | None = None) -> str:
        """Return a class, function, method, or symbol definition."""

        search_input = f"{file_path}::{query_name}" if file_path else query_name
        entities = self.repository.find_entities(
            query_name, file_path=file_path, kinds=_CALLABLE_KINDS
        )
        entities = [entity for entity in entities if self._is_orcaloca_entity(entity)]
        if not entities:
            suffix = f" in {file_path}" if file_path else ""
            return f"Cannot find the definition of {query_name}{suffix}"
        if len(entities) > 1:
            content = self._disambiguation(
                f"Multiple matched callables found about query {query_name}.",
                entities=entities,
            )
            self._record(
                action="search_callable",
                search_input=search_input,
                query=query_name,
                content=content,
                query_type="disambiguation",
                file_path=file_path or "",
            )
            return content

        entity = entities[0]
        query_type = self._query_type(entity)
        if query_type == "class":
            content = self.search_class(query_name, file_path)
            is_skeleton = self._line_span(entity) > self.class_skeleton_threshold
            self._record(
                action="search_callable",
                search_input=search_input,
                query=entity.orcaloca_id,
                content=content,
                query_type=query_type,
                file_path=entity.file_path,
                is_skeleton=is_skeleton,
            )
            return content

        content = self._source(entity)
        self._record(
            action="search_callable",
            search_input=search_input,
            query=entity.orcaloca_id,
            content=content,
            query_type=query_type,
            file_path=entity.file_path,
            is_skeleton=False,
        )
        return self._bounded(
            f"File Path: {entity.file_path} \n"
            f"Query Type: {query_type} \nCode Snippet: \n{content}"
        )

    # ------------------------------------------------------------------
    # Adapter-local helpers
    # ------------------------------------------------------------------

    def _record(
        self,
        *,
        action: str,
        search_input: Any,
        query: Any,
        content: str,
        query_type: str,
        file_path: str = "",
        is_skeleton: bool = False,
    ) -> None:
        self.add_result_to_history(
            {
                "search_action": action,
                "search_input": search_input,
                "search_query": query,
                "search_content": content,
                "query_type": query_type,
                "file_path": file_path,
                "is_skeleton": is_skeleton,
            }
        )

    def _location_info(self, entity: RepositoryEntity) -> OrcaLocationInfo:
        return OrcaLocationInfo(
            loc=self._location(entity),
            type=self._query_type(entity),
        )

    def _location(self, entity: RepositoryEntity) -> OrcaLocation:
        if entity.kind == NODE_TYPE_FILE:
            line_count = max(1, len(self.repository.source_lines(entity.file_path)))
            start_line, end_line = 1, line_count
        elif symbol := self._python_symbol(entity):
            start_line, end_line = symbol.start_line, symbol.end_line
        elif entity.start_line is not None and entity.end_line is not None:
            start_line, end_line = entity.start_line + 1, entity.end_line + 1
        else:
            raise RepositoryPathError(
                f"entity has no source range: {entity.orcaloca_id}"
            )
        return OrcaLocation(
            file_name=entity.file_path,
            node_name=entity.orcaloca_id,
            start_line=start_line,
            end_line=end_line,
        )

    def _query_type(self, entity: RepositoryEntity) -> str:
        symbol = self._python_symbol(entity)
        if symbol is not None:
            return symbol.kind
        return self._graph_query_type(entity)

    @staticmethod
    def _graph_query_type(entity: RepositoryEntity) -> str:
        if entity.kind == NODE_TYPE_FILE:
            return "file"
        if entity.kind == NODE_TYPE_CLASS:
            return "class"
        if entity.kind == NODE_TYPE_METHOD or entity.class_name:
            return "method"
        return "function"

    @staticmethod
    def _has_source(entity: RepositoryEntity) -> bool:
        return bool(
            entity.file_path
            and (
                entity.kind == NODE_TYPE_FILE
                or (entity.start_line is not None and entity.end_line is not None)
            )
        )

    def _is_orcaloca_entity(self, entity: RepositoryEntity) -> bool:
        if not self._has_source(entity):
            return False
        outline = self._python_outline(entity.file_path)
        if outline is None or entity.kind == NODE_TYPE_FILE:
            return True
        return self._python_symbol(entity) is not None

    def _line_span(self, entity: RepositoryEntity) -> int:
        symbol = self._python_symbol(entity)
        if symbol is not None:
            return symbol.end_line - symbol.start_line
        if entity.start_line is None or entity.end_line is None:
            return _NO_PATH
        return entity.end_line - entity.start_line

    def _source(self, entity: RepositoryEntity) -> str:
        if entity.kind == NODE_TYPE_FILE:
            outline = self._python_outline(entity.file_path)
            if outline is not None:
                return outline.source
        symbol = self._python_symbol(entity)
        if symbol is not None:
            return symbol.source
        content, _, _ = self.repository.read_entity(entity)
        return content

    def _signature(self, entity: RepositoryEntity) -> str:
        symbol = self._python_symbol(entity)
        if symbol is not None:
            return symbol.signature or entity.simple_name
        if not self._has_source(entity):
            return entity.simple_name
        content, _, _ = self.repository.read_range(
            entity.file_path, entity.start_line, entity.start_line
        )
        return content.strip() or entity.simple_name

    def _class_skeleton(self, entity: RepositoryEntity) -> str:
        symbol = self._python_symbol(entity)
        outline = self._python_outline(entity.file_path)
        if symbol is not None and symbol.kind == "class" and outline is not None:
            snapshot = (
                f"Class Signature: {symbol.signature}\n"
                f"Docstring: {symbol.docstring}\n"
            )
            for method in outline.symbols:
                if method.parent_id != symbol.node_id or method.kind != "method":
                    continue
                snapshot += (
                    f"\nMethod: {method.name}\n"
                    f"Method Signature: {method.signature}\n"
                    f"Docstring: {method.docstring}\n"
                )
            return snapshot

        lines = [f"Class Signature: {self._signature(entity)}"]
        methods = [
            child
            for child in self.repository.children(entity)
            if child.kind in _METHOD_KINDS
        ]
        for method in methods:
            lines.extend(
                [
                    "",
                    f"Method: {method.simple_name}",
                    f"Method Signature: {self._signature(method)}",
                ]
            )
        return "\n".join(lines)

    def _file_skeleton(self, entity: RepositoryEntity) -> str:
        outline = self._python_outline(entity.file_path)
        if outline is not None:
            snapshot = ""
            for symbol in outline.symbols:
                if symbol.parent_id != entity.file_path:
                    continue
                snapshot += f"\n{symbol.kind.capitalize()}: {symbol.name}\n"
                if symbol.signature:
                    snapshot += f"Signature: {symbol.signature}\n"
                if symbol.docstring:
                    snapshot += f"Docstring: {symbol.docstring}\n"
            return snapshot

        lines: list[str] = []
        for child in self.repository.children(entity):
            query_type = self._query_type(child)
            if query_type not in {"class", "function"}:
                continue
            lines.extend(
                [
                    f"{query_type.capitalize()}: {child.simple_name}",
                    f"Signature: {self._signature(child)}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip()

    def _python_outline(self, file_path: str) -> PythonOutline | None:
        if Path(file_path).suffix.lower() != ".py":
            return None
        if file_path in self._python_outline_cache:
            return self._python_outline_cache[file_path]

        try:
            source = self.repository.source_path(file_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            self._python_outline_cache[file_path] = None
            return None

        outline = parse_python_outline(file_path, source)
        self._python_outline_cache[file_path] = outline
        return outline

    def _python_symbol(self, entity: RepositoryEntity) -> PythonSymbol | None:
        outline = self._python_outline(entity.file_path)
        if outline is None:
            return None

        exact = outline.symbol(entity.orcaloca_id)
        if exact is not None:
            return exact

        candidates = [
            symbol
            for symbol in outline.symbols
            if symbol.name == entity.simple_name
            and symbol.class_name == entity.class_name
        ]
        if not candidates:
            return None
        if entity.start_line is None:
            return candidates[0]
        graph_start = entity.start_line + 1
        return min(
            candidates,
            key=lambda symbol: (
                abs(symbol.start_line - graph_start),
                symbol.end_line - symbol.start_line,
                symbol.node_id,
            ),
        )

    @staticmethod
    def _normalize_code(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip())

    @staticmethod
    def _disambiguation(
        heading: str,
        *,
        entities: Sequence[RepositoryEntity] = (),
        files: Sequence[str] = (),
    ) -> str:
        lines = [heading]
        if entities:
            for index, entity in enumerate(entities, start=1):
                lines.extend(
                    [
                        f"Possible Location {index}:",
                        f"File Path: {entity.file_path}",
                    ]
                )
                if entity.class_name:
                    lines.append(f"Containing Class: {entity.class_name}")
                lines.append("")
        else:
            for index, file_path in enumerate(files, start=1):
                lines.extend(
                    [
                        f"Possible Location {index}:",
                        f"File Path: {file_path}",
                        "",
                    ]
                )
        return "<Disambiguation>\n" + "\n".join(lines).rstrip() + "\n</Disambiguation>"

    def _bounded(self, value: str) -> str:
        if len(value) <= self.max_output_chars:
            return value
        marker = "\n\n[Output truncated by CodeNib's OrcaLoca adapter budget.]"
        return value[: self.max_output_chars - len(marker)].rstrip() + marker


def make_orcaloca_search_manager_factory(
    manifest_path: str | Path,
    **provider_options: Any,
) -> Callable[[str], OrcaLocaSearchProvider]:
    """Build the factory accepted by the proposed upstream injection seam."""

    resolved_manifest = Path(manifest_path).expanduser().resolve()

    def factory(repo_path: str) -> OrcaLocaSearchProvider:
        provider = OrcaLocaSearchProvider.from_manifest(
            resolved_manifest, **provider_options
        )
        requested = Path(repo_path).expanduser().resolve()
        if requested != provider.repository.repo_root:
            raise ValueError(
                "OrcaLoca repo_path does not match the CodeNib manifest: "
                f"{requested} != {provider.repository.repo_root}"
            )
        return provider

    return factory


def orcaloca_bug_location(
    entity: RepositoryEntity,
) -> dict[str, str]:
    """Translate one CodeNib entity to OrcaLoca's final location schema."""

    query_type = OrcaLocaSearchProvider._graph_query_type(entity)
    return {
        "file_path": entity.file_path,
        "class_name": (
            entity.simple_name if query_type == "class" else entity.class_name or ""
        ),
        "method_name": (
            entity.simple_name if query_type in {"method", "function"} else ""
        ),
    }


__all__ = [
    "ORCALOCA_HISTORY_FIELDS",
    "ORCALOCA_MANAGER_HOOKS",
    "ORCALOCA_REVISION",
    "ORCALOCA_TOOL_NAMES",
    "OrcaLocaSearchProvider",
    "OrcaLocation",
    "OrcaLocationInfo",
    "make_orcaloca_search_manager_factory",
    "orcaloca_bug_location",
]
