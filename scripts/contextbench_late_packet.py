# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Compile late source envelopes from an actual completed native investigation."""

from __future__ import annotations

import json
import re
from collections import deque

import tiktoken

from codenib.types import NodeInfo
from scripts import benchmark_context_agent as native
from scripts.benchmark_jev_base import digest
from scripts.contextbench_file_source import source_windows


def packet_nodes(context):
    """Decode our complete, contiguous numbered packet blocks into physical nodes."""
    result = []
    for item in json.loads(context):
        parsed = [
            re.fullmatch(r"(\d+) \| (.*)", line)
            for line in item["content"].splitlines()
        ]
        if not parsed or not all(parsed):
            raise ValueError("Late packet contains incomplete source records")
        numbers = [int(match[1]) for match in parsed]
        if numbers != list(range(numbers[0], numbers[-1] + 1)):
            raise ValueError("Late packet block must have contiguous physical lines")
        result.append(
            NodeInfo(
                file=item["file"],
                node_name=item["file"],
                node_id=f"{item['file']}:{numbers[0]}-{numbers[-1]}",
                start_line=numbers[0] - 1,
                end_line=numbers[-1] - 1,
                content="\n".join(match[2] for match in parsed),
            )
        )
    return result


def compile_packets(*, repo, commit, messages, extra_tokens=4096, total_tokens=32768):
    """Preserve the native prefix and derive source-only, bounded late packets.

    There are no new search/model calls, annotations or mutable per-file claims.
    Only previously observed files qualify. The final handoff supplies file-order
    hints, never source facts. Captured hashes and the checkout audit bracket
    construction; returning packets is not evidence that a model consumed them.
    """
    if not 1 <= extra_tokens <= total_tokens <= 32768:
        raise ValueError("Invalid late/final source token budgets")
    repo = repo.resolve()
    native.audit_contextbench_snapshot(repo, commit)
    observed = native.observed_nodes(messages, repo)
    known = {
        (n.file, line)
        for n in observed
        for line in range(n.start_line + 1, n.end_line + 2)
    }
    files = list(dict.fromkeys(n.file for n in observed))
    handoff = next(
        (
            m.get("content", "")
            for m in reversed(messages)
            if m.get("role") == "assistant" and m.get("content")
        ),
        "",
    )
    positions = {file: i for i, file in enumerate(files)}

    def priority(file):
        index = handoff.find(file)
        return (
            index if index >= 0 else len(handoff) + positions[file],
            positions[file],
        )

    files.sort(key=priority)
    hashes, queues, skipped = {}, [], []
    for file in files:
        path = (repo / file).resolve()
        if not path.is_relative_to(repo):
            raise ValueError("Late source escapes the checkout")
        hashes[file] = digest(path)
        if path.stat().st_size > 2 * 1024**2:
            skipped.append({"file": file, "reason": "over2MiB"})
            continue
        data = path.read_bytes()
        if len(data) > 2 * 1024**2:
            raise ValueError("Late source changed during construction")
        lines = data.decode(errors="replace").splitlines(keepends=True)
        windows, overlong = source_windows(lines)
        if overlong:
            skipped.append({"file": file, "overlong_source_lines": overlong})
        visible = [n for n in observed if n.file == file]
        candidates = []
        for start, end in windows:
            novel = [i for i in range(start, end) if (file, i + 1) not in known]
            if not novel:
                continue
            groups = []
            for i in novel:
                if not groups or i != groups[-1][-1] + 1:
                    groups.append([])
                groups[-1].append(i)
            # Preserve the historical auxiliary-window eligibility boundary:
            # a numbered first unseen line must fit the 3000-character view.
            if len(f"{novel[0] + 1} | {lines[novel[0]].splitlines()[0]}\n") > 3000:
                continue
            nodes = [
                NodeInfo(
                    file=file,
                    node_name=f"{file}:{g[0] + 1}-{g[-1] + 1}",
                    node_id=f"{file}:{g[0] + 1}-{g[-1] + 1}",
                    start_line=g[0],
                    end_line=g[-1],
                    content="".join(lines[i] for i in g),
                )
                for g in groups
            ]
            distance = min(
                max(n.start_line - (end - 1), start - n.end_line, 0) for n in visible
            )
            candidates.append((distance, start, end, nodes))
        candidates.sort(key=lambda item: item[:3])
        queues.append(deque(candidates))
    chosen = []
    while len(chosen) < 200 and any(queues):
        for queue in queues:
            if queue and len(chosen) < 200:
                chosen.append(queue.popleft())
    encoder = tiktoken.get_encoding("cl100k_base")
    extra_nodes = [node for item in chosen for node in item[3]]
    extra, extra_size = native.pack_context(
        extra_nodes,
        top_k=max(1, len(extra_nodes)),
        token_budget=extra_tokens,
        encoder=encoder,
    )
    novel_nodes = packet_nodes(extra)
    if any(
        (n.file, line) in known
        for n in novel_nodes
        for line in range(n.start_line + 1, n.end_line + 2)
    ):
        raise ValueError("Late envelope repeats already observed source")
    original, original_size = native.pack_context(
        observed,
        top_k=max(1, len(observed)),
        token_budget=total_tokens,
        encoder=encoder,
    )
    late, late_size = native.pack_context(
        [*novel_nodes, *observed],
        top_k=max(1, len(novel_nodes) + len(observed)),
        token_budget=total_tokens,
        encoder=encoder,
    )
    for file, sha in hashes.items():
        if digest(repo / file) != sha:
            raise ValueError("Late source version changed during construction")
    native.audit_contextbench_snapshot(repo, commit)
    return {
        "contexts": {"native_packet": original, "late_physical4096": late},
        "context_tokens": {
            "native_packet": original_size,
            "late_physical4096": late_size,
        },
        "extra_context": extra,
        "extra_tokens": extra_size,
        "source_sha256": hashes,
        "candidate_windows": len(chosen),
        "candidate_limit_reached": any(queues),
        "skipped": skipped,
        "source_scope": "Prepared exact source only; model consumption is not established.",
    }
