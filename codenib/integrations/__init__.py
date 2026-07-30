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

__all__ = [
    "IntegrationCapabilityError",
    "RepositoryAdapter",
    "RepositoryEntity",
    "RepositoryPathError",
]
