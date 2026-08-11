#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Python-specific code chunker implementation.
"""

import os
import time
from collections import namedtuple
from struct import Struct
from typing import Any, List, Optional, Tuple

from .base import BaseCodeChunker

_NATIVE_CHUNK_ENV = "CODENIB_NATIVE_PYTHON_CHUNKER"
_NATIVE_CHUNK_OFF = frozenset({"", "0", "false", "no", "off", "disabled"})
_NATIVE_CHUNK_AUTO = frozenset({"1", "true", "yes", "on", "auto"})
_NATIVE_CHUNK_REQUIRED = frozenset({"required", "strict"})
_NATIVE_CHUNK_ABI_VERSION = 1
_NATIVE_CHUNK_ROW = Struct("<IIB3xII")
_NATIVE_CHUNK_GRAMMAR = "tree-sitter-python@v0.25.0"
_NATIVE_CHUNK_RUNTIME = "tree-sitter@v0.25.2"
_NATIVE_CHUNK_SEMANTIC_PROFILE = "python-definition-spans-v1"
_NATIVE_CHUNK_KINDS = {1: "function", 2: "class", 3: "method"}
_NativeNodeSpan = namedtuple("_NativeNodeSpan", ["start_point", "end_point"])


def _native_chunk_mode() -> str:
    value = os.environ.get(_NATIVE_CHUNK_ENV, "off").strip().lower()
    if value in _NATIVE_CHUNK_OFF:
        return "off"
    if value in _NATIVE_CHUNK_AUTO:
        return "auto"
    if value in _NATIVE_CHUNK_REQUIRED:
        return "required"
    raise ValueError(
        f"{_NATIVE_CHUNK_ENV} must be auto, required, or 0/off; got {value!r}"
    )


def _load_native_core():
    try:
        import codenib_core
    except ImportError:
        return None
    return codenib_core


def _validate_native_contract(core: Any) -> dict[str, Any]:
    contract_function = getattr(core, "python_chunk_poc_contract", None)
    if not callable(contract_function):
        raise RuntimeError("compiled core does not expose Python chunk POC contract")
    contract = contract_function()
    if not isinstance(contract, dict):
        raise TypeError("native Python chunk contract must be a dict")

    expected = {
        "abi_version": _NATIVE_CHUNK_ABI_VERSION,
        "row_size": _NATIVE_CHUNK_ROW.size,
        "grammar": _NATIVE_CHUNK_GRAMMAR,
        "runtime": _NATIVE_CHUNK_RUNTIME,
        "semantic_profile": _NATIVE_CHUNK_SEMANTIC_PROFILE,
    }
    for name, value in expected.items():
        compiled = contract.get(name)
        if type(compiled) is not type(value) or compiled != value:
            raise RuntimeError(
                f"native Python chunk {name} mismatch: "
                f"compiled={compiled!r}, python={value!r}"
            )
    return contract


def _decode_native_chunk_payload(
    payload: Any, *, source_line_count: int
) -> tuple[List[Tuple], dict[str, float]]:
    if not isinstance(payload, dict):
        raise TypeError("native Python chunk payload must be a dict")
    abi_version = payload.get("abi_version")
    if type(abi_version) is not int or abi_version != _NATIVE_CHUNK_ABI_VERSION:
        raise ValueError("native Python chunk payload ABI mismatch")
    if type(source_line_count) is not int or source_line_count < 1:
        raise ValueError("native Python chunk source_line_count must be positive")

    rows = payload.get("rows")
    arena = payload.get("arena")
    row_count = payload.get("row_count")
    if not isinstance(rows, bytes) or not isinstance(arena, bytes):
        raise TypeError("native Python chunk rows and arena must be bytes")
    if type(row_count) is not int or row_count < 0:
        raise ValueError("native Python chunk row_count must be a non-negative integer")
    if len(rows) != row_count * _NATIVE_CHUNK_ROW.size:
        raise ValueError("native Python chunk row table has an invalid size")

    definitions: List[Tuple] = []
    previous_start_line: Optional[int] = None
    for offset in range(0, len(rows), _NATIVE_CHUNK_ROW.size):
        (
            start_line,
            end_line,
            kind_id,
            name_offset,
            name_length,
        ) = _NATIVE_CHUNK_ROW.unpack_from(rows, offset)
        if start_line > end_line:
            raise ValueError("native Python chunk span is reversed")
        if end_line >= source_line_count:
            raise ValueError("native Python chunk span exceeds source line bounds")
        if previous_start_line is not None and start_line < previous_start_line:
            raise ValueError("native Python chunk spans are not sorted")
        previous_start_line = start_line

        kind = _NATIVE_CHUNK_KINDS.get(kind_id)
        if kind is None:
            raise ValueError(f"native Python chunk kind is invalid: {kind_id}")
        name_end = name_offset + name_length
        if name_offset > len(arena) or name_end > len(arena):
            raise ValueError("native Python chunk name exceeds arena bounds")
        try:
            name = arena[name_offset:name_end].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("native Python chunk name is not valid UTF-8") from exc
        if not name:
            raise ValueError("native Python chunk name must not be empty")
        node = _NativeNodeSpan((start_line, 0), (end_line, 0))
        definitions.append((node, name, kind))

    timing_fields = {
        "native_parse": payload.get("parse_ns"),
        "native_extract_encode": payload.get("extract_ns"),
    }
    timings: dict[str, float] = {}
    for name, value in timing_fields.items():
        if type(value) is not int or value < 0:
            raise ValueError(f"native Python chunk timing {name} is invalid")
        timings[name] = value / 1_000_000_000
    return definitions, timings


class PythonCodeChunker(BaseCodeChunker):
    """
    Code chunker specifically for Python files.

    L1 entities: module-level functions and classes.
    L2 entities: methods inside classes (including async and decorated definitions).
    Skeleton mode: module skeleton lists L1 definitions and class member signatures;
    class skeleton lists method signatures.
    """

    def __init__(
        self,
        max_lines_per_chunk: Optional[int] = None,
        chunk_depth: int = 2,
        l2_level_exclusive: bool = True,
        **kwargs,
    ):
        """Initialize the Python code chunker."""
        super().__init__(
            "python",
            max_lines_per_chunk=max_lines_per_chunk,
            chunk_depth=chunk_depth,
            l2_level_exclusive=l2_level_exclusive,
            **kwargs,
        )
        self.native_chunk_backend = "python"
        self.native_chunk_fallback_error: Optional[str] = None
        self.native_chunk_timings: dict[str, float] = {}
        self._native_contract_validated = False
        self._native_fallback_warned = False

    def chunk_file(
        self,
        file_path: str,
        relative_path: Optional[str] = None,
        skeleton_mode: Optional[bool] = None,
    ):
        """Use the opt-in native span extractor with per-file fallback."""

        self.native_chunk_backend = "python"
        self.native_chunk_fallback_error = None
        self.native_chunk_timings = {}
        mode = _native_chunk_mode()
        if mode == "off":
            return super().chunk_file(file_path, relative_path, skeleton_mode)

        use_skeleton = self.skeleton_mode if skeleton_mode is None else skeleton_mode
        if self.chunk_depth not in {1, 2} or use_skeleton:
            message = (
                "native Python chunk extraction supports non-skeleton "
                "chunk_depth 1 or 2"
            )
            if mode == "required":
                raise RuntimeError(message)
            self.native_chunk_backend = "python-unsupported"
            return super().chunk_file(file_path, relative_path, skeleton_mode)

        if not os.path.exists(file_path):
            return super().chunk_file(file_path, relative_path, skeleton_mode)

        try:
            core = _load_native_core()
            if core is None or not callable(
                getattr(core, "extract_python_chunk_spans", None)
            ):
                raise RuntimeError(
                    "compiled core does not include the native Python chunk POC"
                )
            if not self._native_contract_validated:
                _validate_native_contract(core)
                self._native_contract_validated = True

            total_started = time.perf_counter()
            read_started = time.perf_counter()
            code_content = self._read_source(file_path)
            file_read = time.perf_counter() - read_started
            binding_started = time.perf_counter()
            payload = core.extract_python_chunk_spans(
                code_content,
                chunk_depth=self.chunk_depth,
                l2_level_exclusive=self.l2_level_exclusive,
            )
            binding = time.perf_counter() - binding_started
            lines = code_content.split("\n")
            decode_started = time.perf_counter()
            definitions, timings = _decode_native_chunk_payload(
                payload, source_line_count=len(lines)
            )
            buffer_decode = time.perf_counter() - decode_started

            path_for_node_id = relative_path if relative_path else file_path
            materialize_started = time.perf_counter()
            chunks = self._generate_chunks(
                lines=lines,
                definitions=definitions,
                file_path=file_path,
                path_for_node_id=path_for_node_id,
                code_content=code_content,
                skeleton_mode=False,
            )
            materialize = time.perf_counter() - materialize_started
            self.native_chunk_backend = "native-tree-sitter-poc"
            self.native_chunk_timings = {
                "file_read": file_read,
                "binding": binding,
                "buffer_decode": buffer_decode,
                "chunk_materialize": materialize,
                "total": time.perf_counter() - total_started,
                **timings,
            }
            return chunks
        except Exception as exc:
            if mode == "required":
                raise
            self.native_chunk_fallback_error = f"{type(exc).__name__}: {exc}"
            self.native_chunk_backend = "python-fallback"
            if not self._native_fallback_warned:
                from ..log_utils import get_logger

                get_logger(__name__).warning(
                    "Native Python chunk extraction failed; falling back per file: %s",
                    self.native_chunk_fallback_error,
                )
                self._native_fallback_warned = True
            return super().chunk_file(file_path, relative_path, skeleton_mode)

    def _find_top_level_definitions(
        self, root_node, include_l2_in_file_skeleton: bool = False
    ) -> List[Tuple]:
        """
        Find all top-level function and class definitions in Python.
        Handles decorated_definition, function_definition, and class_definition nodes.

        Args:
            root_node: Root node of the AST
            include_l2_in_file_skeleton: Include methods when building file skeletons.

        Returns:
            List of tuples (node, name, type) for each top-level definition
        """
        definitions = []

        # For Python, look for function_definition, class_definition, and
        # decorated_definition at module level
        include_type_level = self.chunk_depth < 2 or not self.l2_level_exclusive
        include_methods = self.chunk_depth >= 2 or include_l2_in_file_skeleton

        for child in root_node.children:
            if child.type == "decorated_definition":
                # Extract the actual definition (function or class) from decorated_definition
                actual_def = self._extract_definition_from_decorated(child)
                if actual_def:
                    def_type = actual_def.type
                    if def_type in ("function_definition", "async_function_definition"):
                        name = self._extract_function_name(actual_def)
                        if name:
                            # Use the decorated_definition node (includes decorators)
                            definitions.append((child, name, "function"))
                    elif def_type == "class_definition":
                        name = self._extract_class_name(actual_def)
                        if name:
                            # Use the decorated_definition node (includes decorators)
                            if include_type_level:
                                definitions.append((child, name, "class"))
                            # Extract methods only if chunk_depth >= 2
                            if include_methods:
                                methods = self._find_class_methods(actual_def)
                                definitions.extend(methods)
            elif child.type in ("function_definition", "async_function_definition"):
                name = self._extract_function_name(child)
                if name:
                    definitions.append((child, name, "function"))
            elif child.type == "class_definition":
                name = self._extract_class_name(child)
                if name:
                    if include_type_level:
                        definitions.append((child, name, "class"))
                    # Extract methods only if chunk_depth >= 2
                    if include_methods:
                        methods = self._find_class_methods(child)
                        definitions.extend(methods)

        # Sort by start line
        definitions.sort(key=lambda x: x[0].start_point[0])
        return definitions

    def _extract_signature_text(self, node, def_type: str, code_content: str) -> str:
        """Extract a signature for Python constructs by trimming the block body."""
        stop_types = {
            "function": ("block",),
            "method": ("block",),
            "class": ("block",),
        }.get(def_type, ("block",))
        return self._extract_signature_text_default(node, stop_types, code_content)

    def _build_file_skeleton(
        self,
        definitions: List[Tuple],
        code_content: str,
        include_l2: Optional[bool] = None,
    ) -> str:
        include_l2 = (
            self.include_l2_in_file_skeleton if include_l2 is None else include_l2
        )
        container_names = {
            name for _, name, def_type in definitions if def_type == "class"
        }
        entries: List[str] = []
        for node, name, def_type in definitions:
            if def_type == "method":
                if not include_l2:
                    continue
                parent = name.split(".", 1)[0] if "." in name else None
                if parent and parent in container_names:
                    # Already represented inside the class skeleton.
                    continue
                signature = self._extract_signature_text(node, def_type, code_content)
                if signature:
                    entries.append(self._indent_lines(signature))
                continue

            skeleton = self._build_definition_skeleton(
                node,
                def_type,
                code_content=code_content,
                include_children=include_l2,
            )
            if skeleton:
                entries.append(skeleton)
        return "\n".join(entries)

    def _extract_definition_from_decorated(self, decorated_node) -> Optional[object]:
        """
        Extract the actual definition node from a decorated_definition node.

        Args:
            decorated_node: AST node representing a decorated_definition

        Returns:
            The function_definition, async_function_definition, or class_definition
            node, or None
        """
        for child in decorated_node.children:
            if child.type in (
                "function_definition",
                "async_function_definition",
                "class_definition",
            ):
                return child
        return None

    def _extract_function_name(self, node) -> Optional[str]:
        """
        Extract function name from Python function_definition or async_function_definition node.

        Args:
            node: AST node representing a Python function definition

        Returns:
            Function name or None if extraction failed
        """
        for child in node.children:
            if child.type == "identifier":
                return child.text.decode("utf-8")
        return None

    def _extract_class_name(self, node) -> Optional[str]:
        """
        Extract class name from Python class_definition node.

        Args:
            node: AST node representing a Python class definition

        Returns:
            Class name or None if extraction failed
        """
        for child in node.children:
            if child.type == "identifier":
                return child.text.decode("utf-8")
        return None

    def _find_class_methods(self, class_node) -> List[Tuple]:
        """
        Find all method definitions within a Python class.
        Handles function_definition, async_function_definition, and decorated_definition nodes.

        Args:
            class_node: AST node representing a Python class definition

        Returns:
            List of tuples (node, name, type) for each method definition
        """
        methods = []

        class_name = self._extract_class_name(class_node)
        if not class_name:
            return methods

        # Look for the class body
        for child in class_node.children:
            if child.type == "block":
                # Within the class body, look for function definitions (methods)
                for stmt in child.children:
                    if stmt.type == "decorated_definition":
                        # Extract the actual method definition from decorated_definition
                        actual_def = self._extract_definition_from_decorated(stmt)
                        if actual_def and actual_def.type in (
                            "function_definition",
                            "async_function_definition",
                        ):
                            method_name = self._extract_method_name(actual_def)
                            if method_name:
                                # Include class name prefix for node_id
                                full_method_name = f"{class_name}.{method_name}"
                                # Use the decorated_definition node (includes decorators)
                                methods.append((stmt, full_method_name, "method"))
                    elif stmt.type in (
                        "function_definition",
                        "async_function_definition",
                    ):
                        method_name = self._extract_method_name(stmt)
                        if method_name:
                            # Include class name prefix for node_id
                            full_method_name = f"{class_name}.{method_name}"
                            methods.append((stmt, full_method_name, "method"))

        return methods

    def _extract_method_name(self, node) -> Optional[str]:
        """
        Extract method name from Python function_definition node within a class.

        Args:
            node: AST node representing a Python method definition

        Returns:
            Method name or None if extraction failed
        """
        # Method name extraction is the same as function name extraction
        return self._extract_function_name(node)

    def _get_child_definitions(self, node, def_type: str):
        """Return class methods for class skeletons."""
        if def_type != "class":
            return []

        target_node = node
        if node.type == "decorated_definition":
            target_node = self._extract_definition_from_decorated(node)
        if not target_node:
            return []
        return self._find_class_methods(target_node)
