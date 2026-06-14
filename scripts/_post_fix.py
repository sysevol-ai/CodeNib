# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0
"""Post-fix loop for queries flagged by the LLM judge.

Triages each flagged row (valid / fix / regenerate) and applies the matching
repair via Claude, up to ``max_retries`` times. Modifies rows in-place.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from codeminer.dataset.synthesize._agent import AgentRunner
from codeminer.log_utils import get_logger

logger = get_logger(__name__)


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


FIX_SYSTEM_PROMPT = (
    "You are repairing a code-search query that has a SMALL editable issue. "
    "You are shown the original query AND the judge's specific complaint. "
    "Produce a NEW query that addresses the complaint while preserving the "
    "topic/framing of the original, still describes the same code anchor, "
    "and respects the category + length constraints.\n\n"
    'Output STRICT JSON with one key: {"query": "..."}. No prose, no '
    "markdown, no commentary."
)


REGENERATE_SYSTEM_PROMPT = (
    "You are generating a code-search query from scratch because a previous "
    "attempt was judged fundamentally broken. You will NOT see the prior "
    "query (it would bias you). Produce a fresh query that accurately "
    "describes the supplied code anchor and respects the category + length "
    "constraints.\n\n"
    'Output STRICT JSON with one key: {"query": "..."}. No prose, no '
    "markdown, no commentary."
)


def _derive_dotted_module(anchor_file: str) -> str:
    """``astropy/stats/funcs.py`` -> ``astropy.stats``."""
    if not anchor_file:
        return ""
    parts = anchor_file.replace("\\", "/").split("/")
    if len(parts) < 2:
        return ""
    return ".".join(p for p in parts[:-1] if p)


def _category_rule(category: str, anchor_file: str) -> str:
    dotted = _derive_dotted_module(anchor_file)
    if category == "behavioral":
        return (
            "Avoid ALL code identifiers, file paths, and technical names. "
            "Focus purely on observable behavior."
        )
    if category == "module_hint":
        if dotted:
            return (
                f"Your question MUST explicitly name the module in dotted "
                f"form `{dotted}` (or a clearly-recognizable sub-module of "
                "it). It MUST NOT mention the file path or any specific "
                "function/class names."
            )
        return (
            "Your question MUST explicitly name the module in dotted form "
            "(e.g. `astropy.stats`, `numpy.linalg`). It MUST NOT mention the "
            "file path or any specific function/class names."
        )
    if category == "file_hint":
        return (
            "Your question SHOULD mention the file path shown above, but "
            "MUST NOT mention specific function or class names."
        )
    if category == "symbol_hint":
        return (
            "Your question SHOULD mention the specific function/class/method "
            "name shown above and describe its behavior."
        )
    if category == "reasoning":
        return (
            "Your question SHOULD mention the symbol shown above and frame "
            "the problem in terms of call chains, inheritance, or control "
            "flow that involve it."
        )
    return "Describe the behavior of the anchor code."


def _length_rule(length_variant: str) -> str:
    if length_variant == "simple":
        return "5 to 30 words. One sentence only, symptom-style."
    if length_variant == "detailed":
        return (
            "80 to 150 words. Multiple observed symptoms, inputs and outputs, "
            "and at least one edge case. Read like a detailed GitHub issue."
        )
    return "Match the original query's register."


def _symbol_basename(symbol_id: str) -> str:
    # The trailing identifier is stable across Go/Rust file-prefix mangling
    # (e.g. ``terminal.py:foo()`` vs ``src/terminal.rs:foo()``); the prefix
    # is not. We use the trailing form as the cross-row matching key.
    if not symbol_id:
        return ""
    base = re.split(r"[:/]", symbol_id)[-1]
    return re.sub(r"\([^)]*\)$", "", base)


def _build_anchor_content_lookups(
    rows: List[Dict[str, Any]],
    anchors: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Dict[str, str], Dict[Tuple[str, str], str]]:
    # Behavioral rows have content in ``gt_symbol_nodes[*].content``;
    # non-behavioral rows often have ``content=null``. The file+basename
    # lookup recovers content for non-behavioral when its prefix doesn't
    # match the behavioral row's prefix but the file and trailing
    # identifier do.
    by_node_name: Dict[str, str] = {}
    by_file_basename: Dict[Tuple[str, str], str] = {}
    for r in rows:
        gt_file = (r.get("gt_files") or [""])[0]
        for n in r.get("gt_symbol_nodes") or []:
            sym = n.get("node_name") or ""
            content = n.get("content")
            file = n.get("file") or gt_file
            if not (sym and content):
                continue
            by_node_name.setdefault(sym, content)
            base = _symbol_basename(sym)
            if file and base:
                by_file_basename.setdefault((file, base), content)
    # Mixed rows (module/file/symbol/reasoning) store target_symbol_nodes with
    # content=null, so the loop above recovers nothing for them — every flagged
    # mixed row then hits "no anchor content" and is skipped instead of
    # regenerated. The anchors (behavioral output) DO carry the real code, so
    # index them too: this is what unblocks regeneration for the mixed types.
    for a in anchors or []:
        content = a.get("anchor_content")
        if not content:
            continue
        sym = a.get("anchor_symbol") or ""
        file = a.get("anchor_file") or (a.get("target_files") or [""])[0]
        if sym:
            by_node_name.setdefault(sym, content)
            base = _symbol_basename(sym)
            if file and base:
                by_file_basename.setdefault((file, base), content)
    return by_node_name, by_file_basename


def _get_anchor(
    row: Dict[str, Any],
    by_node_name: Dict[str, str],
    by_file_basename: Dict[Tuple[str, str], str],
) -> Tuple[str, str, str]:
    nodes = row.get("gt_symbol_nodes") or []
    file = (row.get("gt_files") or [""])[0]
    symbol = (row.get("gt_symbols") or [""])[0]
    content = ""
    if nodes:
        n0 = nodes[0]
        file = n0.get("file") or file
        symbol = n0.get("node_name") or symbol
        content = n0.get("content") or ""
    if not content:
        content = by_node_name.get(symbol, "")
    if not content:
        base = _symbol_basename(symbol)
        if file and base:
            content = by_file_basename.get((file, base), "")
    return file, symbol, content


def _extract_query_from_payload(raw: str) -> Optional[str]:
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    if fenced:
        candidate = fenced.group(1)
    else:
        m = re.search(r'\{[\s\S]*?"query"[\s\S]*?\}', raw)
        if not m:
            return None
        candidate = m.group(0)
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    q = obj.get("query")
    return q.strip() if isinstance(q, str) and q.strip() else None


async def _fix_one(
    row: Dict[str, Any],
    *,
    anchor_file: str,
    anchor_symbol: str,
    anchor_content: str,
    model: str,
) -> Optional[str]:
    category = row.get("category") or "behavioral"
    length_variant = row.get("length_variant") or "detailed"
    original_query = row.get("query") or ""
    judge_reason = row.get("judge_reason") or ""

    agent = AgentRunner(
        model=model,
        max_turns=1,
        allowed_tools=[],
        permission_mode="bypassPermissions",
        system_prompt=FIX_SYSTEM_PROMPT,
    )
    snippet = (
        anchor_content if len(anchor_content) <= 1800 else anchor_content[:1800] + "..."
    )
    prompt = (
        f"Category: {category}\n"
        f"Category rule: {_category_rule(category, anchor_file)}\n"
        f"Length: {length_variant}\n"
        f"Length rule: {_length_rule(length_variant)}\n\n"
        f"Anchor file: {anchor_file}\n"
        f"Anchor symbol: {anchor_symbol}\n"
        f"Anchor code:\n```\n{snippet}\n```\n\n"
        f"Original query (FLAGGED):\n{original_query}\n\n"
        f"Judge complaint:\n{judge_reason}\n\n"
        'Produce a fixed query as JSON: {"query": "..."}'
    )
    raw = await agent.run_async(prompt)
    return _extract_query_from_payload(raw)


async def _regenerate_one(
    row: Dict[str, Any],
    *,
    anchor_file: str,
    anchor_symbol: str,
    anchor_content: str,
    model: str,
) -> Optional[str]:
    # Showing the prior (broken) query would bias the new generation toward
    # the same mistake; the LLM only sees the anchor + constraints.
    category = row.get("category") or "behavioral"
    length_variant = row.get("length_variant") or "detailed"

    agent = AgentRunner(
        model=model,
        max_turns=1,
        allowed_tools=[],
        permission_mode="bypassPermissions",
        system_prompt=REGENERATE_SYSTEM_PROMPT,
    )
    snippet = (
        anchor_content if len(anchor_content) <= 1800 else anchor_content[:1800] + "..."
    )
    prompt = (
        f"Category: {category}\n"
        f"Category rule: {_category_rule(category, anchor_file)}\n"
        f"Length: {length_variant}\n"
        f"Length rule: {_length_rule(length_variant)}\n\n"
        f"Anchor file: {anchor_file}\n"
        f"Anchor symbol: {anchor_symbol}\n"
        f"Anchor code:\n```\n{snippet}\n```\n\n"
        'Produce a fresh query as JSON: {"query": "..."}'
    )
    raw = await agent.run_async(prompt)
    return _extract_query_from_payload(raw)


async def _judge_one(
    row: Dict[str, Any],
    *,
    by_node_name: Dict[str, str],
    by_file_basename: Dict[Tuple[str, str], str],
    model: str,
) -> Tuple[str, str]:
    _, _, anchor_content = _get_anchor(row, by_node_name, by_file_basename)
    payload = [
        {
            "query_id": row.get("query_id"),
            "query_type": row.get("category"),
            "query": row.get("query"),
            "target_files": row.get("gt_files", []),
            "target_symbols": row.get("gt_symbols", []),
            "target_code_excerpt": (anchor_content or "")[:2000],
        }
    ]
    agent = AgentRunner(
        model=model,
        max_turns=1,
        allowed_tools=[],
        permission_mode="bypassPermissions",
        system_prompt=JUDGE_SYSTEM_PROMPT,
    )
    prompt = (
        "Evaluate the following synthesized query. Reply with a JSON array of "
        "one object: "
        '[{"query_id": "...", "verdict": "valid|regenerate", "reason": "..."}]. '
        "Do not include any prose outside the JSON.\n\n"
        f"QUERIES:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    raw = await agent.run_async(prompt)
    fenced = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", raw)
    if fenced:
        text = fenced.group(1)
    else:
        m = re.search(r"\[[\s\S]*\]", raw)
        text = m.group(0) if m else None
    if not text:
        return "unknown", "judge did not return JSON"
    try:
        arr = json.loads(text)
    except json.JSONDecodeError:
        return "unknown", "judge JSON parse failed"
    if not arr or not isinstance(arr[0], dict):
        return "unknown", "judge returned empty / invalid"
    v = arr[0]
    return v.get("verdict", "unknown"), v.get("reason", "")


async def post_fix_flagged(
    rows: List[Dict[str, Any]],
    *,
    model: str,
    judge_model: str,
    max_retries: int = 3,
    anchors: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Triage flagged rows, then loop fix/regenerate up to ``max_retries`` times.

    ``anchors`` (the behavioral-output anchor dicts, each with ``anchor_content``)
    let the mixed callers recover code that their own rows store as
    ``content=null`` — without it every flagged mixed row is skipped instead of
    regenerated.

    Returns the mutated rows plus a stats dict
    (``flagged`` / ``promoted_at_triage`` / ``fixed_via_fix`` /
    ``fixed_via_regenerate`` / ``still_flagged``).
    """
    by_node_name, by_file_basename = _build_anchor_content_lookups(rows, anchors)

    def is_flagged(r: Dict[str, Any]) -> bool:
        # 'unknown' is included because a truncated judge-batch JSON leaves
        # rows without a real verdict; triage them too.
        return r.get("judge_verdict") in ("regenerate", "fix", "unknown")

    n_flagged = sum(1 for r in rows if is_flagged(r))
    stats = {
        "flagged": n_flagged,
        "promoted_at_triage": 0,
        "fixed_via_fix": 0,
        "fixed_via_regenerate": 0,
        "still_flagged": 0,
    }
    if n_flagged == 0:
        logger.info("post_fix: 0 rows flagged; nothing to do.")
        return rows, stats

    logger.info("post_fix: %d rows flagged for follow-up.", n_flagged)

    for row in rows:
        if not is_flagged(row):
            continue
        anchor_file, anchor_symbol, anchor_content = _get_anchor(
            row, by_node_name, by_file_basename
        )
        if not anchor_content:
            logger.warning(
                "post_fix [%s]: no anchor content available; skipping.",
                row.get("query_id"),
            )
            stats["still_flagged"] += 1
            continue

        attempts: List[Dict[str, Any]] = []

        triage_verdict, triage_reason = await _judge_one(
            row,
            by_node_name=by_node_name,
            by_file_basename=by_file_basename,
            model=judge_model,
        )
        attempts.append(
            {
                "attempt": 0,
                "mode": "triage",
                "verdict": triage_verdict,
                "reason": triage_reason,
            }
        )
        row["judge_verdict"] = triage_verdict
        row["judge_reason"] = triage_reason
        if triage_verdict == "valid":
            logger.info(
                "post_fix [%s] triage promoted to valid (no repair needed).",
                row.get("query_id"),
            )
            row["post_fix_attempts"] = attempts
            stats["promoted_at_triage"] += 1
            continue
        if triage_verdict not in ("fix", "regenerate"):
            logger.warning(
                "post_fix [%s] triage returned unknown verdict %r; skipping.",
                row.get("query_id"),
                triage_verdict,
            )
            row["post_fix_attempts"] = attempts
            stats["still_flagged"] += 1
            continue

        current_mode = triage_verdict  # 'fix' or 'regenerate'
        fixed_via: Optional[str] = None
        for attempt in range(1, max_retries + 1):
            logger.info(
                "post_fix [%s] attempt %d/%d mode=%s",
                row.get("query_id"),
                attempt,
                max_retries,
                current_mode,
            )
            if current_mode == "fix":
                new_query = await _fix_one(
                    row,
                    anchor_file=anchor_file,
                    anchor_symbol=anchor_symbol,
                    anchor_content=anchor_content,
                    model=model,
                )
            else:
                new_query = await _regenerate_one(
                    row,
                    anchor_file=anchor_file,
                    anchor_symbol=anchor_symbol,
                    anchor_content=anchor_content,
                    model=model,
                )
            if not new_query:
                logger.warning(
                    "post_fix [%s] attempt %d (%s): repair returned no query.",
                    row.get("query_id"),
                    attempt,
                    current_mode,
                )
                attempts.append(
                    {
                        "attempt": attempt,
                        "mode": current_mode,
                        "query": None,
                        "verdict": "no_query",
                    }
                )
                continue
            row["query"] = new_query
            verdict, reason = await _judge_one(
                row,
                by_node_name=by_node_name,
                by_file_basename=by_file_basename,
                model=judge_model,
            )
            attempts.append(
                {
                    "attempt": attempt,
                    "mode": current_mode,
                    "query": new_query,
                    "verdict": verdict,
                    "reason": reason,
                }
            )
            if verdict == "valid":
                row["judge_verdict"] = "valid"
                row["judge_reason"] = reason
                row["post_fix_attempts"] = attempts
                fixed_via = current_mode
                logger.info(
                    "post_fix [%s] FIXED via %s on attempt %d.",
                    row.get("query_id"),
                    current_mode,
                    attempt,
                )
                break
            row["judge_reason"] = reason
            if verdict in ("fix", "regenerate"):
                current_mode = verdict

        if fixed_via is None:
            row["post_fix_attempts"] = attempts
            stats["still_flagged"] += 1
            logger.info(
                "post_fix [%s] still flagged after %d attempts.",
                row.get("query_id"),
                max_retries,
            )
        elif fixed_via == "fix":
            stats["fixed_via_fix"] += 1
        else:
            stats["fixed_via_regenerate"] += 1

    return rows, stats
