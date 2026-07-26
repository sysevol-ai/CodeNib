# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Guardian-local LLM transport adapters."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator


@contextmanager
def agent_loop_session(llm: object) -> Generator[object, None, None]:
    """Open one transport session for exactly one model-owned agent loop.

    L2 and every L3 investigation call this independently.  Transports without
    conversational sessions remain compatible and simply receive the original
    adapter.
    """

    # Inspect the class so dynamic mocks/proxies do not accidentally fabricate
    # a ``start_agent_loop`` attribute and swallow the real model call.
    factory = getattr(type(llm), "start_agent_loop", None)
    session = factory(llm) if callable(factory) else llm
    try:
        yield session
    finally:
        if session is not llm:
            close = getattr(session, "close", None)
            if callable(close):
                close()
