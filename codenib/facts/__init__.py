# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Provider-neutral, per-file semantic fact contracts."""

from .adapters import fact_batch_from_scip_occurrences, fact_batches_from_code_graph
from .model import (
    CAPABILITY_DEFINITIONS,
    CAPABILITY_DIAGNOSTICS,
    CAPABILITY_EDGES,
    CAPABILITY_OCCURRENCES,
    COMPLETENESS_COMPLETE,
    COMPLETENESS_FAILED,
    COMPLETENESS_PARTIAL,
    COMPLETENESS_SYNTACTIC,
    FACT_BATCH_SCHEMA_VERSION,
    PROVENANCE_CLANGD,
    PROVENANCE_FRAMEWORK,
    PROVENANCE_HEURISTIC,
    PROVENANCE_LSP,
    PROVENANCE_SCIP,
    PROVENANCE_SYNTACTIC,
    DiagnosticFact,
    EdgeFact,
    FactBatch,
    OccurrenceFact,
    SourceRange,
    SymbolFact,
    merge_fact_batches,
    sha256_digest,
)

__all__ = [
    "CAPABILITY_DEFINITIONS",
    "CAPABILITY_DIAGNOSTICS",
    "CAPABILITY_EDGES",
    "CAPABILITY_OCCURRENCES",
    "COMPLETENESS_COMPLETE",
    "COMPLETENESS_FAILED",
    "COMPLETENESS_PARTIAL",
    "COMPLETENESS_SYNTACTIC",
    "DiagnosticFact",
    "EdgeFact",
    "FACT_BATCH_SCHEMA_VERSION",
    "FactBatch",
    "OccurrenceFact",
    "PROVENANCE_CLANGD",
    "PROVENANCE_FRAMEWORK",
    "PROVENANCE_HEURISTIC",
    "PROVENANCE_LSP",
    "PROVENANCE_SCIP",
    "PROVENANCE_SYNTACTIC",
    "SourceRange",
    "SymbolFact",
    "fact_batch_from_scip_occurrences",
    "fact_batches_from_code_graph",
    "merge_fact_batches",
    "sha256_digest",
]
