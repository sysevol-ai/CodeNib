# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Pinned policy constants for the CodeNib Base native-LSP study."""

from codenib.dataset_ids import CODENIB_BASE_DATASET

DEFAULT_BASE_DATASET = CODENIB_BASE_DATASET
DEFAULT_BASE_REVISION = "4eb84e2e8918474969ce68c5b06facf14d6be604"
DEFAULT_MODEL = "vertex_ai/claude-haiku-4-5"

PRIMARY_LANGUAGE_GROUPS = {
    "Go": "go",
    "Rust": "rust",
    "TypeScript/JavaScript": "typescript",
}

LANGUAGE_EXTENSION_GROUPS = {
    "C++/C": "cpp",
    "Python": "python",
}

LANGUAGE_GROUPS = {**PRIMARY_LANGUAGE_GROUPS, **LANGUAGE_EXTENSION_GROUPS}

LANGUAGE_EXTENSION_ROLE = "language_extension"

STATIC_NATIVE_BACKENDS = {
    "cpp": "symbol_graph_position_v1",
    "go": "scip_occurrence_index_v1",
    "python": "scip_occurrence_index_v1",
    "rust": "scip_occurrence_index_v1",
    "typescript": "scip_occurrence_index_v1",
}

# These repositories supplied the exploratory replay evidence. Their complete
# clusters stay outside the confirmatory partition.
DEFAULT_DEVELOPMENT_REPOSITORIES = (
    "caddyserver/caddy",
    "gin-gonic/gin",
    "preactjs/preact",
    "sharkdp/bat",
    "tokio-rs/tokio",
    "vuejs/core",
)

__all__ = [
    "DEFAULT_BASE_DATASET",
    "DEFAULT_BASE_REVISION",
    "DEFAULT_DEVELOPMENT_REPOSITORIES",
    "DEFAULT_MODEL",
    "LANGUAGE_EXTENSION_GROUPS",
    "LANGUAGE_EXTENSION_ROLE",
    "LANGUAGE_GROUPS",
    "PRIMARY_LANGUAGE_GROUPS",
    "STATIC_NATIVE_BACKENDS",
]
