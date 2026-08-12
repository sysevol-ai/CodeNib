# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Durable accounting for multi-turn Codex solver logs."""

from __future__ import annotations

import json
from pathlib import Path

CodexTurnLog = tuple[int, bytes]

_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def _replace_bytes(path: Path, content: bytes) -> None:
    """Atomically replace a bind-mounted file that the host cannot overwrite."""

    temporary = path.with_name(f".{path.name}.guardian-tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def capture_codex_turn(logs_dir: Path, turn_index: int) -> tuple[Path, bytes] | None:
    """Archive and snapshot a turn before another Codex run can replace it."""
    source = logs_dir / "codex.txt"
    if not source.is_file():
        return None
    destination = logs_dir / f"codex_turn_{turn_index:02d}.txt"
    source.replace(destination)
    return destination, destination.read_bytes()


def codex_usage(turn_logs: list[CodexTurnLog]) -> dict[str, int]:
    """Sum usage events from immutable snapshots of every solver turn."""
    totals = {key: 0 for key in _USAGE_KEYS}
    for _, transcript in turn_logs:
        for raw_line in transcript.splitlines():
            if b'"turn.completed"' not in raw_line:
                continue
            try:
                usage = json.loads(raw_line).get("usage", {})
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(usage, dict):
                continue
            for key in _USAGE_KEYS:
                value = usage.get(key, 0)
                if isinstance(value, int) and not isinstance(value, bool):
                    totals[key] += value
    return totals


def persist_codex_logs(logs_dir: Path, turn_logs: list[CodexTurnLog]) -> None:
    """Write per-turn logs, their combined transcript, and cumulative usage."""
    if turn_logs:
        combined = b""
        for turn_index, transcript in turn_logs:
            _replace_bytes(
                logs_dir / f"codex_turn_{turn_index:02d}.txt", transcript
            )
            if combined and not combined.endswith(b"\n"):
                combined += b"\n"
            combined += transcript
        _replace_bytes(logs_dir / "codex.txt", combined)

    _replace_bytes(
        logs_dir / "codex_tokens.json",
        (json.dumps(codex_usage(turn_logs), indent=2) + "\n").encode(),
    )


def restore_codex_accounting(
    logs_dir: Path, turn_logs: list[CodexTurnLog]
) -> dict[str, int]:
    """Restore cumulative logs after an installed agent's post-run rewrite."""

    persist_codex_logs(logs_dir, turn_logs)
    return codex_usage(turn_logs)
