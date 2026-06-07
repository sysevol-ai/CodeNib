#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0
"""Synthesize a mix of query types (module_hint / file_hint / symbol_hint /
reasoning, and optionally behavioral) for one SWE-bench instance, with each
non-behavioral row grounded on an anchor pulled from a populated behavioral
JSON (so gt_files / gt_symbols / gt_symbol_nodes are real, not LLM-invented).
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

_LENGTH_DETAILED = (
    "\n\nLENGTH CONSTRAINT: 80 to 150 words. Include multiple observed symptoms, "
    "inputs and outputs, and at least one edge case. Read like a detailed GitHub "
    "issue. Close with an explicit question."
)
_LENGTH_SIMPLE = (
    "\n\nLENGTH CONSTRAINT: 5 to 30 words. One sentence only, symptom-style — "
    "the kind of one-liner a developer pastes in chat or files as a terse issue."
)


JUDGE_SYSTEM_PROMPT = (
    "You evaluate synthesized code-search queries for a retrieval benchmark. "
    "For each item you receive a query text, its declared query_type, and the "
    "target code it is supposed to resolve to. Decide whether the query is a "
    "reasonable search query of its declared type AND whether the target code "
    "is a plausible answer.\n\n"
    "Query type expectations:\n"
    "  - behavioral: plain English, no file paths or code identifiers.\n"
    "  - module_hint: may name a module/package, no file paths or symbols.\n"
    "  - file_hint: may name file paths, no specific symbols.\n"
    "  - symbol_hint: may name specific functions/classes/methods.\n"
    "  - reasoning: requires reasoning over call chains, inheritance, control "
    "flow.\n\n"
    "Pick exactly one verdict per item:\n"
    "  - 'valid': query is correct as written.\n"
    "  - 'fix': query has a SMALL, EDITABLE issue (e.g. category violation "
    "such as a behavioral query that mentions a function name, length out of "
    "range, mildly too generic) but the topic / target / framing is right. "
    "A small rewrite of the same query would resolve it.\n"
    "  - 'regenerate': query is FUNDAMENTALLY broken — wrong target, inverts "
    "the code's actual behavior, describes unrelated functionality, vacuous, "
    "malformed, or empty. The original phrasing should NOT be reused; a "
    "fresh generation from the anchor is the right repair.\n\n"
    "Give a one-sentence reason per item."
)


def _system_prompt_for(query_type: QueryType, length_variant: str) -> str:
    level = get_prompt_for_query_type(query_type)
    length = _LENGTH_SIMPLE if length_variant == "simple" else _LENGTH_DETAILED
    return _BASE_PROMPT + level + length


def _parse_type_counts(raw: str) -> Dict[QueryType, int]:
    """Parse "module_hint=10,file_hint=10,symbol_hint=5,reasoning=5"."""
    out: Dict[QueryType, int] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"Bad --type-counts entry {chunk!r}; want name=N.")
        name, value = chunk.split("=", 1)
        try:
            qt = QueryType(name.strip())
        except ValueError as exc:
            valid = ", ".join(t.value for t in QueryType)
            raise ValueError(f"Unknown query type {name!r}. Valid: {valid}") from exc
        n = int(value.strip())
        if n < 0:
            raise ValueError(f"Count for {name!r} must be >= 0.")
        if n > 0:
            out[qt] = n
    if not out:
        raise ValueError("--type-counts produced no types with count > 0.")
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


def _load_anchors(path: Path) -> List[Dict[str, Any]]:
    """Load anchors (file + symbol + actual code content) from a populated
    synthesized-queries JSON (e.g. the behavioral x20 output).

    Each entry becomes one anchor dict in the shape expected by
    ``QueryCurator._build_anchor_block`` — including ``anchor_content`` (the
    real code) so the curator binds the LLM to it rather than letting it
    hallucinate other repo areas.
    """
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    entries = data if isinstance(data, list) else [data]
    anchors: List[Dict[str, Any]] = []
    for e in entries:
        files = e.get("gt_files") or e.get("target_files") or []
        symbols = e.get("gt_symbols") or e.get("target_symbols") or []
        nodes = e.get("gt_symbol_nodes") or e.get("target_symbol_nodes") or []
        anchor_content = ""
        anchor_file = files[0] if files else ""
        anchor_symbol = symbols[0] if symbols else ""
        for node in nodes:
            if node.get("content"):
                anchor_content = node["content"]
                anchor_file = node.get("file") or anchor_file
                anchor_symbol = node.get("node_name") or anchor_symbol
                break
        if not files and not symbols:
            continue
        anchors.append(
            {
                "target_files": list(files),
                "symbols_modified": list(symbols),
                "anchor_content": anchor_content,
                "anchor_file": anchor_file,
                "anchor_symbol": anchor_symbol,
            }
        )
    if not anchors:
        raise ValueError(
            f"No usable anchors (gt_files/gt_symbols) found in {path}. "
            "Point --anchor-file at a populated synthesized-queries JSON."
        )
    n_with_content = sum(1 for a in anchors if a["anchor_content"])
    logger.info(
        "Loaded %d anchors (%d with code content) from %s",
        len(anchors),
        n_with_content,
        path,
    )
    return anchors


def _build_synth(
    *,
    query_type: QueryType,
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
        query_type=query_type,
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


# Strip a `"question": "...` prefix that occasionally leaks from the
# curator's text-coerce fallback.
_QUERY_LEAK_PREFIX = re.compile(r'^\s*["\']?question["\']?\s*:\s*["\']?', re.IGNORECASE)


def _clean_query_text(text: Optional[str]) -> str:
    if not text:
        return text or ""
    return _QUERY_LEAK_PREFIX.sub("", text).strip()


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
                "query_type": r.get("query_type") or r.get("difficulty"),
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


def _build_plan(
    type_counts: Dict[QueryType, int], simple_ratio: float, rng: random.Random
) -> List[Tuple[QueryType, str, int]]:
    """Return a list of (query_type, length_variant, per_type_index) tuples,
    sized to sum(type_counts.values()). per_type_index is 0-based within its
    type — for BEHAVIORAL this is fed to query_index for anchor diversity.
    """
    plan: List[Tuple[QueryType, str, int]] = []
    for qt, n in type_counts.items():
        variants = _assign_length_variants(n, simple_ratio, rng)
        for i, variant in enumerate(variants):
            plan.append((qt, variant, i))
    return plan


async def _generate_one(
    *,
    synth: ClaudeQuerySynthesizer,
    instance: Dict[str, Any],
    query_type: QueryType,
    per_type_index: int,
    repo_root: Optional[str],
    cache_dir: str,
    anchor: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    inst = dict(instance)
    if query_type == QueryType.BEHAVIORAL:
        # Shared candidate pool; vary the anchor via query_index.
        inst["synthesis_run_id"] = 1
        query_index = per_type_index
        ground_truth = None
    else:
        # Re-seed per query + pass a real anchor; without ground_truth the
        # curator's target discovery hallucinates plausible-wrong symbols.
        inst["synthesis_run_id"] = per_type_index + 1
        query_index = per_type_index
        ground_truth = anchor
    return await synth.synthesize_query_async(
        inst,
        repo_root=repo_root,
        cache_dir=cache_dir,
        ground_truth=ground_truth,
        query_index=query_index,
    )


async def _run_pipeline(args: argparse.Namespace) -> None:
    type_counts = _parse_type_counts(args.type_counts)
    total = sum(type_counts.values())
    logger.info(
        "Generating %d queries across %d types: %s",
        total,
        len(type_counts),
        {qt.value: n for qt, n in type_counts.items()},
    )

    seed_path = Path(args.seed_file).expanduser()
    instance = _load_seed_instance(seed_path)

    anchor_path = Path(args.anchor_file).expanduser() if args.anchor_file else seed_path
    anchors: List[Dict[str, Any]] = []
    needs_anchors = any(qt != QueryType.BEHAVIORAL for qt in type_counts)
    if needs_anchors:
        anchors = _load_anchors(anchor_path)

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = (
        Path(args.cache_dir).expanduser()
        if args.cache_dir
        else Path.home() / ".codeminer"
    )

    rng = random.Random(args.assignment_seed)
    plan = _build_plan(type_counts, args.simple_ratio, rng)

    pool_size = max(total * 2, 40)
    synth_cache: Dict[Tuple[QueryType, str], ClaudeQuerySynthesizer] = {}
    for qt in type_counts:
        for variant in ("simple", "detailed"):
            synth_cache[(qt, variant)] = _build_synth(
                query_type=qt,
                system_prompt=_system_prompt_for(qt, variant),
                model=args.model,
                max_turns=args.max_turns,
                sampling_seed=args.sampling_seed,
                max_candidate_blocks=pool_size,
                behavioral_consensus_runs=args.behavioral_consensus_runs,
            )

    output_path = output_dir / f"synthesized_queries_{instance['instance_id']}.json"
    results: List[Dict[str, Any]] = []
    for i, (qt, variant, per_type_idx) in enumerate(plan):
        logger.info(
            "[%d/%d] type=%s variant=%s per_type_idx=%d",
            i + 1,
            total,
            qt.value,
            variant,
            per_type_idx,
        )
        synth = synth_cache[(qt, variant)]
        anchor = (
            anchors[per_type_idx % len(anchors)]
            if anchors and qt != QueryType.BEHAVIORAL
            else None
        )
        try:
            result = await _generate_one(
                synth=synth,
                instance=instance,
                query_type=qt,
                per_type_index=per_type_idx,
                repo_root=args.repo_cache_dir,
                cache_dir=str(cache_dir),
                anchor=anchor,
            )
        except Exception as exc:
            logger.error("[%d/%d] FAILED: %s", i + 1, total, exc, exc_info=True)
            result = {
                "instance_id": instance["instance_id"],
                "repo": instance["repo"],
                "base_commit": instance["base_commit"],
                "query_id": (
                    f"{instance['instance_id']}_{qt.value}_q{per_type_idx + 1}"
                ),
                "query_type": qt.value,
                "error": str(exc),
            }
        result["language_group"] = instance.get("language_group")
        result["length_variant"] = variant
        results.append(result)
        _write_compact(results, output_path)
        logger.info("Saved %d/%d results to %s", len(results), total, output_path)

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
    by_type: Dict[str, int] = {}
    for r in compact:
        t = r.get("category") or "unknown"
        by_type[t] = by_type.get(t, 0) + 1
    print(f"  By type: {by_type}")
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
    p.add_argument(
        "--anchor-file",
        default=None,
        type=str,
        help=(
            "JSON with populated gt_files/gt_symbols (e.g. the x20 behavioral "
            "output) used to ground non-behavioral queries. Defaults to "
            "--seed-file. Anchors are cycled per type."
        ),
    )
    p.add_argument("--output-dir", default="synthesis_output_mixed_x30", type=str)
    p.add_argument(
        "--type-counts",
        required=True,
        type=str,
        help=(
            "Comma-separated <type>=<count>, e.g. "
            '"module_hint=10,file_hint=10,symbol_hint=5,reasoning=5". '
            "Valid types: behavioral, module_hint, file_hint, symbol_hint, reasoning."
        ),
    )
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
        help="Seed for shuffling simple/detailed assignment within each type.",
    )
    p.add_argument("--cache-dir", type=str, default=None)
    p.add_argument("--repo-cache-dir", type=str, default=None)
    p.add_argument(
        "--behavioral-consensus-runs",
        type=int,
        default=3,
        help=(
            "How many LLM passes to run per behavioral query for consensus "
            "voting on selected_blocks. N=3 matches synthesize_swebench.py "
            "and yields organic multi-hop gt; N=1 is faster but suppresses "
            "multi-hop. Only affects behavioral queries."
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
    asyncio.run(_run_pipeline(args))


if __name__ == "__main__":
    main()
