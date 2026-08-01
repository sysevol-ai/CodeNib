# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""TypeScript/JavaScript-specific incremental patcher."""

from __future__ import annotations

from pathlib import Path

from ...scip_interface.typescript_semantics import typescript_static_module_fingerprint
from ...types import NODE_TYPE_FUNCTION, NODE_TYPE_METHOD
from . import change_mgr
from .patcher_base import IncrementalPatchRebuildRequired, PatcherBase


class PatcherTS(PatcherBase):
    """TS/JS incremental patcher. Matches SCIPTypeScriptGraphDecoder naming."""

    REGISTRY_LANGUAGE = "ts"

    def _validate_patch_contract(
        self, changed_files, earlier_commit, later_commit
    ) -> None:
        # The LSP patcher repairs definitions and references, but does not
        # resolve schema-v5 file-to-file import edges. Fall back before any
        # mutation whenever those edges may differ from a fresh SCIP graph.
        if changed_files["added"]:
            raise IncrementalPatchRebuildRequired(
                "TypeScript file additions require import-edge reindexing"
            )
        if changed_files["renamed"]:
            raise IncrementalPatchRebuildRequired(
                "TypeScript file renames require import-edge reindexing"
            )
        if not changed_files["modified"]:
            return
        if not earlier_commit:
            raise IncrementalPatchRebuildRequired(
                "TypeScript import validation requires the indexed commit"
            )

        for file_path in changed_files["modified"]:
            before = change_mgr.read_file_at_revision(
                str(self.project_root), earlier_commit, file_path
            )
            after = change_mgr.read_file_at_revision(
                str(self.project_root), later_commit, file_path
            )
            if before is None or after is None:
                raise IncrementalPatchRebuildRequired(
                    f"cannot validate TypeScript imports for {file_path}"
                )
            suffix = Path(file_path).suffix
            try:
                before_fingerprint = typescript_static_module_fingerprint(
                    before, suffix
                )
                after_fingerprint = typescript_static_module_fingerprint(after, suffix)
            except (ImportError, RuntimeError, TypeError, ValueError) as exc:
                raise IncrementalPatchRebuildRequired(
                    f"cannot parse TypeScript imports for {file_path}"
                ) from exc
            if before_fingerprint != after_fingerprint:
                raise IncrementalPatchRebuildRequired(
                    f"TypeScript imports changed in {file_path}"
                )

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
