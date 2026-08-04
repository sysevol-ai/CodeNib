# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CoSIL localization tools over CodeNib manifest-backed source views."""

from __future__ import annotations

import ast
import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from ._repository import RepositoryAdapter

COSIL_REVISION = "0568e423735b399d5b089996961fea9ae142e4c7"

_RUNTIME_VIEWS = frozenset({"symbol_graph"})
_TOOL_NAMES = (
    "get_code_of_class",
    "get_code_of_class_function",
    "get_code_of_file_function",
    "exit",
)
_LIST_PREFIX = re.compile(r"^(?:[-*]\s+|\d+[.)]\s+)")

_COSIL_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": "get_code_of_class",
            "description": "Get the source code of a class in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": (
                            "Full file path exactly as shown after 'file:' in the "
                            "candidate file structure."
                        ),
                    },
                    "class_name": {
                        "type": "string",
                        "description": (
                            "Class name exactly as shown in the candidate file "
                            "structure."
                        ),
                    },
                },
                "required": ["file_name", "class_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_code_of_class_function",
            "description": "Get the source code of a method defined in a class.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": (
                            "Full file path exactly as shown after 'file:' in the "
                            "candidate file structure."
                        ),
                    },
                    "class_name": {
                        "type": "string",
                        "description": (
                            "Class name exactly as shown in the candidate file "
                            "structure."
                        ),
                    },
                    "func_name": {
                        "type": "string",
                        "description": (
                            "Method name exactly as shown in the class functions list."
                        ),
                    },
                },
                "required": ["file_name", "class_name", "func_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_code_of_file_function",
            "description": (
                "Get the source code of a top-level function defined in a file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": (
                            "Full file path exactly as shown after 'file:' in the "
                            "candidate file structure."
                        ),
                    },
                    "func_name": {
                        "type": "string",
                        "description": (
                            "Top-level function name exactly as shown in the static "
                            "functions list."
                        ),
                    },
                },
                "required": ["file_name", "func_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exit",
            "description": (
                "Exit tool calling and proceed to give the final answer once you are "
                "confident about the culprit locations. Call this instead of making "
                "further retrieval calls."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
)


def get_cosil_tool_schemas() -> list[dict[str, Any]]:
    """Return independent OpenAI schemas for the pinned CoSIL tools."""

    return copy.deepcopy(list(_COSIL_TOOL_SCHEMAS))


@dataclass(frozen=True, slots=True)
class CoSILMethod:
    name: str
    start_line: int
    end_line: int
    source: str


@dataclass(frozen=True, slots=True)
class CoSILClass:
    name: str
    start_line: int
    end_line: int
    source: str
    methods: tuple[CoSILMethod, ...]


@dataclass(frozen=True, slots=True)
class CoSILFunction:
    name: str
    start_line: int
    end_line: int
    source: str


@dataclass(frozen=True, slots=True)
class CoSILOutline:
    file_path: str
    source: str
    classes: tuple[CoSILClass, ...]
    functions: tuple[CoSILFunction, ...]


class CoSILRepositoryProvider:
    """Serve CoSIL's four tools without its per-instance AST artifact."""

    def __init__(self, context: Any, *, include_tests: bool = False) -> None:
        self.repository = RepositoryAdapter(context, require_graph=True)
        self.include_tests = bool(include_tests)
        self._files = tuple(
            file_path
            for file_path in self.repository.files
            if self._accept_file(file_path)
        )
        self._outlines: dict[str, CoSILOutline | None] = {}

    @classmethod
    def from_manifest(
        cls,
        manifest_path: str | Path,
        **kwargs: Any,
    ) -> "CoSILRepositoryProvider":
        from ..mcp.context import ServerContext

        return cls(ServerContext.load(manifest_path, views=_RUNTIME_VIEWS), **kwargs)

    @property
    def context(self) -> Any:
        return self.repository.context

    @property
    def files(self) -> tuple[str, ...]:
        return self._files

    @property
    def project_root_name(self) -> str:
        return self.repository.repo_root.name

    def bindings(self) -> dict[str, Callable[..., str]]:
        return {
            "get_code_of_class": self.get_code_of_class,
            "get_code_of_class_function": self.get_code_of_class_function,
            "get_code_of_file_function": self.get_code_of_file_function,
            "exit": self.exit,
        }

    def dispatch(
        self,
        name: str,
        arguments: Mapping[str, Any] | str | None = None,
    ) -> str:
        if isinstance(arguments, str):
            try:
                decoded = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ValueError("CoSIL tool arguments must be valid JSON") from exc
            if not isinstance(decoded, Mapping):
                raise ValueError("CoSIL tool arguments must decode to an object")
            parsed = dict(decoded)
        else:
            parsed = dict(arguments or {})
        function = self.bindings().get(name)
        if function is None:
            raise KeyError(
                f"unknown CoSIL tool {name!r}; supported: {', '.join(_TOOL_NAMES)}"
            )
        return function(**parsed)

    def project_structure(self) -> str:
        tree: dict[str, Any] = {}
        for file_path in self._files:
            node = tree
            for part in PurePosixPath(file_path).parts:
                node = node.setdefault(part, {})
        lines = [f"{self.project_root_name}/"]

        def render(current: Mapping[str, Any], depth: int) -> None:
            for name, children in current.items():
                lines.append(f"{' ' * (depth * 4)}{name}{'/' if children else ''}")
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
        lines = values.splitlines() if isinstance(values, str) else values
        root_prefix = self.project_root_name + "/"
        output: list[str] = []
        seen: set[str] = set()
        for raw in lines:
            value = _LIST_PREFIX.sub("", str(raw).strip().strip("`'\" "))
            value = value.replace("\\", "/")
            while value.startswith("./"):
                value = value[2:]
            if value not in self._files and value.startswith(root_prefix):
                value = value[len(root_prefix) :]
            if value in self._files and value not in seen:
                seen.add(value)
                output.append(value)
            if limit is not None and len(output) >= limit:
                break
        return tuple(output)

    def candidate_structure(self, file_names: Sequence[str]) -> str:
        sections: list[str] = []
        for file_path in self.resolve_files(file_names):
            outline = self._outline(file_path)
            if outline is None:
                continue
            classes = [item.name for item in outline.classes]
            functions = [item.name for item in outline.functions]
            class_functions = "[\n"
            for item in outline.classes:
                class_functions += (
                    f"{item.name}: {[method.name for method in item.methods]} \n"
                )
            class_functions += "]"
            sections.append(
                f"file: {file_path} \n"
                f"\t class: {classes} \n"
                f"\t static functions:  {functions} \n"
                f"\t class fucntions: {class_functions}\n"
            )
        return "".join(sections)

    def import_context(self, file_names: Sequence[str]) -> str:
        sections: list[str] = []
        for file_path in self.resolve_files(file_names):
            outline = self._outline(file_path)
            if outline is None:
                continue
            imports = [
                line
                for line in outline.source.splitlines()
                if line.startswith("import")
                or (line.startswith("from") and "import" in line)
            ]
            sections.append(f"file: {file_path}\n {imports}\n")
        return "".join(sections)

    def get_code_of_class(self, file_name: str, class_name: str) -> str:
        outline = self._resolved_outline(file_name)
        if outline is not None:
            match = next(
                (item for item in outline.classes if item.name == class_name),
                None,
            )
            if match is not None:
                return match.source
        return (
            "You provide a wrong file name or class name. Please try another file "
            "name again."
        )

    def get_code_of_class_function(
        self,
        file_name: str,
        class_name: str,
        func_name: str,
    ) -> str:
        outline = self._resolved_outline(file_name)
        if outline is not None:
            class_match = next(
                (item for item in outline.classes if item.name == class_name),
                None,
            )
            if class_match is not None:
                method = next(
                    (item for item in class_match.methods if item.name == func_name),
                    None,
                )
                if method is not None:
                    return method.source
        return (
            "You provide a wrong file name or class name or function name. Please "
            "try another file name again. It may be a file function."
        )

    def get_code_of_file_function(self, file_name: str, func_name: str) -> str:
        outline = self._resolved_outline(file_name)
        if outline is not None:
            match = next(
                (item for item in outline.functions if item.name == func_name),
                None,
            )
            if match is not None:
                return match.source
        return (
            "You provide a wrong file name or function name. Please try another "
            "file name again. It may be a class function."
        )

    def resolve_location(
        self,
        file_name: str,
        kind: str,
        name: str,
    ) -> tuple[str, str, int, int] | None:
        """Resolve CoSIL's XML identity to kind, name, and 1-based range."""

        outline = self._resolved_outline(file_name)
        if outline is None:
            return None
        if kind == "class":
            item = next((item for item in outline.classes if item.name == name), None)
            if item is not None:
                return "class", item.name, item.start_line, item.end_line
            return None
        if kind == "function" and "." in name:
            class_name, method_name = name.rsplit(".", 1)
            class_item = next(
                (item for item in outline.classes if item.name == class_name),
                None,
            )
            if class_item is not None:
                method = next(
                    (item for item in class_item.methods if item.name == method_name),
                    None,
                )
                if method is not None:
                    return "method", name, method.start_line, method.end_line
        if kind == "function":
            item = next(
                (item for item in outline.functions if item.name == name),
                None,
            )
            if item is not None:
                return "function", item.name, item.start_line, item.end_line
        return None

    @staticmethod
    def exit() -> str:
        return "Exiting tool calls. Now provide your final answer."

    def _accept_file(self, file_path: str) -> bool:
        path = PurePosixPath(file_path)
        if path.suffix != ".py":
            return False
        return self.include_tests or not any(
            part.startswith("test") for part in path.parts
        )

    def _resolved_outline(self, file_name: str) -> CoSILOutline | None:
        files = self.resolve_files([file_name], limit=1)
        return self._outline(files[0]) if files else None

    def _outline(self, file_path: str) -> CoSILOutline | None:
        if file_path in self._outlines:
            return self._outlines[file_path]
        source, _, _ = self.repository.read_range(file_path)
        outline = self._parse_outline(file_path, source)
        self._outlines[file_path] = outline
        return outline

    @staticmethod
    def _parse_outline(file_path: str, source: str) -> CoSILOutline | None:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None
        lines = source.splitlines()
        classes: list[CoSILClass] = []
        functions: list[CoSILFunction] = []
        class_method_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                methods: list[CoSILMethod] = []
                for child in node.body:
                    if not isinstance(child, ast.FunctionDef):
                        continue
                    start = int(child.lineno)
                    end = int(child.end_lineno or start)
                    methods.append(
                        CoSILMethod(
                            name=child.name,
                            start_line=start,
                            end_line=end,
                            source="\n".join(lines[start - 1 : end]),
                        )
                    )
                    class_method_names.add(child.name)
                start = int(node.lineno)
                end = int(node.end_lineno or start)
                classes.append(
                    CoSILClass(
                        name=node.name,
                        start_line=start,
                        end_line=end,
                        source="\n".join(lines[start - 1 : end]),
                        methods=tuple(methods),
                    )
                )
            elif isinstance(node, ast.FunctionDef):
                if node.name in class_method_names:
                    continue
                start = int(node.lineno)
                end = int(node.end_lineno or start)
                functions.append(
                    CoSILFunction(
                        name=node.name,
                        start_line=start,
                        end_line=end,
                        source="\n".join(lines[start - 1 : end]),
                    )
                )
        return CoSILOutline(
            file_path=file_path,
            source=source,
            classes=tuple(classes),
            functions=tuple(functions),
        )


__all__ = [
    "COSIL_REVISION",
    "CoSILRepositoryProvider",
    "get_cosil_tool_schemas",
]
