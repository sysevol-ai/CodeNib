# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic text bounds for remote embedding requests."""

REMOTE_EMBEDDING_DOCUMENT_MAX_CHARS = 16_000
_OMISSION_MARKER = "\n\n[CodeNib: middle omitted for embedding]\n\n"


def bounded_remote_embedding_document(
    text: str,
    *,
    max_chars: int = REMOTE_EMBEDDING_DOCUMENT_MAX_CHARS,
) -> str:
    """Keep a stable head/tail view within a remote model's context budget."""

    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        raise ValueError("embedding max_input_chars must be an integer")
    if max_chars <= len(_OMISSION_MARKER):
        raise ValueError("embedding max_input_chars is too small")
    if len(text) <= max_chars:
        return text
    available = max_chars - len(_OMISSION_MARKER)
    head_chars = available * 3 // 4
    tail_chars = available - head_chars
    return text[:head_chars] + _OMISSION_MARKER + text[-tail_chars:]
