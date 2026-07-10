# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Pinned policy constants for the CodeMiner Base native-LSP study."""

DEFAULT_BASE_DATASET = "fishmingyu/codeminer-base-dataset"
DEFAULT_BASE_REVISION = "4eb84e2e8918474969ce68c5b06facf14d6be604"
DEFAULT_MODEL = "vertex_ai/claude-haiku-4-5"

LANGUAGE_GROUPS = {
    "Go": "go",
    "Rust": "rust",
    "TypeScript/JavaScript": "typescript",
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
    "LANGUAGE_GROUPS",
]
