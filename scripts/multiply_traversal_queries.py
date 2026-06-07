#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0
"""Synthesize "traversal" queries — code-search queries whose answer requires
walking 2-3 call/reference edges in the code graph and whose text contains no
direct lexical hook to the chain symbols (so a grep agent loses, a graph
agent wins). GT carries the full chain.

Example
-------
    SEED=synthesis_output_behavior/synthesized_queries_<instance>.json
    python scripts/multiply_traversal_queries.py \\
        --seed-file "$SEED" \\
        --output-dir synthesis_output_traversal_x10/ \\
        --stance-counts "trace_back=10,trace_down=10,bridge=10" \\
        --model opus --judge-model opus \\
        --cache-dir ~/.codeminer --repo-cache-dir ~/.codeminer
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from codeminer.dataset.synthesize._agent import AgentRunner
from codeminer.dataset.synthesize.context_loader import ContextLoader
from codeminer.log_utils import get_logger
from codeminer.types import NODE_TYPE_CLASS, NODE_TYPE_FUNCTION, NODE_TYPE_METHOD

sys.path.insert(0, str(Path(__file__).parent))
from _post_fix import post_fix_flagged  # noqa: E402

logger = get_logger(__name__)


# A stance picks the graph walk direction, which chain members the query may
# paraphrase (anchors), and which are hidden (targets). Symbol names are
# always forbidden — stance only governs whose behavior can be described.
STANCES = {
    "trace_back": {
        # chain = [symptom_loc, caller1, (caller2)], walked via predecessor edges
        "direction": "predecessor",
        "narrative": (
            "Write the question from the perspective of someone DEBUGGING. "
            "They see a wrong observable behavior at the SYMPTOM symbol "
            "(SYMBOL 1) — e.g. unexpected output, confusing exception, "
            "missing side-effect — and want to know WHERE UPSTREAM the cause "
            "originates. The remaining symbols are upstream callers; "
            "describing them would defeat the question, so describe ONLY the "
            "symptom's observable misbehavior."
        ),
        "anchor_indices": [0],  # describe the leaf (symptom)
    },
    "trace_down": {
        # chain = [entry, callee1, (callee2)], walked via successor edges
        "direction": "successor",
        "narrative": (
            "Write the question from the perspective of someone USING the "
            "ENTRY symbol's API (SYMBOL 1). They describe the interface — "
            "what they call with, what comes back, an observable surprise — "
            "and ask WHAT HAPPENS INTERNALLY that explains it. The remaining "
            "symbols are downstream helpers/callees that the entry dispatches "
            "into; describing them would defeat the question, so describe "
            "ONLY the entry's interface and the surprising output."
        ),
        "anchor_indices": [0],  # describe the entry (top)
    },
    "bridge": {
        # chain = [start, middle, end], 3 symbols, callee direction
        "direction": "successor",
        "narrative": (
            "Write the question from the perspective of someone who can "
            "OBSERVE BOTH ENDS of a data/control flow but does not know the "
            "middle. They describe the START symbol's responsibility (SYMBOL "
            "1, an entry/setup) AND the END symbol's observable state "
            "(SYMBOL N, a final effect), and ask HOW DATA TRAVELS between "
            "them. The middle symbol(s) are the answer to be discovered; "
            "describe ONLY the two ends."
        ),
        "anchor_indices": "ends",  # special: chain[0] + chain[-1]
    },
}


def _resolve_anchor_indices(stance: str, chain_len: int) -> List[int]:
    cfg = STANCES[stance]
    raw = cfg["anchor_indices"]
    if raw == "ends":
        return [0, chain_len - 1]
    return list(raw)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_TRAVERSAL_BASE_RULES = (
    "  (a) Frames a real debugging / understanding question that requires "
    "      reading ALL chain symbols together to answer — not just one of "
    "      them in isolation.\n"
    "  (b) Reads like a user encountering an observable issue at runtime.\n"
    "  (c) ABSOLUTELY DOES NOT mention any symbol name, function name, "
    "      class name, method name, file path, file extension, variable "
    "      name, or any other lexical token taken verbatim from ANY chain "
    "      member (including the anchor symbols whose BEHAVIOR you DO "
    "      describe). Names of the language, library, or domain (e.g. "
    '      "astropy", "numpy") are also forbidden.\n'
    "  (d) Frames the question so a keyword/grep agent has NO lexical hook "
    "      to land on the right symbols, but a graph agent can follow edges "
    "      from one anchor to the next.\n"
    "  (e) Length: 60–130 words, ends with a question mark.\n"
)


def _system_prompt_for(stance: str) -> str:
    cfg = STANCES[stance]
    return (
        "You are creating a code-search benchmark query for testing graph-"
        "based retrieval. You will be given an ORDERED CHAIN of source-code "
        f"symbols connected by call/reference edges.\n\n"
        f"STANCE = {stance!r}\n{cfg['narrative']}\n\n"
        "Your job: write ONE natural-language question that satisfies ALL of:\n"
        + _TRAVERSAL_BASE_RULES
        + '\nOutput STRICT JSON with one key: {"query": "..."}. No prose, no '
        "markdown, no commentary outside the JSON."
    )


def _judge_prompt_for(stance: str) -> str:
    cfg = STANCES[stance]
    return (
        "You evaluate synthesized 'traversal' code-search queries for a "
        "retrieval benchmark. The query has a declared STANCE that controls "
        "which symbols' behavior should be paraphrased vs. hidden.\n\n"
        f"STANCE = {stance!r}\n{cfg['narrative']}\n\n"
        "Grade against THREE bars at once:\n"
        "  - Topic correctness: the question accurately describes the "
        "    STANCE's intended anchor(s) and the answer plausibly requires "
        "    traversing the remaining (hidden) chain members.\n"
        "  - Graph-only solvability: the question contains NO verbatim "
        "    symbol/file/class/method/variable names from ANY chain member.\n"
        "  - Stance compliance: behaviors of HIDDEN chain members must not "
        "    be paraphrased; only the anchor positions for this stance may "
        "    be described.\n\n"
        "Pick exactly one verdict:\n"
        "  - 'valid': all three bars met.\n"
        "  - 'fix': topic + stance right but minor lexical leak; small edit "
        "    would fix it.\n"
        "  - 'regenerate': topic mismatch, multiple leaks, hidden member "
        "    behavior described, or vacuous/malformed.\n\n"
        "Give a one-sentence reason."
    )


# ---------------------------------------------------------------------------
# Chain extraction
# ---------------------------------------------------------------------------


# Classes are weaker chain members (mostly contain edges) but kept as fallback.
_CHAIN_NODE_KINDS = {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD, NODE_TYPE_CLASS}

_MIN_CHAIN_MEMBER_LINES = 8
_MAX_CHAIN_MEMBER_LINES = 400


class ChainMember:
    __slots__ = ("vid", "name", "type", "file", "start_line", "end_line", "content")

    def __init__(
        self,
        vid: int,
        name: str,
        type_: str,
        file: str,
        start_line: int,
        end_line: int,
        content: str,
    ) -> None:
        self.vid = vid
        self.name = name
        self.type = type_
        self.file = file
        self.start_line = start_line
        self.end_line = end_line
        self.content = content

    @property
    def basename(self) -> str:
        return re.split(r"[:/]", self.name)[-1].rstrip("()")

    @property
    def line_count(self) -> int:
        return max(0, (self.end_line or 0) - (self.start_line or 0) + 1)


def _vertex_to_member(code_graph: Any, vid: int) -> Optional[ChainMember]:
    try:
        attrs = code_graph.get_node_info_by_id(vid)
    except Exception:
        return None
    if not attrs:
        return None
    node_type = attrs.get("type")
    if node_type not in _CHAIN_NODE_KINDS:
        return None
    file = attrs.get("file")
    start = attrs.get("start_line")
    end = attrs.get("end_line")
    name = attrs.get("name")
    if not (file and name and isinstance(start, int) and isinstance(end, int)):
        return None
    line_count = max(0, end - start + 1)
    if line_count < _MIN_CHAIN_MEMBER_LINES or line_count > _MAX_CHAIN_MEMBER_LINES:
        return None
    content = code_graph.get_node_content(vid) or ""
    content = content.strip()
    if len(content) < 60:
        return None
    return ChainMember(vid, name, node_type, file, start, end, content)


def _pick_neighbor(
    code_graph: Any,
    member: ChainMember,
    *,
    direction: str,
    visited_vids: set,
    visited_basenames: set,
    rng: random.Random,
) -> Optional[ChainMember]:
    # Weighted by graph degree (more central node → richer chain story) and
    # by cross-file bias (same-file neighbors are weaker because file_hint
    # would already pin them).
    if direction == "successor":
        cand_vids = code_graph.get_successors(member.name)
    elif direction == "predecessor":
        cand_vids = code_graph.get_predecessors(member.name)
    else:
        raise ValueError(direction)
    cand_vids = [v for v in cand_vids if v not in visited_vids]
    if not cand_vids:
        return None

    candidates: List[Tuple[ChainMember, int]] = []
    g = code_graph.get_graph()
    for vid in cand_vids:
        m = _vertex_to_member(code_graph, vid)
        if m is None:
            continue
        if m.basename.lower() in visited_basenames:
            continue
        if m.file == member.file:
            weight = 1  # same-file: down-weighted, since file_hint would already pin it
        else:
            weight = 5
        deg = g.degree(vid) if isinstance(vid, int) else 1
        candidates.append((m, weight * (1 + min(deg, 20))))

    if not candidates:
        return None
    members, weights = zip(*candidates, strict=False)
    pick = rng.choices(members, weights=weights, k=1)[0]
    return pick


def build_chain(
    code_graph: Any,
    seed: ChainMember,
    *,
    hops: int,
    direction: str,
    rng: random.Random,
) -> Optional[List[ChainMember]]:
    chain = [seed]
    visited_vids = {seed.vid}
    visited_basenames = {seed.basename.lower()}

    for _ in range(hops):
        prev = chain[-1]
        nxt = _pick_neighbor(
            code_graph,
            prev,
            direction=direction,
            visited_vids=visited_vids,
            visited_basenames=visited_basenames,
            rng=rng,
        )
        if nxt is None:
            return None
        chain.append(nxt)
        visited_vids.add(nxt.vid)
        visited_basenames.add(nxt.basename.lower())

    return chain


def _chain_is_good(chain: List[ChainMember]) -> Tuple[bool, str]:
    if len(chain) < 2:
        return False, "chain too short"
    normalized_contents = {
        re.sub(r"\s+", " ", (m.content or "").strip()) for m in chain
    }
    if len(normalized_contents) < len(chain):
        return False, "chain contains duplicate code excerpts"
    return True, "ok"


def _chain_key(chain: List[ChainMember]) -> Tuple[str, ...]:
    return tuple(m.name for m in chain)


def _read_json_payload(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _load_excluded_chain_keys(paths: List[str]) -> set[Tuple[str, ...]]:
    excluded: set[Tuple[str, ...]] = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if path.is_dir():
            candidates = sorted(
                p
                for p in path.rglob("*.json")
                if p.name.startswith("synthesized_queries_")
            )
        else:
            candidates = [path]
        for candidate in candidates:
            if not candidate.exists():
                raise FileNotFoundError(candidate)
            payload = _read_json_payload(candidate)
            rows = payload if isinstance(payload, list) else [payload]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                symbols = row.get("gt_symbols") or row.get("target_symbols") or []
                if symbols:
                    excluded.add(tuple(str(s) for s in symbols))
    return excluded


# ---------------------------------------------------------------------------
# Query generation
# ---------------------------------------------------------------------------

_MAX_BLOCK_CHARS_IN_PROMPT = 1500


def _format_chain_for_prompt(chain: List[ChainMember], stance: str) -> str:
    anchor_idxs = set(_resolve_anchor_indices(stance, len(chain)))
    parts = []
    for i, m in enumerate(chain, 1):
        is_anchor = (i - 1) in anchor_idxs
        role = (
            "ANCHOR (paraphrase this symbol's BEHAVIOR; never its name)"
            if is_anchor
            else "HIDDEN (do NOT describe this symbol's behavior; it is the answer to discover)"
        )
        if is_anchor:
            snippet = (
                m.content
                if len(m.content) <= _MAX_BLOCK_CHARS_IN_PROMPT
                else m.content[:_MAX_BLOCK_CHARS_IN_PROMPT] + "..."
            )
        else:
            snippet = (
                "[hidden from generation: this chain member is part of the "
                "answer and must not be paraphrased or named in the query]"
            )
        if is_anchor:
            location = (
                f"file: {m.file}\n"
                f"name: {m.name}\n"
                f"kind: {m.type}\n"
                f"lines: {m.start_line}-{m.end_line}"
            )
        else:
            location = "file: [hidden]\nname: [hidden]\nkind: [hidden]\nlines: [hidden]"
        parts.append(
            f"--- SYMBOL {i} of {len(chain)} — ROLE: {role} ---\n"
            f"{location}\n"
            f"```\n{snippet}\n```"
        )
    return "\n\n".join(parts)


def _forbidden_lexical_hooks(chain: List[ChainMember]) -> List[str]:
    hooks: set = set()
    for m in chain:
        if m.file:
            hooks.add(m.file)
            hooks.add(Path(m.file).name)
            hooks.add(Path(m.file).stem)
        parts = re.split(r"[:/.]", m.name)
        for p in parts:
            p = p.strip("()")
            if p and not p.isdigit():
                hooks.add(p)
        if m.basename:
            hooks.add(m.basename)
    # Skip short / generic tokens to avoid false positives on words like "name", "data".
    return [h for h in hooks if len(h) >= 4 and h.lower() not in _COMMON_TOKENS]


_COMMON_TOKENS = {
    "self",
    "init",
    "main",
    "test",
    "data",
    "type",
    "info",
    "value",
    "name",
    "path",
    "file",
    "line",
    "true",
    "false",
    "none",
    "null",
    "args",
    "kwargs",
    "func",
    "list",
    "dict",
    "node",
    "root",
    "size",
}


def _query_violates_anti_grep(query: str, hooks: List[str]) -> List[str]:
    q = query.lower()
    leaked = []
    for h in hooks:
        # Substring (not word-boundary) so compound matches like
        # "binom_conf_interval" inside "binom_conf_intervalled" still leak.
        if h.lower() in q:
            leaked.append(h)
    return leaked


async def _generate_query(
    chain: List[ChainMember],
    *,
    stance: str,
    model: str,
    extra_instruction: str = "",
    max_turns: int = 1,
) -> Optional[str]:
    agent = AgentRunner(
        model=model,
        max_turns=max_turns,
        allowed_tools=[],
        permission_mode="bypassPermissions",
        system_prompt=_system_prompt_for(stance),
    )
    prompt = _format_chain_for_prompt(chain, stance)
    if extra_instruction:
        prompt = f"{prompt}\n\nADDITIONAL CONSTRAINT: {extra_instruction}"
    prompt += "\n\nOutput JSON only."
    raw = await agent.run_async(prompt)
    return _extract_query(raw)


def _extract_query(raw: str) -> Optional[str]:
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    candidate = fenced.group(1) if fenced else None
    if not candidate:
        m = re.search(r'\{[\s\S]*?"query"[\s\S]*?\}', raw)
        if not m:
            return None
        candidate = m.group(0)
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    q = obj.get("query")
    if isinstance(q, str) and q.strip():
        return q.strip()
    return None


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------


async def _judge_one(
    *, query: str, chain: List[ChainMember], stance: str, model: str
) -> Tuple[str, str]:
    anchor_idxs = set(_resolve_anchor_indices(stance, len(chain)))
    payload = {
        "query": query,
        "stance": stance,
        "chain": [
            {
                "position": i + 1,
                "role": "anchor" if i in anchor_idxs else "hidden",
                "name": m.name,
                "file": m.file,
                "code_excerpt": (m.content or "")[:1200],
            }
            for i, m in enumerate(chain)
        ],
    }
    agent = AgentRunner(
        model=model,
        max_turns=1,
        allowed_tools=[],
        permission_mode="bypassPermissions",
        system_prompt=_judge_prompt_for(stance),
    )
    prompt = (
        "Evaluate the following traversal query against the supplied chain. "
        'Reply with a single JSON object: {"verdict": "valid|fix|regenerate", '
        '"reason": "..."}. Do not include any prose outside the JSON.\n\n'
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    raw = await agent.run_async(prompt)
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    text = fenced.group(1) if fenced else None
    if not text:
        m = re.search(r"\{[\s\S]*?\"verdict\"[\s\S]*?\}", raw)
        text = m.group(0) if m else None
    if not text:
        return "unknown", "judge did not return JSON"
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return "unknown", "judge JSON parse failed"
    return obj.get("verdict", "unknown"), obj.get("reason", "")


# ---------------------------------------------------------------------------
# Record assembly
# ---------------------------------------------------------------------------


def _to_record(
    *,
    instance: Dict[str, Any],
    query_id: str,
    query: str,
    chain: List[ChainMember],
    hops: int,
    stance: str,
    judge_verdict: str,
    judge_reason: str,
    anti_grep_leaks: List[str],
) -> Dict[str, Any]:
    anchor_idxs = _resolve_anchor_indices(stance, len(chain))
    hidden_idxs = [i for i in range(len(chain)) if i not in anchor_idxs]
    return {
        "repo": instance.get("repo"),
        "instance_id": instance.get("instance_id"),
        "base_commit": instance.get("base_commit"),
        "language_group": instance.get("language_group"),
        "query": query,
        "category": "traversal",
        "gt_files": sorted({m.file for m in chain}),
        "gt_symbols": [m.name for m in chain],
        "gt_symbol_nodes": [
            {
                "node_name": m.name,
                "file": m.file,
                "type": m.type,
                "start_line": m.start_line,
                "end_line": m.end_line,
                "content": m.content,
            }
            for m in chain
        ],
        "query_id": query_id,
        "length_variant": "detailed",
        "judge_verdict": judge_verdict,
        "judge_reason": judge_reason,
        "chain_metadata": {
            "hop_count": hops,
            "chain_length": len(chain),
            "stance": stance,
            "direction": STANCES[stance]["direction"],
            "anchor_indices": anchor_idxs,
            "hidden_indices": hidden_idxs,
            "anchor_symbols": [chain[i].name for i in anchor_idxs],
            "hidden_symbols": [chain[i].name for i in hidden_idxs],
            "spans_files": sorted({m.file for m in chain}),
            "spans_n_files": len({m.file for m in chain}),
            "anti_grep_leaks_at_last_gen": anti_grep_leaks,
        },
    }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def _parse_stance_counts(raw: str) -> Dict[str, int]:
    """Parse ``"trace_back=10,trace_down=10,bridge=10"`` into a dict.

    Validates stance names against the STANCES registry; raises on unknown
    or non-positive counts.
    """
    out: Dict[str, int] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"Bad --stance-counts entry {chunk!r}; want name=N.")
        name, value = chunk.split("=", 1)
        name = name.strip()
        if name not in STANCES:
            valid = ", ".join(STANCES.keys())
            raise ValueError(f"Unknown stance {name!r}. Valid: {valid}")
        n = int(value.strip())
        if n < 0:
            raise ValueError(f"Count for {name!r} must be >= 0.")
        if n > 0:
            out[name] = n
    if not out:
        raise ValueError("--stance-counts produced no stances with count > 0.")
    return out


def _load_seed_instance(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    seed = data[0] if isinstance(data, list) and data else data
    required = ("repo", "instance_id", "base_commit")
    missing = [k for k in required if not seed.get(k)]
    if missing:
        raise ValueError(f"Seed file {path} missing fields: {missing}")
    instance = {k: seed[k] for k in required}
    if seed.get("language_group"):
        instance["language_group"] = seed["language_group"]
    return instance


async def _generate_one_with_anti_grep(
    chain: List[ChainMember],
    *,
    stance: str,
    model: str,
    max_turns: int,
    extra_instruction: str = "",
    max_anti_grep_retries: int = 2,
) -> Tuple[Optional[str], List[str]]:
    hooks = _forbidden_lexical_hooks(chain)
    leaks: List[str] = []
    extra = extra_instruction
    query: Optional[str] = None
    for attempt in range(1, max_anti_grep_retries + 1 + 1):
        query = await _generate_query(
            chain,
            stance=stance,
            model=model,
            extra_instruction=extra,
            max_turns=max_turns,
        )
        if not query:
            logger.warning("generator returned no query (attempt %d)", attempt)
            continue
        leaks = _query_violates_anti_grep(query, hooks)
        if not leaks:
            return query, []
        logger.info(
            "anti-grep leak on attempt %d: %s — retrying with stronger nudge",
            attempt,
            leaks[:3],
        )
        leak_instruction = (
            "Your previous attempt leaked these forbidden tokens: "
            + ", ".join(f"`{t}`" for t in leaks[:5])
            + ". Rewrite the query so NONE of these tokens appear anywhere in "
            "it, even as a substring of another word. Use pure behavioral "
            "English."
        )
        extra = (
            f"{extra_instruction}\n\n{leak_instruction}"
            if extra_instruction
            else leak_instruction
        )
    return query, leaks


async def _run_pipeline(args: argparse.Namespace) -> None:
    instance = _load_seed_instance(Path(args.seed_file).expanduser())

    stance_counts = _parse_stance_counts(args.stance_counts)
    total = sum(stance_counts.values())

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = (
        Path(args.cache_dir).expanduser()
        if args.cache_dir
        else Path.home() / ".codeminer"
    )

    # ---- Phase 1: load repo + build CodeGraph ----
    logger.info("Building CodeGraph for %s ...", instance["instance_id"])
    context_agent = AgentRunner(
        model=args.model,
        max_turns=4,
        allowed_tools=["Read", "Glob", "Grep", "Bash"],
        permission_mode="bypassPermissions",
    )
    loader = ContextLoader(
        agent=context_agent,
        max_candidate_blocks=max(total * 10, 100),
        sampling_seed=args.sampling_seed,
    )
    repo_path, snapshot = loader.checkout_and_snapshot(
        instance,
        repo_root=args.repo_cache_dir,
        cache_dir=str(cache_dir),
    )
    code_graph = loader.load_code_graph(
        instance=instance, repo_path=repo_path, cache_dir=str(cache_dir)
    )
    if code_graph is None:
        raise RuntimeError("Failed to build SCIP code graph.")
    candidate_blocks = loader.sample_candidate_blocks(code_graph, instance)
    if not candidate_blocks:
        raise RuntimeError("No usable candidate blocks sampled from graph.")
    logger.info("Graph + %d candidate blocks ready.", len(candidate_blocks))

    # ---- Phase 2: pick seeds ----
    rng = random.Random(args.sampling_seed)
    seed_members: List[ChainMember] = []
    for blk in candidate_blocks:
        m = _vertex_to_member(code_graph, blk.node_id)
        if m is not None:
            seed_members.append(m)
        if len(seed_members) >= total * 10:
            break
    if len(seed_members) < total:
        logger.warning(
            "Only %d hydratable seeds available for %d queries; will retry "
            "until we hit the target.",
            len(seed_members),
            total,
        )

    # ---- Phase 3: generate queries ----
    logger.info(
        "Stance plan: %s (total=%d)",
        {k: v for k, v in stance_counts.items()},
        total,
    )
    excluded_chain_keys = _load_excluded_chain_keys(args.exclude_chain_file)
    if excluded_chain_keys:
        logger.info("Loaded %d excluded traversal chain(s).", len(excluded_chain_keys))

    # Shuffle so failures in one stance don't starve others.
    stance_plan: List[str] = []
    for stance, n in stance_counts.items():
        stance_plan.extend([stance] * n)
    rng.shuffle(stance_plan)

    output_path = output_dir / f"synthesized_queries_{instance['instance_id']}.json"
    records: List[Dict[str, Any]] = []
    seen_chain_keys: set[Tuple[str, ...]] = set(excluded_chain_keys)

    next_seed_idx = 0
    per_stance_q_counter: Dict[str, int] = {s: 0 for s in stance_counts}
    overall_done = 0
    plan_idx = 0
    while overall_done < total and next_seed_idx < len(seed_members):
        if plan_idx >= len(stance_plan):
            # Refill with whatever's left of each stance's quota.
            stance_plan = [
                s
                for s, target in stance_counts.items()
                for _ in range(target - per_stance_q_counter[s])
            ]
            rng.shuffle(stance_plan)
            plan_idx = 0
            if not stance_plan:
                break

        stance = stance_plan[plan_idx]
        plan_idx += 1
        seed = seed_members[next_seed_idx]
        next_seed_idx += 1

        # bridge needs 3-symbol chains (2 hops); trace_back/down need 2-symbol (1 hop)
        hops = 2 if stance == "bridge" else 1
        direction = STANCES[stance]["direction"]
        chain = build_chain(
            code_graph,
            seed,
            hops=hops,
            direction=direction,
            rng=rng,
        )
        if chain is None:
            logger.info(
                "[seed=%s][stance=%s] chain build failed (no %s neighbor)",
                seed.basename,
                stance,
                direction,
            )
            continue
        chain_key = _chain_key(chain)
        if chain_key in seen_chain_keys:
            logger.info(
                "[seed=%s][stance=%s] chain skipped: already generated/excluded",
                seed.basename,
                stance,
            )
            continue
        ok, why = _chain_is_good(chain)
        if not ok:
            logger.info(
                "[seed=%s][stance=%s] chain rejected: %s (members=%s)",
                seed.basename,
                stance,
                why,
                [m.basename for m in chain],
            )
            continue

        query_id = (
            f"{instance['instance_id']}_traversal_{stance}_q"
            f"{per_stance_q_counter[stance] + 1}"
        )
        logger.info(
            "[%d/%d] %s — chain (%d hops, %s): %s",
            overall_done + 1,
            total,
            query_id,
            len(chain) - 1,
            direction,
            " -> ".join(m.basename for m in chain),
        )

        query: Optional[str] = None
        leaks: List[str] = []
        verdict = "regenerate"
        reason = "empty query from generator"
        extra_instruction = ""
        for judge_attempt in range(args.judge_retries + 1):
            query, leaks = await _generate_one_with_anti_grep(
                chain,
                stance=stance,
                model=args.model,
                max_turns=args.max_turns,
                extra_instruction=extra_instruction,
                max_anti_grep_retries=args.anti_grep_retries,
            )
            if not query:
                logger.warning("[%s] empty query after retries", query_id)
                reason = "empty query from generator"
                continue

            verdict, reason = await _judge_one(
                query=query, chain=chain, stance=stance, model=args.judge_model
            )
            logger.info(
                "[%s] judge=%s | leaks=%s | first 120ch=%s",
                query_id,
                verdict,
                leaks[:3] if leaks else "none",
                query[:120],
            )
            if verdict == "valid":
                break
            if judge_attempt < args.judge_retries:
                extra_instruction = (
                    "A traversal-query judge rejected the previous attempt: "
                    f"{reason} Regenerate a substantially different query that "
                    "keeps only the allowed anchor behavior, hides every answer "
                    "symbol's behavior, and avoids verbatim code tokens."
                )

        if not query:
            if args.keep_invalid:
                records.append(
                    _to_record(
                        instance=instance,
                        query_id=query_id,
                        query="",
                        chain=chain,
                        hops=len(chain) - 1,
                        stance=stance,
                        judge_verdict=verdict,
                        judge_reason=reason,
                        anti_grep_leaks=leaks,
                    )
                )
                per_stance_q_counter[stance] += 1
                overall_done += 1
            continue

        record = _to_record(
            instance=instance,
            query_id=query_id,
            query=query,
            chain=chain,
            hops=len(chain) - 1,
            stance=stance,
            judge_verdict=verdict,
            judge_reason=reason,
            anti_grep_leaks=leaks,
        )
        if verdict == "valid" or args.keep_invalid:
            records.append(record)
            seen_chain_keys.add(chain_key)
            per_stance_q_counter[stance] += 1
            overall_done += 1
            # Incremental save
            output_path.write_text(
                json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        else:
            logger.info("[%s] skipped non-valid traversal query.", query_id)

    # ---- Phase 4: post-fix flagged rows ----
    if args.keep_invalid and args.post_fix_max_retries > 0:
        logger.info(
            "Initial pass complete (%d/%d generated). Running post-fix on any "
            "flagged rows...",
            len(records),
            total,
        )
        records, postfix_stats = await post_fix_flagged(
            records,
            model=args.model,
            judge_model=args.judge_model,
            max_retries=args.post_fix_max_retries,
        )
    else:
        postfix_stats = {
            "flagged": 0,
            "promoted_at_triage": 0,
            "fixed_via_fix": 0,
            "fixed_via_regenerate": 0,
            "still_flagged": 0,
        }
    output_path.write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # ---- Phase 5: summary ----
    n_valid = sum(1 for r in records if r.get("judge_verdict") == "valid")
    n_multihop = sum(1 for r in records if len(r.get("gt_symbol_nodes") or []) > 1)
    by_stance: Dict[str, Dict[str, int]] = {
        s: {"total": 0, "valid": 0} for s in stance_counts
    }
    for r in records:
        s = (r.get("chain_metadata") or {}).get("stance") or "unknown"
        if s not in by_stance:
            by_stance[s] = {"total": 0, "valid": 0}
        by_stance[s]["total"] += 1
        if r.get("judge_verdict") == "valid":
            by_stance[s]["valid"] += 1

    print()
    print("=" * 60)
    print(f"traversal synthesis  →  {output_path}")
    print("=" * 60)
    print(f"  total queries:           {len(records)}/{total}")
    print(f"  judge valid:             {n_valid}")
    print(f"  with multi-hop GT (>=2): {n_multihop}")
    print(f"  post-fix stats:          {postfix_stats}")
    print(f"  per-stance:")
    for s, d in by_stance.items():
        print(f"      {s:<12}  {d['valid']:>2}/{d['total']:<2} valid")
    if len(records) < total and not args.allow_partial:
        raise RuntimeError(
            f"Generated {len(records)} traversal rows, expected {total}. "
            "Rerun with a different --sampling-seed or --allow-partial for diagnostics."
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--seed-file",
        required=True,
        type=str,
        help="JSON whose first row gives repo / instance_id / base_commit.",
    )
    p.add_argument(
        "--output-dir", default="synthesis_output_traversal_v2_x30", type=str
    )
    p.add_argument(
        "--stance-counts",
        default="trace_back=10,trace_down=10,bridge=10",
        type=str,
        help=(
            "Comma-separated <stance>=<count>, e.g. "
            '"trace_back=10,trace_down=10,bridge=10". '
            "Valid stances: trace_back, trace_down, bridge."
        ),
    )
    p.add_argument(
        "--n-queries",
        type=int,
        default=None,
        help="Deprecated. Use --stance-counts instead.",
    )
    p.add_argument("--model", default="opus", help="Model for the generator.")
    p.add_argument("--judge-model", default="opus", help="Model for the judge.")
    p.add_argument(
        "--sampling-seed",
        type=int,
        default=42,
        help="Seed for chain extraction RNG.",
    )
    p.add_argument(
        "--anti-grep-retries",
        type=int,
        default=2,
        help="Extra generator attempts when the first attempt leaks chain "
        "symbol names into the query.",
    )
    p.add_argument(
        "--judge-retries",
        type=int,
        default=2,
        help="Regenerate a candidate this many times after traversal judge rejection.",
    )
    p.add_argument("--max-turns", type=int, default=10)
    p.add_argument(
        "--post-fix-max-retries",
        type=int,
        default=3,
        help="Standard post-fix loop max retries (same flag as other multipliers).",
    )
    p.add_argument(
        "--keep-invalid",
        action="store_true",
        help=(
            "Keep traversal rows judged non-valid instead of skipping them. "
            "Useful for diagnostics; release generation should leave this off."
        ),
    )
    p.add_argument(
        "--allow-partial",
        action="store_true",
        help="Exit successfully even if fewer than the requested rows are generated.",
    )
    p.add_argument(
        "--exclude-chain-file",
        action="append",
        default=[],
        help=(
            "Existing traversal JSON file or directory whose gt_symbols chains "
            "should be skipped while generating new rows. May be repeated."
        ),
    )
    p.add_argument("--cache-dir", type=str, default=None)
    p.add_argument("--repo-cache-dir", type=str, default=None)
    return p


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(_run_pipeline(args))


if __name__ == "__main__":
    main()
