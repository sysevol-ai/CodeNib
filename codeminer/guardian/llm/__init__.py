# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Guardian-local LLM transport adapters and shared loop mechanics."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from .loop import AgentLoopSession, ContextManager, LoopOutcome


@contextmanager
def agent_loop_session(llm: object) -> Generator[object, None, None]:
    """Compatibility wrapper around :class:`AgentLoopSession`.

    L2 and every L3 investigation call this independently.  Transports without
    conversational sessions remain compatible and simply receive the original
    adapter.
    """

    with AgentLoopSession(llm) as session:
        yield session


__all__ = [
    "AgentLoopSession",
    "ContextManager",
    "LoopOutcome",
    "agent_loop_session",
]
