# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Canonical deterministic measurement type."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class Signal:
    """A deterministic measurement about the repository, never a finding."""

    id: str
    kind: str
    locus: List[str]
    detail: str
    value: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        kind: str,
        locus: Iterable[str],
        detail: str,
        value: Optional[Dict[str, Any]] = None,
    ) -> "Signal":
        normalized_locus = [str(item) for item in locus if str(item)]
        digest_input = json.dumps(
            [kind, normalized_locus, detail],
            sort_keys=True,
            separators=(",", ":"),
        )
        signal_id = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:20]
        return cls(
            id=signal_id,
            kind=kind,
            locus=normalized_locus,
            detail=detail,
            value=dict(value or {}),
        )

    @property
    def path(self) -> str:
        return self.locus[0] if self.locus else ""

    @property
    def commit_count(self) -> int:
        return int(self.value.get("commit_count", 0) or 0)

    @property
    def symbol(self) -> str:
        return str(self.value.get("symbol", ""))

    @property
    def file(self) -> str:
        return str(self.value.get("file", ""))

    @property
    def edge_changes(self) -> list:
        return list(self.value.get("edge_changes", []))

    @property
    def nodeid(self) -> str:
        return str(self.value.get("nodeid", self.path))

    @property
    def message(self) -> str:
        return str(self.value.get("message", self.detail))


__all__ = ["Signal"]
