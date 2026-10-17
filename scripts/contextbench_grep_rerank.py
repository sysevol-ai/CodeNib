# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Matched grep source views for the checkout-only ContextBench experiment.

Native grep selects the files and lines. The existing chunker supplies bounded
physical source around those hits; optional Jev ordering happens before the
agent sees the response. No graph, index, annotations or separate planner.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time

import tiktoken

from codenib.agent.decision_rerank import decide_code_relevance
from codenib.code_chunker import CodeChunker
from codenib.languages import extension_to_language_map
from codenib.llm.decisions import OpenRouterDecisions
from codenib.types import NodeInfo
from scripts.benchmark_context_agent import observed_nodes
from scripts.benchmark_graph_decisions import node_key, visible_node

JEV_MODEL = "typesafe/jev-1.13"
GREP_VIEW_DESCRIPTION = (
    "Content results return bounded, numbered source blocks around grep hits. "
    "Use read to inspect additional ranges. Other output modes and underlying "
    "search limits are unchanged. Source blocks may contain neighboring lines "
    "that do not themselves match the pattern."
)
GREP_HINT_DESCRIPTION = (
    "Content results preserve every original grep match and may append up to "
    "three suggested source ranges for this search. Suggestions are navigation "
    "metadata, not source evidence. Use read to inspect a suggested range when "
    "it resolves your current uncertainty. Other output modes are unchanged."
)
GREP_POLICIES = ("native", "chunks", "jev", "hints", "jev-hints")


def candidates(repo, arguments, original, limit=40):
    """Choose the smallest visible source chunk covering each verified hit.

    Unmapped hits retain their exact observed source. Source windows are at most
    100 lines / 3,000 characters, with whole lines only. A chunk whose visible
    prefix omits the hit cannot replace it. Stop at the candidate cap and report
    it explicitly; this is not an exhaustive repository search.
    """
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "grep",
                    "function": {"name": "grep", "arguments": json.dumps(arguments)},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "grep", "content": original},
    ]
    observations = observed_nodes(messages, repo)
    languages = extension_to_language_map("chunker")
    files, selected, seen = {}, [], set()
    for observation in observations:
        file = observation.file
        if file not in files:
            path = repo / file
            source = path.read_text(errors="replace").splitlines(keepends=True)
            language = languages.get(path.suffix.lower())
            chunks = []
            if language:
                chunker = CodeChunker(
                    language=language, chunk_depth=2, max_lines_per_chunk=100
                )
                chunks = [
                    visible_node(chunk, source).model_copy(update={"file": file})
                    for chunk in chunker.chunk_file(str(path), relative_path=file)
                ]
            files[file] = (source, chunks)
        source, chunks = files[file]
        for line in range(observation.start_line, observation.end_line + 1):
            covering = [n for n in chunks if n.start_line <= line <= n.end_line]
            if covering:
                node = min(covering, key=lambda n: n.end_line - n.start_line)
            else:
                content = source[line].rstrip("\r\n")
                if not content.strip() or len(content) > 3000:
                    continue
                node = NodeInfo(
                    node_id=f"{file}:{line + 1}",
                    node_name=file,
                    file=file,
                    start_line=line,
                    end_line=line,
                    content=content,
                )
            key = node_key(node)
            if key in seen:
                continue
            seen.add(key)
            selected.append(node)
            if len(selected) >= limit:
                return selected
    return selected


def render(nodes, token_budget, encoder, *, original_limited=False):
    """Emit exact grep-shaped lines, deduplicated within the bounded reply."""
    notice = (
        "[CodeNib grep source view: at most 40 candidates and 10 returned blocks; "
        f"{token_budget}-token output limit. Use read for additional source. "
        + (
            "Underlying grep hit a limit.]"
            if original_limited
            else "Grep limits apply.]"
        )
    )
    text, seen, blocks = "", set(), 0
    for node in nodes:
        if blocks >= 10:
            break
        added = False
        for line, content in enumerate(node.content.splitlines(), node.start_line + 1):
            identity = (node.file, line)
            if identity in seen:
                continue
            proposal = text + f"{node.file}:{line}: {content}\n"
            if (
                len(proposal + notice) > 15900
                or len(encoder.encode(proposal + notice, disallowed_special=()))
                > token_budget
            ):
                break
            text = proposal
            seen.add(identity)
            added = True
        blocks += added
    return text + notice


def render_hints(original, nodes, token_budget, encoder):
    """Keep the raw reply byte-for-byte and append bounded read arguments only."""
    suffix = "\n[Suggested read ranges; source not yet inspected by this suggestion]\n"
    hints = []
    for node in nodes:
        if node.end_line == node.start_line:
            continue  # A one-line fallback contains no source beyond the hit.
        hint = {
            "file_path": node.file,
            "offset": node.start_line + 1,
            "limit": node.end_line - node.start_line + 1,
        }
        proposal = suffix + json.dumps(hint, ensure_ascii=False) + "\n"
        if len(original + proposal) > 15900 or len(
            encoder.encode(proposal, disallowed_special=())
        ) > min(512, token_budget):
            continue
        suffix = proposal
        hints.append(hint)
        if len(hints) == 3:
            break
    return (original + suffix if hints else original), hints


class GrepSourceView:
    """One case's source-view policy and serialized Jev receipt ledger.

    The lock covers selection, every API reservation/receipt, and checkpointing.
    Appending a pending receipt before dispatch is the reservation linearization
    point. No later request runs after uncertain usage or a failed decision.
    There are no other locks, cross-process owners or resumable requests; callers
    retain the terminal ledger and never retry an interrupted case implicitly.
    Checkpoints must write a separate sidecar, not the SDK's concurrent event file.
    """

    def __init__(
        self,
        *,
        repo,
        query,
        policy,
        token_budget=4096,
        max_cost_usd=0.05,
        checkpoint=lambda ledger: None,
    ):
        if policy not in GREP_POLICIES[1:] or not 256 <= token_budget <= 8192:
            raise ValueError("Invalid grep view or token budget")
        if not math.isfinite(max_cost_usd) or max_cost_usd <= 0:
            raise ValueError("Jev cost guard must be finite and positive")
        self.repo = repo.resolve()
        self.query = query[:16000]
        self.policy = policy
        self.token_budget = token_budget
        self.max_cost_usd = max_cost_usd
        self.checkpoint = checkpoint
        self.encoder = tiktoken.get_encoding("cl100k_base")
        self.client = OpenRouterDecisions(model=JEV_MODEL, max_retries=0, timeout=30)
        self.lock = threading.Lock()
        self.ledger = {
            "policy": policy,
            "views": [],
            "calls": [],
            "cost_usd": 0.0,
            "failed": False,
            "unknown_usage_calls": 0,
        }

    @property
    def description(self):
        return (
            GREP_HINT_DESCRIPTION if "hints" in self.policy else GREP_VIEW_DESCRIPTION
        )

    def _rank(self, nodes, query):
        scores = {}
        for start in range(0, len(nodes), 10):
            if self.ledger["cost_usd"] + 0.003 > self.max_cost_usd:
                self.ledger["failed"] = True
                raise RuntimeError("Jev experiment cost guard reached")
            batch = list(enumerate(nodes[start : start + 10], start))
            record = {"status": "pending", "candidate_indices": [i for i, _ in batch]}
            self.ledger["calls"].append(record)
            self.checkpoint(self.ledger)
            started = time.perf_counter()
            try:
                result = decide_code_relevance(self.client, query, batch)
                record.update(
                    model=result.model,
                    usage=result.usage,
                    response_id=result.id,
                    answers={k: v.model_dump() for k, v in result.answers.items()},
                )
                cost = result.usage.get("cost")
                if (
                    type(cost) not in (int, float)
                    or not math.isfinite(cost)
                    or cost < 0
                ):
                    raise ValueError("Jev usage unavailable")
                self.ledger["cost_usd"] += cost
                if cost > 0.003:
                    raise ValueError("Jev request reservation exceeded")
                if not result.model.startswith(JEV_MODEL):
                    raise ValueError("Unexpected Jev response model")
                for index, _ in batch:
                    scores[index] = result.answers[f"node_{index}"].score
                record["status"] = "ok"
            except Exception as exc:
                record.update(status="error", error_type=type(exc).__name__)
                cost = record.get("usage", {}).get("cost")
                if (
                    type(cost) not in (int, float)
                    or not math.isfinite(cost)
                    or cost < 0
                ):
                    self.ledger["unknown_usage_calls"] += 1
                self.ledger["failed"] = True
                raise RuntimeError(
                    "Jev scoring failed; experiment fallback disabled"
                ) from exc
            finally:
                record["latency_s"] = time.perf_counter() - started
                self.checkpoint(self.ledger)
        return [nodes[i] for i in sorted(scores, key=lambda i: (-scores[i], i))]

    def wrap(self, execute):
        def viewed(**arguments):
            original = execute(**arguments)
            if (
                original.lstrip().startswith("Error")
                or arguments.get("output_mode", "content") != "content"
            ):
                return original
            with self.lock:
                if self.ledger["failed"]:
                    raise RuntimeError("Jev experiment previously failed")
                if "hints" in self.policy and len(original) >= 15900:
                    return original
                try:
                    nodes = candidates(self.repo, arguments, original)
                    if not nodes:
                        return original
                    trace = {
                        "arguments": arguments,
                        "original_sha256": hashlib.sha256(
                            original.encode()
                        ).hexdigest(),
                        "original": original,
                        "candidates": [n.model_dump() for n in nodes],
                        "candidate_limit_reached": len(nodes) == 40,
                    }
                    self.ledger["views"].append(trace)
                    query = self.query
                    if "hints" in self.policy:
                        query = (
                            "Choose source ranges to read next for the current search. "
                            "Prioritize code that answers this search's identifiers or "
                            "conditions, including supporting behavior and tests. "
                            "The issue provides context; do not rank only the likely "
                            "edit site when this search concerns a dependency.\n"
                            "Issue:\n"
                            + self.query
                            + "\nCurrent grep arguments:\n"
                            + json.dumps(arguments, ensure_ascii=False)
                        )
                    trace["ranking_query"] = query
                    ranked = (
                        self._rank(nodes, query)
                        if self.policy in {"jev", "jev-hints"}
                        else nodes
                    )
                    if "hints" in self.policy:
                        reply, trace["suggestions"] = render_hints(
                            original, ranked, self.token_budget, self.encoder
                        )
                    else:
                        reply = render(
                            ranked,
                            self.token_budget,
                            self.encoder,
                            original_limited="truncat" in original
                            or "head_limit=" in original,
                        )
                    trace.update(
                        order=[node_key(n) for n in ranked],
                        reply=reply,
                        reply_tokens=len(
                            self.encoder.encode(reply, disallowed_special=())
                        ),
                    )
                    return reply
                except Exception:
                    self.ledger["failed"] = True
                    raise
                finally:
                    self.checkpoint(self.ledger)

        return viewed
