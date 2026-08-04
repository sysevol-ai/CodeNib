# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Classic Agentless repository context over manifest-backed views.

The compatibility boundary is the localization phase from Agentless v1.5.0:
Python project structure, compressed symbol context, and bounded line context.
Repair generation and patch validation remain outside this adapter.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from ..types import (
    NODE_TYPE_CLASS,
    NODE_TYPE_FIELD,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NODE_TYPE_SYMBOL,
)
from ._repository import RepositoryAdapter, RepositoryEntity

AGENTLESS_REVISION = "b150f28465a77a81a7f4776384957a4271f5bd69"
AGENTLESS_VERSION = "v1.5.0"

_RUNTIME_VIEWS = frozenset({"symbol_graph"})
_SYMBOL_KINDS = frozenset(
    {
        NODE_TYPE_CLASS,
        NODE_TYPE_FIELD,
        NODE_TYPE_FUNCTION,
        NODE_TYPE_METHOD,
        NODE_TYPE_SYMBOL,
    }
)
_LIST_PREFIX = re.compile(r"^(?:[-*]\s+|\d+[.)]\s+)")


def _is_agentless_python_path(file_path: str) -> bool:
    """Match Agentless's Python-only and ``test*`` subtree filters."""

    path = PurePosixPath(file_path)
    return path.suffix == ".py" and not any(
        part.startswith("test") for part in path.parts
    )


class AgentlessRepositoryProvider:
    """Serve classic Agentless localization context from one manifest graph."""

    def __init__(self, context: Any) -> None:
        self.repository = RepositoryAdapter(context, require_graph=True)
        self._files = tuple(
            file_path
            for file_path in self.repository.files
            if _is_agentless_python_path(file_path)
        )

    @classmethod
    def from_manifest(
        cls, manifest_path: str | Path, **_kwargs: Any
    ) -> "AgentlessRepositoryProvider":
        from ..mcp.context import ServerContext

        return cls(ServerContext.load(manifest_path, views=_RUNTIME_VIEWS))

    @property
    def context(self) -> Any:
        return self.repository.context

    @property
    def files(self) -> tuple[str, ...]:
        return self._files

    @property
    def project_root_name(self) -> str:
        return self.repository.repo_root.name

    def project_structure(self) -> str:
        """Render the Python tree in Agentless's four-space layout."""

        tree: dict[str, Any] = {}
        for file_path in self._files:
            node = tree
            for part in PurePosixPath(file_path).parts:
                node = node.setdefault(part, {})

        lines = [f"{self.project_root_name}/"]

        def render(current: Mapping[str, Any], depth: int) -> None:
            for name, children in current.items():
                suffix = "/" if children else ""
                lines.append(f"{' ' * (depth * 4)}{name}{suffix}")
                if children:
                    render(children, depth + 1)

        render(tree, 1)
        return "\n".join(lines)

    def resolve_files(
        self,
        values: str | Sequence[str],
        *,
        limit: int | None = None,
    ) -> tuple[str, ...]:
        """Resolve an Agentless file response to ranked repository paths."""

        lines = values.splitlines() if isinstance(values, str) else values
        selected: list[str] = []
        seen: set[str] = set()
        root_prefix = self.project_root_name + "/"

        for raw in lines:
            value = _LIST_PREFIX.sub("", str(raw).strip().strip("`'\" "))
            if not value:
                continue
            value = value.replace("\\", "/")
            while value.startswith("./"):
                value = value[2:]
            if value.startswith(root_prefix):
                value = value[len(root_prefix) :]
            matches = self.repository.find_files(value)
            for match in matches:
                if match not in self._files or match in seen:
                    continue
                seen.add(match)
                selected.append(match)
                break
            if limit is not None and len(selected) >= limit:
                break
        return tuple(selected)

    def skeleton_context(self, file_paths: Sequence[str]) -> str:
        """Format graph-selected Python skeletons for Agentless stage two."""

        sections: list[str] = []
        for file_path in self.resolve_files(file_paths):
            content, _, _ = self.repository.read_range(file_path)
            skeleton = self._python_skeleton(content)
            sections.append(
                f"\n### File: {file_path} ###\n```python\n{skeleton}\n```\n"
            )
        return "".join(sections)

    def line_context(
        self,
        file_paths: Sequence[str],
        locations: Sequence[Any],
        *,
        context_window: int = 10,
    ) -> str:
        """Build numbered source windows around coarse Agentless locations."""

        window = max(0, int(context_window))
        allowed_files = self.resolve_files(file_paths)
        by_file: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for location in locations:
            file_path = str(getattr(location, "file_path", "") or "")
            resolved = self.resolve_files([file_path], limit=1)
            if not resolved or resolved[0] not in allowed_files:
                continue
            file_path = resolved[0]
            interval = self._location_interval(file_path, location)
            if interval is not None:
                by_file[file_path].append(
                    (max(0, interval[0] - window), interval[1] + window)
                )

        sections: list[str] = []
        for file_path in allowed_files:
            lines = self.repository.source_lines(file_path)
            if not lines:
                continue
            intervals = by_file.get(file_path)
            if not intervals:
                continue
            intervals = self._merge_intervals(
                (start, min(end, len(lines) - 1)) for start, end in intervals
            )
            rendered: list[str] = []
            previous_end: int | None = None
            for start, end in intervals:
                if previous_end is not None and start > previous_end + 1:
                    rendered.append("...")
                rendered.extend(
                    f"{line_number + 1}|{lines[line_number]}"
                    for line_number in range(start, end + 1)
                )
                previous_end = end
            sections.append(f"### File: {file_path} ###\n" + "\n".join(rendered))
        return "\n\n".join(sections)

    def resolve_entity(
        self, file_path: str, kind: str, name: str
    ) -> RepositoryEntity | None:
        """Resolve one parsed Agentless class/function/variable location."""

        resolved = self.resolve_files([file_path], limit=1)
        if not resolved or not name:
            return None
        kinds: set[str]
        if kind == "class":
            kinds = {NODE_TYPE_CLASS}
        elif kind in {"function", "method"}:
            kinds = {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}
        elif kind == "variable":
            kinds = {NODE_TYPE_CLASS, NODE_TYPE_FIELD, NODE_TYPE_SYMBOL}
        else:
            kinds = set(_SYMBOL_KINDS)
        matches = self.repository.find_entities(
            name,
            file_path=resolved[0],
            kinds=kinds,
        )
        if len(matches) == 1:
            return matches[0]
        if "." in name:
            matches = self.repository.find_entities(
                name.rsplit(".", 1)[-1],
                file_path=resolved[0],
                kinds=kinds,
            )
        return matches[0] if len(matches) == 1 else None

    def _location_interval(
        self, file_path: str, location: Any
    ) -> tuple[int, int] | None:
        line_start = getattr(location, "line_start", None)
        line_end = getattr(location, "line_end", None)
        if isinstance(line_start, int) and not isinstance(line_start, bool):
            start = max(0, line_start - 1)
            end = max(start, (line_end or line_start) - 1)
            return start, end

        entity = self.resolve_entity(
            file_path,
            str(getattr(location, "kind", "") or ""),
            str(getattr(location, "name", "") or ""),
        )
        if entity and entity.start_line is not None and entity.end_line is not None:
            return entity.start_line, entity.end_line
        return None

    @staticmethod
    def _merge_intervals(
        intervals: Iterable[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        merged: list[list[int]] = []
        for start, end in sorted(intervals):
            if not merged or start > merged[-1][1] + 1:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return [(start, end) for start, end in merged]

    @classmethod
    def _python_skeleton(cls, content: str) -> str:
        """Approximate Agentless's libcst skeleton with the Python stdlib AST."""

        try:
            module = ast.parse(content)
        except SyntaxError:
            return content
        lines = content.splitlines()
        rendered: list[str] = []
        for node in module.body:
            rendered.extend(cls._render_python_node(node, lines, indent=0))
        return "\n".join(rendered).rstrip()

    @classmethod
    def _render_python_node(
        cls,
        node: ast.AST,
        lines: Sequence[str],
        *,
        indent: int,
    ) -> list[str]:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            start = max(0, int(getattr(node, "lineno", 1)) - 1)
            end = max(start, int(getattr(node, "end_lineno", start + 1)) - 1)
            return [" " * indent + line.lstrip() for line in lines[start : end + 1]]

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            header = cls._definition_header(node, lines, indent)
            return [*header, " " * (indent + 4) + "..."]

        if isinstance(node, ast.ClassDef):
            output = cls._definition_header(node, lines, indent)
            body: list[str] = []
            for child in node.body:
                if isinstance(child, ast.Expr) and isinstance(
                    getattr(child, "value", None), (ast.Constant, ast.Str)
                ):
                    continue
                body.extend(cls._render_python_node(child, lines, indent=indent + 4))
            output.extend(body or [" " * (indent + 4) + "..."])
            return output
        return []

    @staticmethod
    def _definition_header(
        node: ast.AST,
        lines: Sequence[str],
        indent: int,
    ) -> list[str]:
        decorators = list(getattr(node, "decorator_list", ()) or ())
        output = [" " * indent + "@" + ast.unparse(item) for item in decorators]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            signature = f"{prefix} {node.name}({ast.unparse(node.args)})"
            if node.returns is not None:
                signature += f" -> {ast.unparse(node.returns)}"
            output.append(" " * indent + signature + ":")
            return output
        if isinstance(node, ast.ClassDef):
            arguments = [ast.unparse(base) for base in node.bases]
            arguments.extend(
                (
                    f"{keyword.arg}={ast.unparse(keyword.value)}"
                    if keyword.arg is not None
                    else f"**{ast.unparse(keyword.value)}"
                )
                for keyword in node.keywords
            )
            suffix = f"({', '.join(arguments)})" if arguments else ""
            output.append(" " * indent + f"class {node.name}{suffix}:")
            return output
        line_number = max(1, int(getattr(node, "lineno", 1)))
        fallback = lines[line_number - 1].lstrip() if lines else "symbol"
        output.append(" " * indent + fallback)
        return output


__all__ = [
    "AGENTLESS_REVISION",
    "AGENTLESS_VERSION",
    "AgentlessRepositoryProvider",
]
