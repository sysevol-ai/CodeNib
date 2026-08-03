# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Token-budgeted chat history for the agent loop (issue #109, phase 4).

The agent loop in :mod:`codenib.agent.runner` appends every assistant
turn and every tool result to a flat ``messages`` list. On long runs that
list grows without bound and eventually overflows the model's context
window.

:class:`TokenBudgetedChatHistory` is a drop-in container that caps the
*total* context tokens. When adding a message would exceed the budget it
evicts the **oldest non-system messages first**, always preserving:

* every ``system`` message (the system prompt — pinned, never evicted), and
* the most recent messages (the live working set the model needs to act).

It is deliberately a plain container (``add_message`` / ``get_messages`` /
``clear``) so the runner can swap a bare ``list`` for it with no other
change. Token counting uses ``litellm.token_counter`` when available and
falls back to a cheap character-based heuristic so the class stays usable
in unit tests and offline environments.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..log_utils import get_logger

logger = get_logger(__name__)

# Rough chars-per-token used by the offline heuristic. ~4 chars/token is the
# conventional English-text approximation; intentionally conservative so the
# fallback over-counts slightly rather than under-counts (eviction is safer
# than overflow).
_CHARS_PER_TOKEN = 4


def _message_text(message: Dict[str, Any]) -> str:
    """Flatten a chat message dict into the text we count tokens for.

    Covers the three shapes the runner produces: a plain ``content`` string,
    ``content=None`` with ``tool_calls`` (assistant tool requests), and tool
    result messages. Best-effort — anything unrecognized is ``str()``-ified.
    """
    parts: List[str] = []
    content = message.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif content is not None:
        parts.append(str(content))

    for tc in message.get("tool_calls") or []:
        fn = (tc or {}).get("function") if isinstance(tc, dict) else None
        if isinstance(fn, dict):
            parts.append(str(fn.get("name", "")))
            parts.append(str(fn.get("arguments", "")))
    return "\n".join(p for p in parts if p)


def count_message_tokens(
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
) -> int:
    """Best-effort token count for a list of chat messages.

    Uses ``litellm.token_counter`` (which understands the model's real
    tokenizer and per-message role overhead) when a ``model`` is supplied
    and litellm is importable. Falls back to a ``chars / 4`` heuristic
    otherwise, so this never raises and stays usable in offline tests.
    """
    if not messages:
        return 0
    if model:
        try:
            import litellm

            return int(litellm.token_counter(model=model, messages=messages))
        except Exception as exc:  # offline / unknown model / litellm bug
            logger.debug("token_counter unavailable (%s); using char heuristic", exc)

    chars = sum(len(_message_text(m)) for m in messages)
    # +4 per message approximates the role / formatting overhead litellm adds.
    return chars // _CHARS_PER_TOKEN + 4 * len(messages)


class TokenBudgetedChatHistory:
    """Chat-history container that caps total context tokens.

    Pinned ``system`` messages are always retained. When appending a message
    would push the estimated token total above ``max_tokens``, the oldest
    *non-system* messages are evicted one at a time (oldest first) until the
    history fits again, so the system prompt plus the most recent turns are
    preserved.

    ``keep_last`` guards the tail: the most recent ``keep_last`` non-system
    messages are never evicted even under budget pressure, so the model
    always sees the immediate context it needs to act. A single message that
    on its own exceeds the budget is kept (we never drop the message just
    added) and a warning is logged.

    The container is intentionally minimal — ``add_message`` /
    ``get_messages`` / ``clear`` / ``__len__`` / ``__iter__`` — so the runner
    can use it in place of a bare ``list``.
    """

    def __init__(
        self,
        max_tokens: int,
        *,
        model: Optional[str] = None,
        keep_last: int = 2,
    ) -> None:
        if max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got {max_tokens}")
        self.max_tokens = max_tokens
        self.model = model
        self.keep_last = max(0, keep_last)
        self._messages: List[Dict[str, Any]] = []

    # -- container protocol -------------------------------------------------

    def __len__(self) -> int:
        return len(self._messages)

    def __iter__(self):
        return iter(self._messages)

    def get_messages(self) -> List[Dict[str, Any]]:
        """Return the live message list (the object the LLM is called with)."""
        return self._messages

    def clear(self) -> None:
        """Drop all messages."""
        self._messages.clear()

    # -- mutation -----------------------------------------------------------

    def add_message(self, message: Dict[str, Any]) -> None:
        """Append *message*, then evict oldest non-system messages if over budget."""
        self._messages.append(message)
        self._enforce_budget()

    def extend(self, messages: List[Dict[str, Any]]) -> None:
        """Append several messages, enforcing the budget after each one."""
        for message in messages:
            self.add_message(message)

    def total_tokens(self) -> int:
        """Estimated token total of the current history."""
        return count_message_tokens(self._messages, model=self.model)

    # -- internals ----------------------------------------------------------

    def _enforce_budget(self) -> None:
        """Evict oldest non-system messages until within ``max_tokens``.

        Walks from the front, skipping pinned ``system`` messages and the
        protected ``keep_last`` tail, dropping the oldest eligible message
        each pass until the total fits or nothing more is evictable.
        """
        while self.total_tokens() > self.max_tokens:
            idx = self._oldest_evictable_index()
            if idx is None:
                # Only system + protected tail remain (or the single message
                # is itself over budget). Keep what we have; the budget is a
                # best-effort cap, not a hard guarantee against a giant turn.
                logger.warning(
                    "TokenBudgetedChatHistory: %d tokens still over budget %d "
                    "with only system + last %d message(s) left; not evicting "
                    "further",
                    self.total_tokens(),
                    self.max_tokens,
                    self.keep_last,
                )
                return
            evicted = self._messages.pop(idx)
            logger.debug(
                "TokenBudgetedChatHistory: evicted oldest %s message to stay "
                "within %d-token budget",
                evicted.get("role", "?"),
                self.max_tokens,
            )

    def _oldest_evictable_index(self) -> Optional[int]:
        """Index of the oldest message that may be evicted, or ``None``.

        A message is evictable when it is non-system and not inside the
        protected ``keep_last`` tail of non-system messages.
        """
        n = len(self._messages)
        # The last ``keep_last`` *non-system* messages are pinned (the live
        # working set the model needs to act). Everything older than that and
        # non-system is evictable, oldest first.
        non_system_positions = [
            i for i, m in enumerate(self._messages) if m.get("role") != "system"
        ]
        if self.keep_last:
            protected = set(non_system_positions[-self.keep_last :])
        else:
            protected = set()
        for i in range(n):
            m = self._messages[i]
            if m.get("role") == "system":
                continue
            if i in protected:
                continue
            return i
        return None


class PlainChatHistory:
    """Unbounded chat history with the same interface as the budgeted one.

    Lets :class:`~codenib.agent.runner.AgentRunner` use a single code path
    whether or not a token budget is configured. Behaviour is identical to a
    bare ``list`` of message dicts — nothing is ever evicted.
    """

    def __init__(self, messages: Optional[List[Dict[str, Any]]] = None) -> None:
        self._messages: List[Dict[str, Any]] = list(messages or [])

    def __len__(self) -> int:
        return len(self._messages)

    def __iter__(self):
        return iter(self._messages)

    def get_messages(self) -> List[Dict[str, Any]]:
        return self._messages

    def add_message(self, message: Dict[str, Any]) -> None:
        self._messages.append(message)

    def extend(self, messages: List[Dict[str, Any]]) -> None:
        self._messages.extend(messages)

    def clear(self) -> None:
        self._messages.clear()
