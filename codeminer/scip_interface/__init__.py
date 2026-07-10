# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
SCIP Interface Module

Language-specific SCIP indexing and decoding for active and candidate
cold-start routes.
"""

from .lsp_occurrence_index import (
    LSP_OCCURRENCE_INDEX_SCHEMA_VERSION,
    SCIPLocation,
    SCIPOccurrence,
    SCIPOccurrenceIndex,
)
from .scip_decode_csharp import SCIPCSharpGraphDecoder
from .scip_decode_java import SCIPJavaGraphDecoder
from .scip_decode_php import SCIPPHPGraphDecoder
from .scip_decode_python import SCIPPythonGraphDecoder
from .scip_decode_ruby import SCIPRubyGraphDecoder
from .scip_decode_rust import SCIPRustGraphDecoder
from .scip_decode_ts import SCIPTypeScriptGraphDecoder
from .scip_indexer_base import SCIPIndexerBase
from .scip_indexer_csharp import SCIPCSharpIndexer
from .scip_indexer_java import SCIPJavaIndexer, SCIPKotlinIndexer, SCIPScalaIndexer
from .scip_indexer_php import PHPHybridIndexer, SCIPPHPIndexer
from .scip_indexer_python import SCIPPythonIndexer
from .scip_indexer_ruby import RubyHybridIndexer, SCIPRubyIndexer
from .scip_indexer_rust import SCIPRustIndexer
from .scip_indexer_ts import SCIPTypeScriptIndexer

__all__ = [
    "SCIPIndexerBase",
    "SCIPPythonIndexer",
    "SCIPRustIndexer",
    "SCIPTypeScriptIndexer",
    "SCIPCSharpIndexer",
    "SCIPJavaIndexer",
    "SCIPKotlinIndexer",
    "SCIPScalaIndexer",
    "RubyHybridIndexer",
    "SCIPRubyIndexer",
    "SCIPPHPIndexer",
    "PHPHybridIndexer",
    "SCIPJavaGraphDecoder",
    "SCIPCSharpGraphDecoder",
    "SCIPRubyGraphDecoder",
    "SCIPPHPGraphDecoder",
    "SCIPPythonGraphDecoder",
    "SCIPRustGraphDecoder",
    "SCIPTypeScriptGraphDecoder",
    "LSP_OCCURRENCE_INDEX_SCHEMA_VERSION",
    "SCIPLocation",
    "SCIPOccurrence",
    "SCIPOccurrenceIndex",
]
