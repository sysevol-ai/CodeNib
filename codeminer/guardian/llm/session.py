# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Provider-session ownership for one Guardian agent."""

from contextlib import AbstractContextManager


class AgentLoopSession(AbstractContextManager):
    """Own exactly one transport session for exactly one model agent."""

    def __init__(self, llm: object) -> None:
        self.llm = llm
        self.session: object = llm

    def __enter__(self) -> object:
        # Inspect the class so mocks/proxies cannot fabricate this attribute.
        factory = getattr(type(self.llm), "start_agent_loop", None)
        self.session = factory(self.llm) if callable(factory) else self.llm
        return self.session

    def reset(self) -> None:
        """Reset provider-side history after local transcript compaction."""

        reset = getattr(self.session, "reset", None)
        if callable(reset):
            reset()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.session is not self.llm:
            close = getattr(self.session, "close", None)
            if callable(close):
                close()
