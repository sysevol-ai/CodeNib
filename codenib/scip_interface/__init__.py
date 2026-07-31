# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Language-specific SCIP indexing and decoding.

The public classes stay lazy so importing an LSP-shaped provider does not load
the optional igraph runtime or every language backend.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._lazy import exported_dir, load_export

if TYPE_CHECKING:  # pragma: no cover - imported only by static analyzers
    from codenib.scip_interface.lsp_occurrence_index import (
        LSP_OCCURRENCE_INDEX_SCHEMA_VERSION,
        SCIPLocation,
        SCIPOccurrence,
        SCIPOccurrenceIndex,
    )
    from codenib.scip_interface.scip_decode_csharp import SCIPCSharpGraphDecoder
    from codenib.scip_interface.scip_decode_java import SCIPJavaGraphDecoder
    from codenib.scip_interface.scip_decode_php import SCIPPHPGraphDecoder
    from codenib.scip_interface.scip_decode_python import SCIPPythonGraphDecoder
    from codenib.scip_interface.scip_decode_ruby import SCIPRubyGraphDecoder
    from codenib.scip_interface.scip_decode_rust import SCIPRustGraphDecoder
    from codenib.scip_interface.scip_decode_ts import SCIPTypeScriptGraphDecoder
    from codenib.scip_interface.scip_indexer_base import SCIPIndexerBase
    from codenib.scip_interface.scip_indexer_csharp import SCIPCSharpIndexer
    from codenib.scip_interface.scip_indexer_java import (
        SCIPJavaIndexer,
        SCIPKotlinIndexer,
        SCIPScalaIndexer,
    )
    from codenib.scip_interface.scip_indexer_php import PHPHybridIndexer, SCIPPHPIndexer
    from codenib.scip_interface.scip_indexer_python import SCIPPythonIndexer
    from codenib.scip_interface.scip_indexer_ruby import (
        RubyHybridIndexer,
        SCIPRubyIndexer,
    )
    from codenib.scip_interface.scip_indexer_rust import SCIPRustIndexer
    from codenib.scip_interface.scip_indexer_ts import SCIPTypeScriptIndexer

_EXPORTS = {
    "SCIPIndexerBase": (
        "codenib.scip_interface.scip_indexer_base",
        "SCIPIndexerBase",
    ),
    "SCIPPythonIndexer": (
        "codenib.scip_interface.scip_indexer_python",
        "SCIPPythonIndexer",
    ),
    "SCIPRustIndexer": (
        "codenib.scip_interface.scip_indexer_rust",
        "SCIPRustIndexer",
    ),
    "SCIPTypeScriptIndexer": (
        "codenib.scip_interface.scip_indexer_ts",
        "SCIPTypeScriptIndexer",
    ),
    "SCIPCSharpIndexer": (
        "codenib.scip_interface.scip_indexer_csharp",
        "SCIPCSharpIndexer",
    ),
    "SCIPJavaIndexer": (
        "codenib.scip_interface.scip_indexer_java",
        "SCIPJavaIndexer",
    ),
    "SCIPKotlinIndexer": (
        "codenib.scip_interface.scip_indexer_java",
        "SCIPKotlinIndexer",
    ),
    "SCIPScalaIndexer": (
        "codenib.scip_interface.scip_indexer_java",
        "SCIPScalaIndexer",
    ),
    "RubyHybridIndexer": (
        "codenib.scip_interface.scip_indexer_ruby",
        "RubyHybridIndexer",
    ),
    "SCIPRubyIndexer": (
        "codenib.scip_interface.scip_indexer_ruby",
        "SCIPRubyIndexer",
    ),
    "SCIPPHPIndexer": (
        "codenib.scip_interface.scip_indexer_php",
        "SCIPPHPIndexer",
    ),
    "PHPHybridIndexer": (
        "codenib.scip_interface.scip_indexer_php",
        "PHPHybridIndexer",
    ),
    "SCIPJavaGraphDecoder": (
        "codenib.scip_interface.scip_decode_java",
        "SCIPJavaGraphDecoder",
    ),
    "SCIPCSharpGraphDecoder": (
        "codenib.scip_interface.scip_decode_csharp",
        "SCIPCSharpGraphDecoder",
    ),
    "SCIPRubyGraphDecoder": (
        "codenib.scip_interface.scip_decode_ruby",
        "SCIPRubyGraphDecoder",
    ),
    "SCIPPHPGraphDecoder": (
        "codenib.scip_interface.scip_decode_php",
        "SCIPPHPGraphDecoder",
    ),
    "SCIPPythonGraphDecoder": (
        "codenib.scip_interface.scip_decode_python",
        "SCIPPythonGraphDecoder",
    ),
    "SCIPRustGraphDecoder": (
        "codenib.scip_interface.scip_decode_rust",
        "SCIPRustGraphDecoder",
    ),
    "SCIPTypeScriptGraphDecoder": (
        "codenib.scip_interface.scip_decode_ts",
        "SCIPTypeScriptGraphDecoder",
    ),
    "LSP_OCCURRENCE_INDEX_SCHEMA_VERSION": (
        "codenib.scip_interface.lsp_occurrence_index",
        "LSP_OCCURRENCE_INDEX_SCHEMA_VERSION",
    ),
    "SCIPLocation": (
        "codenib.scip_interface.lsp_occurrence_index",
        "SCIPLocation",
    ),
    "SCIPOccurrence": (
        "codenib.scip_interface.lsp_occurrence_index",
        "SCIPOccurrence",
    ),
    "SCIPOccurrenceIndex": (
        "codenib.scip_interface.lsp_occurrence_index",
        "SCIPOccurrenceIndex",
    ),
}

__all__ = [
    "LSP_OCCURRENCE_INDEX_SCHEMA_VERSION",
    "PHPHybridIndexer",
    "RubyHybridIndexer",
    "SCIPCSharpGraphDecoder",
    "SCIPCSharpIndexer",
    "SCIPIndexerBase",
    "SCIPJavaGraphDecoder",
    "SCIPJavaIndexer",
    "SCIPKotlinIndexer",
    "SCIPLocation",
    "SCIPOccurrence",
    "SCIPOccurrenceIndex",
    "SCIPPHPGraphDecoder",
    "SCIPPHPIndexer",
    "SCIPPythonGraphDecoder",
    "SCIPPythonIndexer",
    "SCIPRubyGraphDecoder",
    "SCIPRubyIndexer",
    "SCIPRustGraphDecoder",
    "SCIPRustIndexer",
    "SCIPScalaIndexer",
    "SCIPTypeScriptGraphDecoder",
    "SCIPTypeScriptIndexer",
]


def __getattr__(name: str) -> Any:
    return load_export(globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return exported_dir(globals(), _EXPORTS)
