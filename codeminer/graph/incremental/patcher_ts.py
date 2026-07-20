# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""TypeScript/JavaScript-specific incremental patcher."""

from __future__ import annotations

from ...types import NODE_TYPE_FUNCTION, NODE_TYPE_METHOD
from . import change_mgr
from .patcher_base import PatcherBase


class PatcherTS(PatcherBase):
    """TS/JS incremental patcher. Matches SCIPTypeScriptGraphDecoder naming."""

    REGISTRY_LANGUAGE = "ts"

    def _file_level_rebuild_reason(self, file_path, old_symbols, new_symbols, hunks):
        """Reject non-injective TypeScript symbol identities.

        tsserver can emit callbacks, object properties, and overloads with the
        same hierarchical name. Line numbers keep the full-graph vertices
        distinct, but symbol-level invalidation cannot safely infer which
        occurrence survived an edit. Rebuild that document instead of
        silently coalescing declarations.
        """

        base_reason = super()._file_level_rebuild_reason(
            file_path, old_symbols, new_symbols, hunks
        )
        if base_reason:
            return base_reason

        old_locations = {}
        for vertex_id in self.code_graph.file_to_vertices.get(file_path, set()):
            attrs = self.code_graph.graph.vs[vertex_id].attributes()
            unified_name = attrs.get("unified_name")
            if not unified_name or attrs.get("start_line") is None:
                continue
            old_locations.setdefault(unified_name, []).append(attrs["start_line"])

        for lines in old_locations.values():
            if len(lines) < 2:
                continue
            if any(
                change_mgr.map_old_line_to_new(line, hunks) != line for line in lines
            ):
                return "duplicate hierarchical symbol identities cross an edited hunk"

        def intersects_new_hunk(line):
            return any(
                hunk.new_count
                and hunk.new_start <= line < hunk.new_start + hunk.new_count
                for hunk in hunks
            )

        for lines in self._flattened_symbol_collision_lines.values():
            if len(lines) > 1 and any(intersects_new_hunk(line) for line in lines):
                return "an edited hunk introduces duplicate hierarchical identities"
        return None

    def _get_crossfile_token_types(self):
        return {
            "type",
            "class",
            "enum",
            "function",
            "method",
            "namespace",
            "interface",
            "variable",
            "property",
        }

    def _build_unified_name(self, file_path, name, parent_unified_part, kind):
        if name in ("<constructor>", "constructor"):
            name = "constructor"

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
        return self._flatten_symbols_default(file_path, lsp_symbols)
