# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Admission control for incrementally updated indexes.

An incremental update is only *admissible* when the patched artifact is
equivalent to what a fresh rebuild would have produced. This module defines
that check as a pluggable contract so the compiler can decide, per update,
whether to keep the patched result or discard it and rebuild.

Two implementations ship here:

``NullVerifier``
    Cannot prove anything, and says so. Every result comes back
    ``verified=False`` with ``reason="no verifier configured"``. This is the
    default: it makes "we did not check" explicit rather than silent.

``AlwaysAdmitVerifier``
    Admits without checking. Only for environments that have accepted the
    risk deliberately -- it exists so that choice is a visible configuration
    value rather than a missing code path.

The real exactness check (fact-projection equality plus serving replay) lives
in the evaluation harness and is intended to drop in here as a third
implementation without changing any caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@dataclass(slots=True)
class VerificationResult:
    """Outcome of checking a patched artifact against its fresh target.

    Attributes:
        verified: True only when the patched artifact was *proven* equivalent.
            False covers both "checked and differs" and "could not check".
        reason: Human-readable explanation, always populated when not verified.
        checked: Whether a real comparison ran at all. Distinguishes "no
            verifier available" (``checked=False``) from "compared and found
            different" (``checked=True, verified=False``) -- these have very
            different implications and should never be conflated in reporting.
        details: Optional verifier-specific metrics (edge F1, mismatch counts).
    """

    verified: bool
    reason: str = ""
    checked: bool = False
    details: Dict[str, Any] = field(default_factory=dict)

    def to_metadata(self) -> Dict[str, Any]:
        """Flatten into manifest metadata so admission is auditable on disk."""
        meta: Dict[str, Any] = {
            "verified": self.verified,
            "verification_checked": self.checked,
        }
        if self.reason:
            meta["verification_reason"] = self.reason
        if self.details:
            meta["verification_details"] = dict(self.details)
        return meta


@runtime_checkable
class UpdateVerifier(Protocol):
    """Decides whether a patched artifact may be kept.

    Implementations must be side-effect free: returning ``verified=False``
    tells the caller to discard the patch and rebuild, so a verifier that
    mutated state would corrupt that fallback.
    """

    def verify(
        self,
        *,
        index_type: str,
        patched: Any,
        fresh: Optional[Any] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> VerificationResult:
        """Check *patched*, optionally against a freshly built *fresh*."""
        ...


class NullVerifier:
    """Default verifier: proves nothing and reports that honestly."""

    name = "null"

    def verify(
        self,
        *,
        index_type: str,
        patched: Any,
        fresh: Optional[Any] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> VerificationResult:
        return VerificationResult(
            verified=False,
            checked=False,
            reason="no verifier configured; incremental result unverified",
        )


class AlwaysAdmitVerifier:
    """Admits every update without checking.

    Use only where the correctness risk has been accepted knowingly. The
    result still records ``checked=False`` so downstream reporting cannot
    mistake this for a proof.
    """

    name = "always-admit"

    def verify(
        self,
        *,
        index_type: str,
        patched: Any,
        fresh: Optional[Any] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> VerificationResult:
        return VerificationResult(
            verified=True,
            checked=False,
            reason="admitted without verification by configuration",
        )
