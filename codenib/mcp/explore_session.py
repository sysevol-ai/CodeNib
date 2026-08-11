# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded, fingerprint-safe context deduplication for stdio MCP sessions."""

from __future__ import annotations

import asyncio
import copy
import hashlib
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Mapping

from .explore_bounds import MAX_EXPLORE_PAYLOAD_BYTES, bound_explore_response


@dataclass(frozen=True, slots=True)
class _SeenContext:
    content_fingerprint: str
    delivery_fingerprint: str
    first_seen_call: int
    last_seen_call: int


@dataclass(slots=True)
class _ContextRef:
    context: dict[str, Any]
    key: tuple[str, int, int]
    content_fingerprint: str
    delivery_fingerprint: str
    previous: _SeenContext | None
    repeated: bool


class ExploreSessionLedger:
    """Deduplicate only byte-identical, identically projected source ranges.

    Calls are committed only after response-budget projection. A range whose
    body was not actually delivered is therefore never recorded as seen.
    """

    def __init__(self, *, max_ranges: int = 160) -> None:
        if isinstance(max_ranges, bool) or not isinstance(max_ranges, int):
            raise ValueError("max_ranges must be a positive integer")
        if max_ranges < 1:
            raise ValueError("max_ranges must be a positive integer")
        self.max_ranges = max_ranges
        self._call_id = 0
        self._evictions = 0
        self._seen: OrderedDict[tuple[str, int, int], _SeenContext] = OrderedDict()
        self._lock = RLock()

    def reset(self) -> None:
        with self._lock:
            self._call_id = 0
            self._evictions = 0
            self._seen.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "calls": self._call_id,
                "ranges": len(self._seen),
                "max_ranges": self.max_ranges,
                "evictions": self._evictions,
            }

    def project(
        self,
        response: Mapping[str, Any],
        *,
        max_response_bytes: int = MAX_EXPLORE_PAYLOAD_BYTES,
    ) -> dict[str, Any]:
        """Return a bounded copy with safe repeats replaced by direct pointers."""

        result = copy.deepcopy(dict(response))
        with self._lock:
            call_id = self._call_id + 1
            previous_seen = OrderedDict(self._seen)
            contexts = self._context_refs(result, seen=previous_seen)
            eligible = [reference for reference in contexts if reference.repeated]
            retained: _ContextRef | None = None
            if eligible and len(eligible) == len(contexts):
                retained = eligible[0]

            for reference in contexts:
                if reference.repeated and reference is not retained:
                    self._replace_with_pointer(reference, call_id=call_id)
                else:
                    self._annotate_retained(reference, call_id=call_id)

            result = bound_explore_response(result, max_bytes=max_response_bytes)
            proposed = previous_seen
            evicted = 0
            for _ in range(8):
                self._reconcile_session_dedup(
                    result,
                    previous_seen=previous_seen,
                    call_id=call_id,
                )
                before = _delivery_signature(result)
                proposed, evicted = self._propose_seen(
                    result,
                    previous_seen=previous_seen,
                    call_id=call_id,
                )
                self._set_session_usage(
                    result,
                    call_id=call_id,
                    ranges=len(proposed),
                    evicted=evicted,
                )
                result = bound_explore_response(result, max_bytes=max_response_bytes)
                if _delivery_signature(result) == before:
                    break
            else:  # pragma: no cover - fixed public budget converges immediately
                raise RuntimeError("explore response projection did not converge")

            self._call_id = call_id
            self._seen = proposed
            self._evictions += evicted
        return result

    def _context_refs(
        self,
        response: Mapping[str, Any],
        *,
        seen: Mapping[tuple[str, int, int], _SeenContext],
    ) -> list[_ContextRef]:
        references: list[_ContextRef] = []
        for file_path, context in _iter_contexts(response):
            if context.get("verified") is not True:
                continue
            content = context.get("content")
            content_fingerprint = context.get("content_fingerprint")
            start_line = context.get("start_line")
            end_line = context.get("end_line")
            if (
                not isinstance(content, str)
                or not content
                or not isinstance(content_fingerprint, str)
                or isinstance(start_line, bool)
                or not isinstance(start_line, int)
                or isinstance(end_line, bool)
                or not isinstance(end_line, int)
            ):
                continue
            key = (file_path, start_line, end_line)
            delivery_fingerprint = _text_fingerprint(content)
            previous = seen.get(key)
            repeated = bool(
                previous
                and previous.content_fingerprint == content_fingerprint
                and previous.delivery_fingerprint == delivery_fingerprint
            )
            references.append(
                _ContextRef(
                    context=context,
                    key=key,
                    content_fingerprint=content_fingerprint,
                    delivery_fingerprint=delivery_fingerprint,
                    previous=previous,
                    repeated=repeated,
                )
            )
        return references

    def _reconcile_session_dedup(
        self,
        response: Mapping[str, Any],
        *,
        previous_seen: Mapping[tuple[str, int, int], _SeenContext],
        call_id: int,
    ) -> None:
        """Make annotations describe the bodies that survived projection."""

        for file_path, context in _iter_contexts(response):
            if context.get("verified") is not True:
                context.pop("session_dedup", None)
                continue
            start_line = context.get("start_line")
            end_line = context.get("end_line")
            content_fingerprint = context.get("content_fingerprint")
            if (
                isinstance(start_line, bool)
                or not isinstance(start_line, int)
                or isinstance(end_line, bool)
                or not isinstance(end_line, int)
                or not isinstance(content_fingerprint, str)
            ):
                context.pop("session_dedup", None)
                continue

            previous = previous_seen.get((file_path, start_line, end_line))
            content = context.get("content")
            if not isinstance(content, str) or not content:
                if context.get("content_kind") != "session_pointer":
                    context.pop("session_dedup", None)
                continue
            if previous is None:
                context.pop("session_dedup", None)
                continue

            delivery_fingerprint = _text_fingerprint(content)
            if (
                previous.content_fingerprint == content_fingerprint
                and previous.delivery_fingerprint == delivery_fingerprint
            ):
                context["session_dedup"] = {
                    "repeated": True,
                    "content_retained": True,
                    "source_call": previous.first_seen_call,
                    "first_seen_call": previous.first_seen_call,
                    "previous_call": previous.last_seen_call,
                    "current_call": call_id,
                }
            elif previous.content_fingerprint != content_fingerprint:
                context["session_dedup"] = {
                    "repeated": False,
                    "content_changed": True,
                    "source_call": call_id,
                    "first_seen_call": call_id,
                    "previous_call": previous.last_seen_call,
                    "current_call": call_id,
                }
            else:
                context["session_dedup"] = {
                    "repeated": False,
                    "projection_changed": True,
                    "source_call": call_id,
                    "first_seen_call": call_id,
                    "previous_call": previous.last_seen_call,
                    "current_call": call_id,
                }

    def _propose_seen(
        self,
        response: Mapping[str, Any],
        *,
        previous_seen: OrderedDict[tuple[str, int, int], _SeenContext],
        call_id: int,
    ) -> tuple[OrderedDict[tuple[str, int, int], _SeenContext], int]:
        proposed = OrderedDict(previous_seen)
        for file_path, context in _iter_contexts(response):
            if context.get("verified") is not True:
                continue
            start_line = context.get("start_line")
            end_line = context.get("end_line")
            content_fingerprint = context.get("content_fingerprint")
            if (
                isinstance(start_line, bool)
                or not isinstance(start_line, int)
                or isinstance(end_line, bool)
                or not isinstance(end_line, int)
                or not isinstance(content_fingerprint, str)
            ):
                continue
            key = (file_path, start_line, end_line)
            previous = previous_seen.get(key)
            content = context.get("content")
            if isinstance(content, str) and content:
                delivery_fingerprint = _text_fingerprint(content)
                repeated = bool(
                    previous
                    and previous.content_fingerprint == content_fingerprint
                    and previous.delivery_fingerprint == delivery_fingerprint
                )
                proposed[key] = _SeenContext(
                    content_fingerprint=content_fingerprint,
                    delivery_fingerprint=delivery_fingerprint,
                    first_seen_call=(
                        previous.first_seen_call if repeated and previous else call_id
                    ),
                    last_seen_call=call_id,
                )
                proposed.move_to_end(key)
                continue

            dedup = context.get("session_dedup")
            if (
                previous is not None
                and context.get("content_kind") == "session_pointer"
                and isinstance(dedup, Mapping)
                and dedup.get("repeated") is True
                and dedup.get("source_call") == previous.first_seen_call
            ):
                proposed[key] = _SeenContext(
                    content_fingerprint=previous.content_fingerprint,
                    delivery_fingerprint=previous.delivery_fingerprint,
                    first_seen_call=previous.first_seen_call,
                    last_seen_call=call_id,
                )
                proposed.move_to_end(key)

        evicted = 0
        while len(proposed) > self.max_ranges:
            proposed.popitem(last=False)
            evicted += 1
        return proposed, evicted

    def _set_session_usage(
        self,
        result: dict[str, Any],
        *,
        call_id: int,
        ranges: int,
        evicted: int,
    ) -> None:
        summary = result.setdefault("summary", {})
        if isinstance(summary, dict):
            summary["session_call"] = call_id
            summary["session_deduplicated_contexts"] = _session_pointer_count(result)
            summary["session_ranges"] = ranges
            summary["session_max_ranges"] = self.max_ranges
            summary["session_remaining_ranges"] = self.max_ranges - ranges
            summary["session_evicted_ranges"] = evicted
            summary["session_total_evictions"] = self._evictions + evicted
            summary["returned_content_chars"] = _returned_content_chars(result)
        plan = result.get("plan")
        if isinstance(plan, dict):
            delivery = plan.get("delivery")
            if isinstance(delivery, dict):
                delivery["session_dedup"] = True
                delivery["session_scope"] = "stdio_connection"
                delivery["session_max_ranges"] = self.max_ranges

    @staticmethod
    def _replace_with_pointer(reference: _ContextRef, *, call_id: int) -> None:
        previous = reference.previous
        if previous is None:  # pragma: no cover - guarded by ``repeated``
            return
        content = reference.context.pop("content")
        reference.context["content_kind"] = "session_pointer"
        reference.context["session_pointer"] = (
            "Identical verified source was returned by explore_context call "
            f"{previous.first_seen_call}; {len(content)} repeated characters omitted."
        )
        reference.context["session_dedup"] = {
            "repeated": True,
            "content_retained": False,
            "source_call": previous.first_seen_call,
            "first_seen_call": previous.first_seen_call,
            "previous_call": previous.last_seen_call,
            "current_call": call_id,
        }

    @staticmethod
    def _annotate_retained(reference: _ContextRef, *, call_id: int) -> None:
        previous = reference.previous
        if previous is None:
            return
        if reference.repeated:
            reference.context["session_dedup"] = {
                "repeated": True,
                "content_retained": True,
                "source_call": previous.first_seen_call,
                "first_seen_call": previous.first_seen_call,
                "previous_call": previous.last_seen_call,
                "current_call": call_id,
            }
            return
        if previous.content_fingerprint != reference.content_fingerprint:
            reference.context["session_dedup"] = {
                "repeated": False,
                "content_changed": True,
                "source_call": call_id,
                "first_seen_call": call_id,
                "previous_call": previous.last_seen_call,
                "current_call": call_id,
            }
        elif previous.delivery_fingerprint != reference.delivery_fingerprint:
            reference.context["session_dedup"] = {
                "repeated": False,
                "projection_changed": True,
                "source_call": call_id,
                "first_seen_call": call_id,
                "previous_call": previous.last_seen_call,
                "current_call": call_id,
            }


@dataclass(slots=True)
class ExploreSessionRuntime:
    """Connection-scoped ledger and serialization barrier for explore calls."""

    ledger: ExploreSessionLedger = field(default_factory=ExploreSessionLedger)
    call_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_worker: asyncio.Task[Any] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def close(self) -> None:
        """Discard all delivery history owned by this connection."""

        self.ledger.reset()


def _text_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _iter_contexts(
    response: Mapping[str, Any],
) -> Iterator[tuple[str, dict[str, Any]]]:
    files = response.get("files")
    if not isinstance(files, list):
        return
    for group in files:
        if not isinstance(group, Mapping):
            continue
        file_path = group.get("file")
        contexts = group.get("contexts")
        if not isinstance(file_path, str) or not isinstance(contexts, list):
            continue
        for context in contexts:
            if isinstance(context, dict):
                yield file_path, context


def _session_pointer_count(response: Mapping[str, Any]) -> int:
    return sum(
        context.get("content_kind") == "session_pointer"
        for _file_path, context in _iter_contexts(response)
    )


def _delivery_signature(response: Mapping[str, Any]) -> tuple[tuple[Any, ...], ...]:
    """Describe only delivered bodies/pointers that affect ledger commits."""

    signature: list[tuple[Any, ...]] = []
    for file_path, context in _iter_contexts(response):
        content = context.get("content")
        dedup = context.get("session_dedup")
        signature.append(
            (
                file_path,
                context.get("start_line"),
                context.get("end_line"),
                _text_fingerprint(content) if isinstance(content, str) else None,
                context.get("content_kind"),
                dedup.get("source_call") if isinstance(dedup, Mapping) else None,
            )
        )
    return tuple(signature)


def _returned_content_chars(response: Mapping[str, Any]) -> int:
    return sum(
        len(content)
        for _file_path, context in _iter_contexts(response)
        if isinstance((content := context.get("content")), str)
    )


__all__ = ["ExploreSessionLedger", "ExploreSessionRuntime"]
