#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0
"""Synthesize N behavioral queries for one SWE-bench instance.

Cycles ``query_index`` so each query gets a distinct anchor block; mixes
simple (5-30 word) and detailed (80-150 word) variants via length-biased
system prompts; runs a 3-way judge + post-fix loop.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from codeminer.dataset.synthesize import ClaudeQuerySynthesizer
from codeminer.dataset.synthesize._agent import AgentRunner
from codeminer.dataset.utils import QueryType, get_prompt_for_query_type
from codeminer.log_utils import get_logger

sys.path.insert(0, str(Path(__file__).parent))
from _post_fix import post_fix_flagged  # noqa: E402

logger = get_logger(__name__)


_BASE_PROMPT = (
    "You are a codebase assistant helping create code search evaluation queries. "
)
_LEVEL_PROMPT = get_prompt_for_query_type(QueryType.BEHAVIORAL)

_LENGTH_DETAILED = (
    "\n\nLENGTH CONSTRAINT: 80 to 150 words. Include multiple observed symptoms, "
    "inputs and outputs, and at least one edge case. Read like a detailed GitHub "
    "issue. Close with an explicit question."
)
_LENGTH_SIMPLE = (
    "\n\nLENGTH CONSTRAINT: 5 to 30 words. One sentence only, symptom-style — "
    "the kind of one-liner a developer pastes in chat or files as a terse issue."
)

DETAILED_SYSTEM_PROMPT = _BASE_PROMPT + _LEVEL_PROMPT + _LENGTH_DETAILED
SIMPLE_SYSTEM_PROMPT = _BASE_PROMPT + _LEVEL_PROMPT + _LENGTH_SIMPLE


JUDGE_SYSTEM_PROMPT = (
    "You evaluate synthesized code-search queries for a retrieval benchmark. "
    "For each item you receive a query text plus the target code it is supposed "
    "to resolve to. Decide whether the query is a reasonable behavioral search "
    "query AND whether the target code is a plausible answer.\n\n"
    "Pick exactly one verdict per item:\n"
    "  - 'valid': query is correct as written.\n"
    "  - 'fix': query has a SMALL, EDITABLE issue (mentions a code identifier "
    "in a behavioral context, length out of range, mildly too generic) but "
    "the topic / target / framing is right. A small rewrite would resolve it.\n"
    "  - 'regenerate': query is FUNDAMENTALLY broken — wrong target, inverts "
    "the code's actual behavior, describes unrelated functionality, vacuous, "
    "malformed, or empty. A fresh generation from the anchor is the right "
    "repair.\n\n"
    "Give a one-sentence reason per item."
)


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


def _build_synth(
    *,
    system_prompt: str,
    model: str,
    max_turns: int,
    sampling_seed: int,
    max_candidate_blocks: int,
    behavioral_consensus_runs: int,
) -> ClaudeQuerySynthesizer:
    return ClaudeQuerySynthesizer(
        model=model,
        max_turns=max_turns,
        allowed_tools=["Read", "Glob", "Grep", "Bash"],
        permission_mode="bypassPermissions",
        system_prompt=system_prompt,
        query_type=QueryType.BEHAVIORAL,
        sampling_seed=sampling_seed,
        max_candidate_blocks=max_candidate_blocks,
        behavioral_consensus_runs=behavioral_consensus_runs,
        num_queries=1,
        verification_mode="lenient",
    )


def _assign_length_variants(
    n: int, simple_ratio: float, rng: random.Random
) -> List[str]:
    n_simple = round(n * simple_ratio)
    variants = ["simple"] * n_simple + ["detailed"] * (n - n_simple)
    rng.shuffle(variants)
    return variants


# Strip a `"question": "...` prefix that occasionally leaks when the
# curator's structured parser falls back to text-coerce mode.
_QUERY_LEAK_PREFIX = re.compile(r'^\s*["\']?question["\']?\s*:\s*["\']?', re.IGNORECASE)


def _clean_query_text(text: Optional[str]) -> str:
    if not text:
        return text or ""
    cleaned = _QUERY_LEAK_PREFIX.sub("", text).strip()
    return cleaned


def _to_compact_record(item: Dict[str, Any]) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "repo": item.get("repo"),
        "instance_id": item.get("instance_id"),
        "base_commit": item.get("base_commit"),
        "query": _clean_query_text(item.get("query")),
        "category": item.get("query_type") or item.get("difficulty"),
        "gt_symbols": item.get("target_symbols") or [],
        "gt_symbol_nodes": item.get("target_symbol_nodes") or [],
        "gt_files": item.get("target_files") or [],
        "query_id": item.get("query_id"),
    }
    if item.get("language_group"):
        record["language_group"] = item["language_group"]
    if "verification_passed" in item:
        record["verification_passed"] = item["verification_passed"]
    if "length_variant" in item:
        record["length_variant"] = item["length_variant"]
    if "judge_verdict" in item:
        record["judge_verdict"] = item["judge_verdict"]
    if "judge_reason" in item:
        record["judge_reason"] = item["judge_reason"]
    if item.get("error"):
        record["error"] = item["error"]
    return record


def _write_compact(results: List[Dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(
            [_to_compact_record(r) for r in results], fh, indent=2, ensure_ascii=False
        )


def _judge_payload(results: List[Dict[str, Any]]) -> str:
    items: List[Dict[str, Any]] = []
    for r in results:
        if r.get("error") or not r.get("query"):
            continue
        nodes = r.get("target_symbol_nodes") or []
        target_code = (nodes[0].get("content", "") if nodes else "") or ""
        items.append(
            {
                "query_id": r.get("query_id"),
                "query": r.get("query"),
                "target_files": r.get("target_files", []),
                "target_symbols": r.get("target_symbols", []),
                "target_code_excerpt": target_code[:2000],
            }
        )
    return json.dumps(items, ensure_ascii=False, indent=2)


async def _run_judge(
    *, results: List[Dict[str, Any]], model: str
) -> Dict[str, Dict[str, str]]:
    payload = _judge_payload(results)
    if not payload or payload == "[]":
        return {}
    agent = AgentRunner(
        model=model,
        max_turns=1,
        allowed_tools=[],
        permission_mode="bypassPermissions",
        system_prompt=JUDGE_SYSTEM_PROMPT,
    )
    prompt = (
        "Evaluate each of the following synthesized queries. Reply with a JSON "
        "array, one object per query: "
        '[{"query_id": "...", "verdict": "valid|regenerate", "reason": "..."}]. '
        "Do not include any prose outside the JSON.\n\n"
        f"QUERIES:\n{payload}"
    )
    raw = await agent.run_async(prompt)
    fenced = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", raw)
    if fenced:
        text = fenced.group(1)
    else:
        m = re.search(r"\[[\s\S]*\]", raw)
        text = m.group(0) if m else raw
    try:
        verdicts = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("Judge returned non-JSON response: %s\nRaw: %s", exc, raw[:500])
        return {}
    return {
        v["query_id"]: v for v in verdicts if isinstance(v, dict) and "query_id" in v
    }


async def _generate_one(
    *,
    synth: ClaudeQuerySynthesizer,
    instance: Dict[str, Any],
    query_index: int,
    repo_root: Optional[str],
    cache_dir: str,
) -> Dict[str, Any]:
    inst = dict(instance)
    inst["synthesis_run_id"] = 1  # one shared candidate pool; query_index varies pick
    return await synth.synthesize_query_async(
        inst,
        repo_root=repo_root,
        cache_dir=cache_dir,
        query_index=query_index,
    )


async def _run_pipeline(args: argparse.Namespace) -> None:
    seed_path = Path(args.seed_file).expanduser()
    instance = _load_seed_instance(seed_path)

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = (
        Path(args.cache_dir).expanduser()
        if args.cache_dir
        else Path.home() / ".codeminer"
    )

    rng = random.Random(args.assignment_seed)
    variants = _assign_length_variants(args.n_queries, args.simple_ratio, rng)

    pool_size = max(args.n_queries * 2, 40)
    detailed_synth = _build_synth(
        system_prompt=DETAILED_SYSTEM_PROMPT,
        model=args.model,
        max_turns=args.max_turns,
        sampling_seed=args.sampling_seed,
        max_candidate_blocks=pool_size,
        behavioral_consensus_runs=args.behavioral_consensus_runs,
    )
    simple_synth = _build_synth(
        system_prompt=SIMPLE_SYSTEM_PROMPT,
        model=args.model,
        max_turns=args.max_turns,
        sampling_seed=args.sampling_seed,
        max_candidate_blocks=pool_size,
        behavioral_consensus_runs=args.behavioral_consensus_runs,
    )

    output_path = output_dir / f"synthesized_queries_{instance['instance_id']}.json"
    results: List[Dict[str, Any]] = []
    for i, variant in enumerate(variants):
        logger.info(
            "[%d/%d] Generating %s query (query_index=%d)...",
            i + 1,
            args.n_queries,
            variant,
            i,
        )
        synth = simple_synth if variant == "simple" else detailed_synth
        try:
            result = await _generate_one(
                synth=synth,
                instance=instance,
                query_index=i,
                repo_root=args.repo_cache_dir,
                cache_dir=str(cache_dir),
            )
        except Exception as exc:
            logger.error(
                "[%d/%d] FAILED: %s", i + 1, args.n_queries, exc, exc_info=True
            )
            result = {
                "instance_id": instance["instance_id"],
                "repo": instance["repo"],
                "base_commit": instance["base_commit"],
                "query_id": (f"{instance['instance_id']}_behavioral_q{i + 1}"),
                "error": str(exc),
            }
        result["language_group"] = instance.get("language_group")
        result["length_variant"] = variant
        results.append(result)
        _write_compact(results, output_path)
        logger.info(
            "Saved %d/%d results to %s", len(results), args.n_queries, output_path
        )

    logger.info("Running judge over %d generated queries...", len(results))
    verdicts = await _run_judge(results=results, model=args.judge_model)
    for r in results:
        v = verdicts.get(r.get("query_id"), {})
        r["judge_verdict"] = v.get("verdict", "unknown")
        r["judge_reason"] = v.get("reason", "")

    _write_compact(results, output_path)

    # Operate on the compact projection so the saved file matches what we judged.
    compact = [_to_compact_record(r) for r in results]
    compact, postfix_stats = await post_fix_flagged(
        compact,
        model=args.model,
        judge_model=args.judge_model,
        max_retries=args.post_fix_max_retries,
    )
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(compact, fh, indent=2, ensure_ascii=False)

    n_valid = sum(1 for r in compact if r.get("judge_verdict") == "valid")
    n_regen = sum(1 for r in compact if r.get("judge_verdict") == "regenerate")
    n_unknown = sum(1 for r in compact if r.get("judge_verdict") == "unknown")
    n_err = sum(1 for r in compact if r.get("error"))

    print("\n--- Summary ---")
    print(f"Total queries generated: {len(compact)}")
    print(f"  Errored during generation: {n_err}")
    print(f"  Judge valid:               {n_valid}")
    print(f"  Judge needs regeneration:  {n_regen}")
    print(f"  Judge unknown / no verdict:{n_unknown}")
    print(f"  Post-fix: {postfix_stats}")
    print(f"Output: {output_path}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--seed-file",
        required=True,
        type=str,
        help="Per-instance q1 JSON; reads repo / instance_id / base_commit from it.",
    )
    p.add_argument("--output-dir", default="synthesis_output_behavior_x20", type=str)
    p.add_argument("--n-queries", type=int, default=20)
    p.add_argument(
        "--simple-ratio",
        type=float,
        default=0.5,
        help="Fraction of queries that should be short symptom-style (0..1).",
    )
    p.add_argument("--model", default="opus", help="Claude model for the synthesizer.")
    p.add_argument(
        "--judge-model", default="opus", help="Claude model for the final judge."
    )
    p.add_argument("--max-turns", type=int, default=10)
    p.add_argument(
        "--sampling-seed",
        type=int,
        default=42,
        help="Seed for the candidate-block pool.",
    )
    p.add_argument(
        "--assignment-seed",
        type=int,
        default=0,
        help="Seed for shuffling simple/detailed assignment.",
    )
    p.add_argument("--cache-dir", type=str, default=None)
    p.add_argument("--repo-cache-dir", type=str, default=None)
    p.add_argument(
        "--behavioral-consensus-runs",
        type=int,
        default=3,
        help=(
            "How many LLM passes to run per behavioral query. Blocks selected "
            "by majority (>= ceil(N/2)) become the ground truth. N=3 matches "
            "synthesize_swebench.py and yields organic multi-hop gt when the "
            "LLM consistently picks the core block + related neighbors. N=1 "
            "is faster but suppresses multi-hop."
        ),
    )
    p.add_argument(
        "--post-fix-max-retries",
        type=int,
        default=3,
        help=(
            "After the batch judge, for each row flagged 'regenerate', call "
            "Claude with the judge's complaint and ask for a fix, then "
            "re-judge. Repeat up to N times before giving up. Set 0 to skip."
        ),
    )
    return p


def main() -> None:
    args = build_parser().parse_args()
    if not 0.0 <= args.simple_ratio <= 1.0:
        raise ValueError("--simple-ratio must be in [0, 1]")
    if args.n_queries < 1:
        raise ValueError("--n-queries must be >= 1")
    asyncio.run(_run_pipeline(args))


if __name__ == "__main__":
    main()
