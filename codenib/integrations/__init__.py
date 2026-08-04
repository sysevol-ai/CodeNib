# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Compatibility providers for external coding-agent runtimes."""

from ._repository import (
    IntegrationCapabilityError,
    RepositoryAdapter,
    RepositoryEntity,
    RepositoryPathError,
)
from .swe_explore import (
    CodeNibSWEExploreExplorer,
    SWEExploreContextRegion,
    SWEExploreResult,
)

__all__ = [
    "CodeNibSWEExploreExplorer",
    "IntegrationCapabilityError",
    "RepositoryAdapter",
    "RepositoryEntity",
    "RepositoryPathError",
    "SWEExploreContextRegion",
    "SWEExploreResult",
]
