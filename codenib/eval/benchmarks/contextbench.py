# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Observed-context conversion into ContextBench's official scoring format.

Prediction spans describe source lines in delivered tool messages, not a
retrieved node's enclosing range. Source ranges use 1-based inclusive lines.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from codenib.dataset.contextbench import ContextBenchCase, relative_source_path

CONTEXTBENCH_UPSTREAM_REVISION = "1436c28a8eb95496da4ea69ad458b9f8a8eb7d61"
_NUMBERED_LINE = re.compile(r"^\s*(\d+)\s*\| ?(.*)$")


def contextbench_gold_record(case: ContextBenchCase) -> dict[str, Any]:
    """Official JSON gold without reference-patch EditLoc fallback leakage."""
    return {
        "inst_id": case.instance_id,
        "original_inst_id": case.original_inst_id,
        "repo_url": case.repo_url,
        "commit": case.base_commit,
        "gold_ctx": list(case.gold_context),
    }


def _merge_ranges(ranges: Iterable[tuple[int, int]]) -> list[dict[str, int]]:
    merged: list[dict[str, int]] = []
    for start, end in sorted(set(ranges)):
        if merged and start <= merged[-1]["end"] + 1:
            merged[-1]["end"] = max(merged[-1]["end"], end)
        else:
            merged.append({"start": start, "end": end})
    return merged


def _visible_json_nodes(text: str) -> Iterable[dict[str, Any]]:
    """Decode nodes and complete source lines in a truncated final JSON node."""
    decoder = json.JSONDecoder()
    remaining = text.lstrip()
    if remaining.startswith("["):
        remaining = remaining[1:].lstrip()
    while remaining:
        try:
            node, end = decoder.raw_decode(remaining)
        except ValueError:
            partial = _partial_json_node(remaining)
            if partial:
                yield partial
            return
        if isinstance(node, dict):
            yield node
        remaining = remaining[end:].lstrip()
        if not remaining.startswith(","):
            return
        remaining = remaining[1:].lstrip()


def _partial_json_node(text: str) -> dict[str, Any]:
    """Recover parsed fields and newline-terminated content, without guessing."""
    if not text.startswith("{"):
        return {}
    decoder = json.JSONDecoder()
    remaining = text[1:].lstrip()
    fields = {}
    while remaining:
        key = None
        try:
            key, end = decoder.raw_decode(remaining)
            remaining = remaining[end:].lstrip()
            if not isinstance(key, str) or not remaining.startswith(":"):
                break
            remaining = remaining[1:].lstrip()
            value, end = decoder.raw_decode(remaining)
        except ValueError:
            if key == "content" and remaining.startswith('"'):
                # Scan JSON escapes; a literal backslash+n in source is not
                # an encoded newline. Keep only complete delivered lines.
                offset, last_newline = 1, None
                while offset < len(remaining):
                    char = remaining[offset]
                    if char == '"' or char == "\n":
                        break
                    if char == "\\":
                        if remaining[offset : offset + 2] == "\\n":
                            last_newline = offset + 2
                        offset += 2
                    else:
                        offset += 1
                if last_newline is not None:
                    try:
                        fields[key] = json.loads(remaining[:last_newline] + '"')
                    except ValueError:
                        pass
            break
        fields[key] = value
        remaining = remaining[end:].lstrip()
        if not remaining.startswith(","):
            break
        remaining = remaining[1:].lstrip()
    return fields


def contextbench_prediction(
    *,
    instance_id: str,
    messages: Sequence[Mapping[str, Any]],
    repo_path: str | Path,
    model_patch: str = "",
) -> dict[str, Any]:
    """Convert delivered CodeNib read/grep/retrieval messages to official JSON.

    Directory listings, candidate names, errors, and unnumbered snippets do
    not count as viewed source. Gaps between visible lines remain gaps. Each
    tool response is one step, including empty responses, so unsuccessful
    searches and repeated reads remain in the trajectory denominator.
    """
    root = Path(repo_path).resolve()
    calls: dict[str, Mapping[str, Any]] = {}
    steps: list[dict[str, Any]] = []
    cumulative: dict[str, list[tuple[int, int]]] = {}

    def source_path(raw: str) -> str | None:
        try:
            path = Path(raw)
            if path.is_absolute():
                path = path.relative_to(root)
            relative = relative_source_path(path.as_posix())
            resolved = (root / relative).resolve()
            if not resolved.is_relative_to(root) or not resolved.is_file():
                return None
            return relative
        except (ValueError, OSError):
            return None

    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                calls[str(call["id"])] = call["function"]
        if message.get("role") != "tool":
            continue
        call = calls.get(str(message.get("tool_call_id")))
        if call is None:
            raise ValueError("tool message is missing its originating call")
        arguments = call.get("arguments") or "{}"
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        content = str(message.get("content") or "")
        viewed: dict[str, list[tuple[int, int]]] = {}

        def add_numbered(raw_path: str, text: str, *, viewed=viewed) -> None:
            path = source_path(raw_path)
            if path is None:
                return
            # Line numbers already passed through the agent's 1-based boundary.
            for line in text.splitlines():
                match = _NUMBERED_LINE.match(line)
                if match and int(match[1]) > 0:
                    n = int(match[1])
                    viewed.setdefault(path, []).append((n, n))

        if not (
            call.get("unavailable")
            or message.get("is_error")
            or content.lstrip().startswith("Error")
        ):
            if call["name"] == "read":
                add_numbered(str(arguments.get("file_path") or ""), content)
            elif call["name"] == "grep":
                if arguments.get("output_mode", "content") == "content":
                    for line in content.splitlines():
                        match = re.match(r"^(.*?):(\d+): ?", line)
                        if match and int(match[2]) > 0:
                            path = source_path(match[1])
                            if path:
                                n = int(match[2])
                                viewed.setdefault(path, []).append((n, n))
            elif call["name"] != "glob":
                for node in _visible_json_nodes(content):
                    add_numbered(str(node.get("file") or ""), node.get("content") or "")
        steps.append(
            {
                "files": sorted(viewed),
                "spans": {
                    path: _merge_ranges(ranges) for path, ranges in viewed.items()
                },
            }
        )
        for path, ranges in viewed.items():
            cumulative.setdefault(path, []).extend(ranges)
    return {
        "instance_id": instance_id,
        "traj_data": {
            "pred_steps": steps,
            "pred_files": sorted(cumulative),
            "pred_spans": {
                path: _merge_ranges(ranges) for path, ranges in cumulative.items()
            },
        },
        "model_patch": model_patch,
    }
