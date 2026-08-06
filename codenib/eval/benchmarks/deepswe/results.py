# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Load fixed-slot results emitted by external DeepSWE runs."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

COUNTED_JOB_IDS = frozenset(f"job_{index}" for index in range(1, 5))


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _row_setting_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("task") or ""),
        str(row.get("baseline") or ""),
        str(row.get("model") or ""),
        str(row.get("reasoning_effort") or ""),
    )


def metadata_paths(output_root: Path) -> list[Path]:
    """Discover counted solo and Guardian trial metadata below *output_root*."""

    paths: list[Path] = []
    for path in output_root.rglob("metadata.json"):
        job_id = path.parent.name
        baseline = path.parent.parent.name if path.parent.parent else ""
        if job_id in COUNTED_JOB_IDS and baseline in {"solo", "guardian"}:
            paths.append(path)
    return sorted(paths)


def load_rows(output_root: Path) -> list[dict[str, Any]]:
    """Load trial rows, preferring setting-scoped layout over legacy rows."""

    rows: list[dict[str, Any]] = []
    for path in metadata_paths(output_root):
        job_id = path.parent.name
        row = _load_json(path)
        if not row:
            print(f"[warn] ignoring unreadable metadata: {path}", file=sys.stderr)
            continue
        if str(row.get("job_id") or job_id) not in COUNTED_JOB_IDS:
            continue
        row["job_id"] = job_id
        row.setdefault("metadata_path", str(path))
        row["_layout"] = (
            "setting" if len(path.relative_to(output_root).parts) >= 5 else "legacy"
        )
        rows.append(row)

    setting_keys = {
        _row_setting_key(row) for row in rows if row.get("_layout") == "setting"
    }
    filtered = [
        row
        for row in rows
        if row.get("_layout") == "setting" or _row_setting_key(row) not in setting_keys
    ]
    for row in filtered:
        row.pop("_layout", None)
    return filtered
