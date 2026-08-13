# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Failure-safe ownership helpers for vector-store consumers."""

from __future__ import annotations

from typing import Protocol

from ..._atomic_directory import _annotate_secondary_error


class _ClosableVectorStore(Protocol):
    def close(self) -> None: ...


def close_vector_after_failure(
    vector_store: _ClosableVectorStore,
    primary: BaseException,
) -> None:
    """Close an unpublished store with cancellation-safe failure priority.

    ``CodeVectorStore.close`` retains native indices that fail to reset, so the
    store itself is the retryable cleanup owner.  Keep it reachable from the
    original failure whenever cleanup does not complete, including when close
    is interrupted by ``KeyboardInterrupt`` or another ``BaseException``.
    """

    try:
        vector_store.close()
    except BaseException as cleanup_error:  # noqa: B036 - preserve primary
        try:
            _annotate_secondary_error(
                primary,
                "vector store cleanup also failed",
                cleanup_error,
            )
        except BaseException:  # noqa: B036 - notes must not replace primary
            pass
        preferred = (
            cleanup_error
            if isinstance(primary, Exception)
            and not isinstance(cleanup_error, Exception)
            else primary
        )
        try:
            preferred.vector_cleanup_owner = vector_store  # type: ignore[attr-defined]
        except BaseException:  # noqa: B036 - traceback still retains the owner
            pass
        if preferred is cleanup_error:
            raise cleanup_error from primary
        raise primary from cleanup_error


__all__ = ["close_vector_after_failure"]
