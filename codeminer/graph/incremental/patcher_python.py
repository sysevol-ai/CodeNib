# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Python-specific incremental patcher."""

from __future__ import annotations

from ...types import NODE_TYPE_FUNCTION, NODE_TYPE_METHOD
from .patcher_base import PatcherBase


class PatcherPython(PatcherBase):
    """Python incremental patcher. Matches SCIPPythonGraphDecoder naming."""

    REGISTRY_LANGUAGE = "python"

    def _reference_scope_for_token(self, file_path, token, preferred_scope):
        scope = super()._reference_scope_for_token(file_path, token, preferred_scope)
        if scope is None:
            return None
        if not self.respect_backend_exclusions:
            # The generic LSP decoder assigns decorator references to the
            # innermost documentSymbol range. Preserve that provider contract
            # when an LSP-built graph is the incremental baseline.
            return scope

        source_path = self.project_root / file_path
        try:
            stat = source_path.stat()
            cache_key = (file_path, stat.st_mtime_ns, stat.st_size)
            cache = getattr(self, "_python_source_lines_cache", None)
            if cache is None or cache[0] != cache_key:
                cache = (cache_key, source_path.read_text().splitlines())
                self._python_source_lines_cache = cache
            lines = cache[1]
            line = lines[token["line"]]
        except (OSError, UnicodeError, IndexError):
            return scope

        if line.lstrip().startswith("@"):
            return self._containment_parent(scope) or scope
        return scope

    def _get_crossfile_token_types(self):
        return {
            "type",
            "class",
            "function",
            "method",
            "namespace",
            "property",
            "variable",
        }

    def _build_unified_name(self, file_path, name, parent_unified_part, kind):
        node_type = self._classify_symbol_type(kind)

        if parent_unified_part:
            display = f"{parent_unified_part}.{name}"
        else:
            display = name

        if node_type in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD):
            if not display.endswith("()"):
                display = f"{display}()"

        return f"{file_path}:{display}"

    def flatten_symbols(self, file_path, lsp_symbols):
        flattened = self._flatten_symbols_default(file_path, lsp_symbols)
        canonical = {}
        for unified_name, metadata in flattened.items():
            parent_name = metadata.get("parent_uname")
            parent = (
                flattened.get(f"{file_path}:{parent_name}") if parent_name else None
            )
            node_type = self._classify_symbol_type(metadata["kind"])
            parent_type = self._classify_symbol_type(parent["kind"]) if parent else None
            if (
                node_type == NODE_TYPE_FUNCTION
                and parent_type == NODE_TYPE_METHOD
                and parent.get("parent_uname")
            ):
                leaf = unified_name.rsplit(".", 1)[-1].removesuffix("()")
                metadata = {
                    **metadata,
                    "kind": 6,
                    "parent_uname": parent["parent_uname"],
                }
                unified_name = self._build_unified_name(
                    file_path,
                    leaf,
                    parent["parent_uname"],
                    metadata["kind"],
                )

            existing = canonical.get(unified_name)
            if existing is None or self._prefer_duplicate_symbol(existing, metadata):
                canonical[unified_name] = metadata
        return canonical

    def _prefer_duplicate_symbol(self, existing: dict, candidate: dict) -> bool:
        existing_type = self._classify_symbol_type(existing["kind"])
        candidate_type = self._classify_symbol_type(candidate["kind"])
        callable_types = {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}
        if existing_type in callable_types and candidate_type in callable_types:
            return candidate["start_line"] < existing["start_line"]
        return super()._prefer_duplicate_symbol(existing, candidate)
