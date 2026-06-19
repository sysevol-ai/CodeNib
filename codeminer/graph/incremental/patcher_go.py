# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Go-specific incremental patcher."""

from __future__ import annotations

import re

from ...types import NODE_TYPE_FUNCTION, NODE_TYPE_METHOD
from .patcher_base import PatcherBase


class PatcherGo(PatcherBase):
    """Go incremental patcher. Matches SCIPGoGraphDecoder naming."""

    REGISTRY_LANGUAGE = "go"

    def _should_skip_path(self, path: str) -> bool:
        """SCIP-go skips ``*_test.go`` files (see scip_decode_go.py:92).
        Match that so patcher graphs and rebuild graphs share the same
        file scope."""
        return path.endswith("_test.go")

    def _get_crossfile_token_types(self):
        return {
            "type",
            "function",
            "method",
            "namespace",
            "interface",
            "property",
        }

    def _build_unified_name(self, file_path, name, parent_unified_part, kind):
        node_type = self._classify_symbol_type(kind)

        # gopls returns method names as "(*ReceiverType).MethodName"
        # Strip parens and leading * to match SCIP format "Type.Method()"
        m = re.match(r"^\(\*?([^)]+)\)\.(.+)$", name)
        if m:
            display = f"{m.group(1)}.{m.group(2)}"
        elif parent_unified_part:
            display = f"{parent_unified_part}.{name}"
        else:
            display = name

        if node_type in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD):
            if not display.endswith("()"):
                display = f"{display}()"

        return f"{file_path}:{display}"

    def flatten_symbols(self, file_path, lsp_symbols):
        return self._flatten_symbols_default(file_path, lsp_symbols)
