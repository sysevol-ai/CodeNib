#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0
"""Synthesize "traversal" queries — code-search queries whose answer requires
walking 2-3 call/reference edges in the code graph and whose text contains no
direct lexical hook to the chain symbols (so a grep agent loses, a graph
agent wins). GT carries the full chain.

Example::

    SEED=synthesis_output_behavior/synthesized_queries_<instance>.json
    python scripts/multiply_traversal_queries.py \\
        --seed-file "$SEED" \\
        --output-dir synthesis_output_traversal/ \\
        --stance-counts "trace_back=10,trace_down=10,bridge=10"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from codeminer.dataset.synthesize._agent import AgentRunner
from codeminer.dataset.synthesize.context_loader import ContextLoader
from codeminer.log_utils import get_logger
from codeminer.types import NODE_TYPE_CLASS, NODE_TYPE_FUNCTION, NODE_TYPE_METHOD

logger = get_logger(__name__)


# A stance picks the graph walk direction, which chain members the query may
# paraphrase (anchors), and which are hidden (targets). Symbol names are
# always forbidden — stance only governs whose behavior can be described.
STANCES: Dict[str, Dict[str, Any]] = {
    "trace_back": {
        "direction": "predecessor",
        "narrative": (
            "Write the question from the perspective of someone DEBUGGING. "
            "They see a wrong observable behavior at the SYMPTOM symbol "
            "(SYMBOL 1) and want to know WHERE UPSTREAM the cause originates. "
            "The remaining symbols are upstream callers; describe ONLY the "
            "symptom's observable misbehavior."
        ),
        "anchor_indices": [0],
    },
    "trace_down": {
        "direction": "successor",
        "narrative": (
            "Write the question from the perspective of someone USING the "
            "ENTRY symbol's API (SYMBOL 1). They describe the interface and "
            "ask WHAT HAPPENS INTERNALLY that explains an observed output. "
            "The remaining symbols are downstream callees; describe ONLY the "
            "entry's interface and the surprising output."
        ),
        "anchor_indices": [0],
    },
    "bridge": {
        "direction": "successor",
        "narrative": (
            "Write the question from the perspective of someone who can "
            "OBSERVE BOTH ENDS of a data/control flow but does not know the "
            "middle. Describe the START symbol's responsibility (SYMBOL 1) "
            "AND the END symbol's observable state (SYMBOL N), and ask HOW "
            "DATA TRAVELS between them. Describe ONLY the two ends."
        ),
        "anchor_indices": "ends",
    },
}


def _resolve_anchor_indices(stance: str, chain_len: int) -> List[int]:
    raw = STANCES[stance]["anchor_indices"]
    if raw == "ends":
        return [0, chain_len - 1]
    return list(raw)


_BASE_RULES = (
    "  (a) Frames a real debugging / understanding question that requires "
    "reading ALL chain symbols together to answer.\n"
    "  (b) Reads like a user encountering an observable issue at runtime.\n"
    "  (c) Does NOT mention any symbol name, function name, class name, "
    "method name, file path, file extension, or variable name taken "
    "verbatim from ANY chain member. Library/domain names are also "
    "forbidden.\n"
    "  (d) Length: 60-130 words, ends with a question mark.\n"
)


def _system_prompt_for(stance: str) -> str:
    cfg = STANCES[stance]
    return (
        "You are creating a code-search benchmark query for testing graph-"
        "based retrieval. You will be given an ORDERED CHAIN of source-code "
        "symbols connected by call/reference edges.\n\n"
        f"STANCE = {stance!r}\n{cfg['narrative']}\n\n"
        "Write ONE natural-language question satisfying ALL of:\n"
        + _BASE_RULES
        + '\nOutput STRICT JSON: {"query": "..."}. No prose outside the JSON.'
    )


def _judge_prompt_for(stance: str) -> str:
    cfg = STANCES[stance]
    return (
        "You evaluate synthesized 'traversal' code-search queries. The query "
        "has a declared STANCE that controls which symbols' behavior should "
        "be paraphrased vs. hidden.\n\n"
        f"STANCE = {stance!r}\n{cfg['narrative']}\n\n"
        "Grade against three bars: topic correctness (question matches the "
        "anchor's role and requires traversing hidden members), graph-only "
        "solvability (no verbatim symbol/file names from any chain member), "
        "and stance compliance (hidden members not paraphrased).\n\n"
        "Pick one verdict: 'valid' (all bars met), 'fix' (minor lexical leak "
        "only), 'regenerate' (topic mismatch or multiple problems). Give a "
        "one-sentence reason."
    )


# ---------------------------------------------------------------------------
# Chain extraction
# ---------------------------------------------------------------------------

_CHAIN_NODE_KINDS = {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD, NODE_TYPE_CLASS}
_MIN_LINES, _MAX_LINES = 8, 400


class ChainMember:
    __slots__ = ("vid", "name", "type", "file", "start_line", "end_line", "content")

    def __init__(self, vid, name, type_, file, start_line, end_line, content):
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


def _vertex_to_member(code_graph: Any, vid: int) -> Optional[ChainMember]:
    try:
        attrs = code_graph.get_node_info_by_id(vid)
    except Exception:
        return None
    if not attrs or attrs.get("type") not in _CHAIN_NODE_KINDS:
        return None
    file = attrs.get("file")
    start, end, name = attrs.get("start_line"), attrs.get("end_line"), attrs.get("name")
    if not (file and name and isinstance(start, int) and isinstance(end, int)):
        return None
    if not (_MIN_LINES <= (end - start + 1) <= _MAX_LINES):
        return None
    content = (code_graph.get_node_content(vid) or "").strip()
    if len(content) < 60:
        return None
    return ChainMember(vid, name, attrs["type"], file, start, end, content)


def _pick_neighbor(
    code_graph: Any,
    member: ChainMember,
    *,
    direction: str,
    visited_vids: set,
    visited_basenames: set,
    rng: random.Random,
) -> Optional[ChainMember]:
    if direction == "successor":
        cand_vids = code_graph.get_successors(member.name)
    elif direction == "predecessor":
        cand_vids = code_graph.get_predecessors(member.name)
    else:
        raise ValueError(direction)
    candidates = []
    for vid in cand_vids:
        if vid in visited_vids:
            continue
        m = _vertex_to_member(code_graph, vid)
        if m is None or m.basename.lower() in visited_basenames:
            continue
        # Cross-file neighbors preferred — same-file would already be solvable
        # by file_hint, so down-weight them.
        weight = 1 if m.file == member.file else 5
        candidates.append((m, weight))
    if not candidates:
        return None
    members, weights = zip(*candidates, strict=False)
    return rng.choices(members, weights=weights, k=1)[0]


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
        nxt = _pick_neighbor(
            code_graph,
            chain[-1],
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
    if len({m.file for m in chain}) < 2:
        return False, "chain stays in one file"
    roots = {m.basename.lower().split("_")[0] for m in chain if m.basename}
    if len(roots) <= 1:
        return False, "all chain symbols share a name root"
    return True, "ok"


# ---------------------------------------------------------------------------
# Query generation + judge
# ---------------------------------------------------------------------------

_MAX_BLOCK_CHARS = 1500


def _format_chain_for_prompt(chain: List[ChainMember], stance: str) -> str:
    anchor_idxs = set(_resolve_anchor_indices(stance, len(chain)))
    parts = []
    for i, m in enumerate(chain, 1):
        role = (
            "ANCHOR (paraphrase BEHAVIOR; never the name)"
            if (i - 1) in anchor_idxs
            else "HIDDEN (do NOT describe; this is the answer)"
        )
        snippet = (
            m.content
            if len(m.content) <= _MAX_BLOCK_CHARS
            else m.content[:_MAX_BLOCK_CHARS] + "..."
        )
        parts.append(
            f"--- SYMBOL {i}/{len(chain)} — {role} ---\n"
            f"file: {m.file}\nname: {m.name}\nkind: {m.type}\n"
            f"lines: {m.start_line}-{m.end_line}\n```\n{snippet}\n```"
        )
    return "\n\n".join(parts)


def _extract_query(raw: str) -> Optional[str]:
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    candidate = fenced.group(1) if fenced else None
    if not candidate:
        m = re.search(r'\{[\s\S]*?"query"[\s\S]*?\}', raw)
        candidate = m.group(0) if m else None
    if not candidate:
        return None
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    q = obj.get("query")
    return q.strip() if isinstance(q, str) and q.strip() else None


async def _generate_query(
    chain: List[ChainMember], *, stance: str, model: str
) -> Optional[str]:
    agent = AgentRunner(
        model=model,
        max_turns=1,
        allowed_tools=[],
        permission_mode="bypassPermissions",
        system_prompt=_system_prompt_for(stance),
    )
    prompt = _format_chain_for_prompt(chain, stance) + "\n\nOutput JSON only."
    return _extract_query(await agent.run_async(prompt))


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
        "Evaluate the traversal query. Reply with one JSON object: "
        '{"verdict": "valid|fix|regenerate", "reason": "..."}. No prose '
        f"outside the JSON.\n\nINPUT:\n{json.dumps(payload, indent=2)}"
    )
    raw = await agent.run_async(prompt)
    m = re.search(r"\{[\s\S]*?\"verdict\"[\s\S]*?\}", raw)
    if not m:
        return "unknown", "judge did not return JSON"
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return "unknown", "judge JSON parse failed"
    return obj.get("verdict", "unknown"), obj.get("reason", "")


# ---------------------------------------------------------------------------
# Record assembly + pipeline
# ---------------------------------------------------------------------------


def _to_record(
    *,
    instance: Dict[str, Any],
    query_id: str,
    query: str,
    chain: List[ChainMember],
    stance: str,
    judge_verdict: str,
    judge_reason: str,
) -> Dict[str, Any]:
    anchor_idxs = _resolve_anchor_indices(stance, len(chain))
    hidden_idxs = [i for i in range(len(chain)) if i not in anchor_idxs]
    return {
        "repo": instance.get("repo"),
        "instance_id": instance.get("instance_id"),
        "base_commit": instance.get("base_commit"),
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
        "judge_verdict": judge_verdict,
        "judge_reason": judge_reason,
        "chain_metadata": {
            "chain_length": len(chain),
            "stance": stance,
            "direction": STANCES[stance]["direction"],
            "anchor_indices": anchor_idxs,
            "hidden_indices": hidden_idxs,
            "anchor_symbols": [chain[i].name for i in anchor_idxs],
            "hidden_symbols": [chain[i].name for i in hidden_idxs],
            "spans_n_files": len({m.file for m in chain}),
        },
    }


def _parse_stance_counts(raw: str) -> Dict[str, int]:
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
            raise ValueError(f"Unknown stance {name!r}. Valid: {', '.join(STANCES)}")
        n = int(value.strip())
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
    return {k: seed[k] for k in required}


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

    logger.info("Building CodeGraph for %s ...", instance["instance_id"])
    context_agent = AgentRunner(
        model=args.model,
        max_turns=4,
        allowed_tools=["Read", "Glob", "Grep", "Bash"],
        permission_mode="bypassPermissions",
    )
    loader = ContextLoader(
        agent=context_agent,
        max_candidate_blocks=max(total * 4, 80),
        sampling_seed=args.sampling_seed,
    )
    repo_path, _ = loader.checkout_and_snapshot(
        instance, repo_root=args.repo_cache_dir, cache_dir=str(cache_dir)
    )
    code_graph = loader.load_code_graph(
        instance=instance, repo_path=repo_path, cache_dir=str(cache_dir)
    )
    if code_graph is None:
        raise RuntimeError("Failed to build SCIP code graph.")
    candidate_blocks = loader.sample_candidate_blocks(code_graph, instance)
    if not candidate_blocks:
        raise RuntimeError("No usable candidate blocks sampled from graph.")

    rng = random.Random(args.sampling_seed)
    seed_members: List[ChainMember] = []
    for blk in candidate_blocks:
        m = _vertex_to_member(code_graph, blk.node_id)
        if m is not None:
            seed_members.append(m)
        if len(seed_members) >= total * 4:
            break

    # Round-robin stance plan.
    stance_plan: List[str] = []
    for s, n in stance_counts.items():
        stance_plan.extend([s] * n)
    rng.shuffle(stance_plan)

    output_path = output_dir / f"synthesized_queries_{instance['instance_id']}.json"
    records: List[Dict[str, Any]] = []
    per_stance: Dict[str, int] = {s: 0 for s in stance_counts}

    for plan_idx, stance in enumerate(stance_plan):
        if plan_idx >= len(seed_members):
            logger.warning("Ran out of seeds at %d/%d.", plan_idx, total)
            break
        seed = seed_members[plan_idx]

        hops = 2 if stance == "bridge" else 1
        chain = build_chain(
            code_graph,
            seed,
            hops=hops,
            direction=STANCES[stance]["direction"],
            rng=rng,
        )
        if chain is None:
            continue
        ok, _ = _chain_is_good(chain)
        if not ok:
            continue

        per_stance[stance] += 1
        query_id = f"{instance['instance_id']}_traversal_{stance}_q{per_stance[stance]}"
        query = await _generate_query(chain, stance=stance, model=args.model)
        if not query:
            records.append(
                _to_record(
                    instance=instance,
                    query_id=query_id,
                    query="",
                    chain=chain,
                    stance=stance,
                    judge_verdict="regenerate",
                    judge_reason="empty query from generator",
                )
            )
            continue

        verdict, reason = await _judge_one(
            query=query, chain=chain, stance=stance, model=args.judge_model
        )
        logger.info(
            "[%s] judge=%s | chain=%s",
            query_id,
            verdict,
            " -> ".join(m.basename for m in chain),
        )
        records.append(
            _to_record(
                instance=instance,
                query_id=query_id,
                query=query,
                chain=chain,
                stance=stance,
                judge_verdict=verdict,
                judge_reason=reason,
            )
        )
        output_path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    n_valid = sum(1 for r in records if r.get("judge_verdict") == "valid")
    print(f"traversal synthesis -> {output_path}")
    print(f"  total: {len(records)}/{total}   valid: {n_valid}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed-file", required=True, type=str)
    p.add_argument("--output-dir", default="synthesis_output_traversal", type=str)
    p.add_argument(
        "--stance-counts",
        default="trace_back=10,trace_down=10,bridge=10",
        type=str,
    )
    p.add_argument("--model", default="opus")
    p.add_argument("--judge-model", default="opus")
    p.add_argument("--sampling-seed", type=int, default=42)
    p.add_argument("--cache-dir", type=str, default=None)
    p.add_argument("--repo-cache-dir", type=str, default=None)
    return p


def main() -> None:
    asyncio.run(_run_pipeline(build_parser().parse_args()))


if __name__ == "__main__":
    main()
