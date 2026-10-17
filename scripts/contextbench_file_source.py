# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Opt-in first-read source windows for the ContextBench experiment."""

from __future__ import annotations

import hashlib
import json
import threading

import tiktoken

from scripts.benchmark_context_agent import observed_nodes

MARKER = "\n[CodeNib additional same-file source: exact numbered windows]\n"
DESCRIPTION = (
    "A read with no previously observed read of that file may append bounded, "
    "numbered source windows from the same file, nearest the current range first. "
    "The original read stays intact. Additional windows can be discontinuous; "
    "use their physical line numbers when requesting more source."
)
MAX_REPLY_CHARS = 32000


def source_windows(lines):
    """Tile source into at most64-line/3000-character whole-line windows."""
    windows, skipped = [], []
    start = 0
    while start < len(lines):
        end, size = start, 0
        while end < len(lines) and end - start < 64 and size + len(lines[end]) <= 3000:
            size += len(lines[end])
            end += 1
        if end == start:
            skipped.append(start + 1)
            start += 1
            continue
        windows.append((start, end))
        start = end
    return windows, skipped


def read_messages(arguments, text):
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "current-read",
                    "function": {"name": "read", "arguments": json.dumps(arguments)},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "current-read", "content": text},
    ]


class FileSourceView:
    """Select from observation snapshots and publish exact return receipts.

    Selection owns no per-file claim: simultaneous first reads may repeat source
    until their replies enter observations. The receipt lock only serializes
    append plus checkpoint, the publication linearization point before return;
    it never nests another lock or encloses source execution. Every returned
    augmentation has one receipt, including concurrent duplicates. There is no
    retained state/recovery owner beyond this SDK session.
    """

    def __init__(self, *, repo, token_budget, checkpoint):
        if not 1 <= token_budget <= 4096:
            raise ValueError("File-source token budget must be in [1, 4096]")
        self.repo = repo.resolve()
        self.token_budget = token_budget
        self.encoder = tiktoken.get_encoding("cl100k_base")
        self.checkpoint = checkpoint
        self.lock = threading.Lock()
        self.ledger = {"views": [], "failed": False}

    def wrap(self, execute, *, observed_messages):
        def read(**arguments):
            snapshot = tuple(observed_messages())
            original = execute(**arguments)
            if original.lstrip().startswith("Error"):
                return original
            try:
                reply, receipt = self.select(arguments, original, snapshot)
                if receipt is not None:
                    with self.lock:
                        self.ledger["views"].append(receipt)
                        self.checkpoint(self.ledger)
                return reply
            except Exception:
                with self.lock:
                    self.ledger["failed"] = True
                    self.checkpoint(self.ledger)
                raise

        return read

    def select(self, arguments, original, snapshot):
        current = observed_nodes(read_messages(arguments, original), self.repo)
        if not current:
            return original, None
        file = current[0].file
        assert all(node.file == file for node in current)
        calls = {
            call["id"]: call["function"]
            for message in snapshot
            for call in message.get("tool_calls", [])
        }
        previous_reads = [
            message
            for message in snapshot
            if message.get("role") != "tool"
            or calls.get(message.get("tool_call_id"), {}).get("name") == "read"
        ]
        if any(n.file == file for n in observed_nodes(previous_reads, self.repo)):
            return original, None
        path = (self.repo / file).resolve()
        if not path.is_relative_to(self.repo):
            raise ValueError("File source escapes checkout")
        if path.stat().st_size > 2 * 1024**2:
            return original, None
        data = path.read_bytes()
        if len(data) > 2 * 1024**2:
            return original, None
        lines = data.decode(errors="replace").splitlines(keepends=True)
        windows, skipped = source_windows(lines)
        prior = observed_nodes(snapshot, self.repo)
        known = {
            line
            for node in [*prior, *current]
            if node.file == file
            for line in range(node.start_line, node.end_line + 1)
        }
        eligible = [
            (start, end)
            for start, end in windows
            if any(line not in known for line in range(start, end))
        ]

        def distance(window):
            start, end = window
            return min(
                max(node.start_line - (end - 1), start - node.end_line, 0)
                for node in current
            )

        ordered = sorted(eligible, key=lambda window: (distance(window), *window))[:200]
        suffix, added, blocks = MARKER, [], 0
        for start, end in ordered:
            if blocks >= 10:
                break
            used = False
            for line in range(start, end):
                if line in known:
                    continue
                proposal = suffix + f"{line + 1:6d} | {lines[line].splitlines()[0]}\n"
                if (
                    len(original + proposal) > MAX_REPLY_CHARS
                    or len(self.encoder.encode(proposal, disallowed_special=()))
                    > self.token_budget
                ):
                    break
                suffix = proposal
                known.add(line)
                added.append(line + 1)
                used = True
            blocks += used
        reply = original + suffix if added else original
        if path.read_bytes() != data:
            raise ValueError("Source changed during file-source selection")
        return reply, {
            "arguments": arguments,
            "file": file,
            "original": original,
            "reply": reply,
            "source_sha256": hashlib.sha256(data).hexdigest(),
            "previous_source": [
                [n.file, n.start_line + 1, n.end_line + 1] for n in prior
            ],
            "current_ranges": [[n.start_line + 1, n.end_line + 1] for n in current],
            "candidate_count": len(ordered),
            "candidate_limit_reached": len(eligible) > 200,
            "overlong_lines_skipped": skipped,
            "added_lines": added,
            "extra_tokens": len(
                self.encoder.encode(reply[len(original) :], disallowed_special=())
            ),
            "blocks": blocks,
        }
