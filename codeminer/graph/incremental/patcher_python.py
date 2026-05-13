# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Python-specific incremental patcher."""

from __future__ import annotations

from ...types import NODE_TYPE_FUNCTION, NODE_TYPE_METHOD
from .patcher_base import PatcherBase


class PatcherPython(PatcherBase):
    """Python incremental patcher. Matches SCIPPythonGraphDecoder naming."""

    def get_lsp_command(self):
        # ty 0.0.33 is faster on small queries but hangs on some larger
        # workspaces (observed on xarray bin=300, 36min+ no response).
        # basedpyright is slower but reliable; default to it. Override via
        # ``CODEMINER_PYTHON_LSP_CMD=ty\ server`` to experiment with ty.
        import os

        cmd = os.environ.get("CODEMINER_PYTHON_LSP_CMD")
        if cmd:
            return cmd.split()
        return ["basedpyright-langserver", "--stdio"]

    def _language_id(self):
        return "python"

    def _get_crossfile_token_types(self):
        return {
            "type",
            "class",
            "function",
            "method",
            "namespace",
            "property",
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
        return self._flatten_symbols_default(file_path, lsp_symbols)
