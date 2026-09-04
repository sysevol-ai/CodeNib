# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Source-checkout-only hybrid index persistence experiment.

Nothing in this package is part of the installed :mod:`codenib` API.  The
stable :mod:`codenib.storage` namespace remains the Wiki-only database facade.
"""

from .repository import IndexRepository, PublicationResult

__all__ = ["IndexRepository", "PublicationResult"]
