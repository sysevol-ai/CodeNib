# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""The drafter interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from codenib.serving.types import DraftTree, TokenId


class Drafter(ABC):
    """Proposes candidate continuations for the target model to verify.

    Implementations must be cheap relative to a target-model forward pass — a
    drafter that costs more than the tokens it saves is a net loss. ``draft`` is
    called once per decoding step with the tokens generated so far.
    """

    #: Provenance label attached to nodes this drafter produces.
    source: str = "drafter"

    def begin_run(self) -> None:
        """Reset request-scoped state before a new generation begins."""
        return None

    @abstractmethod
    def draft(self, context: Sequence[TokenId], max_tokens: int) -> DraftTree:
        """Return a draft tree of at most ``max_tokens`` candidate positions.

        Args:
            context: token ids generated so far (prompt + decoded suffix).
            max_tokens: speculation budget — the verification tree should not
                exceed this many non-root nodes.

        Returns an empty tree when the drafter has no confident proposal; the
        caller then falls back to other sources or plain decoding.
        """
        raise NotImplementedError
