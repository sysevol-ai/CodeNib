# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rust-specific incremental patcher."""

from __future__ import annotations

import re

from ...types import NODE_TYPE_FUNCTION, NODE_TYPE_METHOD
from .patcher_base import PatcherBase

_CFG_TEST = re.compile(r"#\[\s*cfg\s*\(\s*(?:all\s*\(\s*)?test\b")


class PatcherRust(PatcherBase):
    """Rust incremental patcher. Matches SCIPRustGraphDecoder naming."""

    REGISTRY_LANGUAGE = "rust"

    def _get_crossfile_token_types(self):
        return {
            "type",
            "struct",
            "enum",
            "function",
            "method",
            "namespace",
            "macro",
            "interface",
            "property",
        }

    def _graph_symbols(self, file_path, symbols):
        """Drop modules compiled only for tests.

        rust-analyzer lists the contents of ``#[cfg(test)] mod tests`` as
        document symbols. The index the graph is built from leaves code under
        ``cfg(test)`` out, so those functions are no vertices of a fresh
        graph. Patching byteorder's lib.rs added 14 vertices and, through the
        references to them, 673 edges.
        """

        try:
            lines = (
                (self.project_root / file_path)
                .read_text(encoding="utf-8", errors="replace")
                .splitlines()
            )
        except OSError:
            return symbols
        return self._without_test_modules(symbols, lines)

    def _without_test_modules(self, symbols, lines):
        kept = []
        for symbol in symbols or []:
            start = symbol.get("range", {}).get("start", {}).get("line", 0)
            # The attribute sits on the module's first lines or just above.
            window = lines[max(0, start - 3) : start + 1]
            if symbol.get("kind") == 2 and any(
                _CFG_TEST.search(line) for line in window
            ):
                continue
            children = symbol.get("children")
            if children:
                symbol = {
                    **symbol,
                    "children": self._without_test_modules(children, lines),
                }
            kept.append(symbol)
        return kept

    # ── Unified name construction ──────────────────────────

    @staticmethod
    def _normalize_impl_name(impl_name: str) -> str:
        """Convert LSP impl block name to SCIP-compatible type display.

        "impl EqHash"                        → "EqHash"
        "impl Violation for EqWithoutHash"   → "EqWithoutHash<Violation>"
        "impl<'a> From<io::Error> for Error" → "Error<From<io::Error>>"
        """
        text = impl_name.strip()
        if not text.startswith("impl"):
            return text
        text = text[4:].strip()

        # Strip leading lifetime/generic params
        if text.startswith("<"):
            depth = 0
            for i, ch in enumerate(text):
                if ch == "<":
                    depth += 1
                elif ch == ">":
                    depth -= 1
                    if depth == 0:
                        text = text[i + 1 :].strip()
                        break

        for_idx = _find_toplevel_for(text)
        if for_idx >= 0:
            trait_part = text[:for_idx].strip()
            type_part = text[for_idx + 4 :].strip()
            trait_part = re.sub(r"<['\\a-z_,\s]+>", "", trait_part)
            if trait_part:
                return f"{type_part}<{trait_part}>"
            return type_part
        else:
            text = re.sub(r"<['\\a-z_,\s]+>", "", text)
            return text

    def _build_unified_name(self, file_path, name, parent_unified_part, kind):
        node_type = self._classify_symbol_type(kind)

        if parent_unified_part and parent_unified_part.startswith("impl"):
            parent_unified_part = self._normalize_impl_name(parent_unified_part)

        if name.startswith("impl"):
            name = self._normalize_impl_name(name)

        if parent_unified_part:
            display = f"{parent_unified_part}.{name}"
        else:
            display = name

        if node_type in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD):
            if not display.endswith("()"):
                display = f"{display}()"

        return f"{file_path}:{display}"

    # ── Symbol flattening ──────────────────────────────────

    def flatten_symbols(self, file_path, lsp_symbols):
        return self._flatten_symbols_default(file_path, lsp_symbols)

    # ── Rust doesn't override get_old_symbols ──────────────


def _find_toplevel_for(text: str) -> int:
    """Find start index of top-level 'for ' keyword (not inside <>)."""
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif depth == 0 and text[i : i + 5] == " for ":
            return i + 1
        i += 1
    return -1
