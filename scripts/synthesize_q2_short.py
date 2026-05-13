#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Generate short, domain-vocab-stripped query variants — issue #130 Tier-1.

For each ``_behavioral_q1`` query in the input directory, ask an LLM to rewrite
it as a 5–30 word symptom-style query that:

- Does **not** reuse any noun from the GT file path, module name, or function name.
- Uses synonyms where possible (\"linter\" → \"static checker\", \"VOTable\" → \"that XML
  table format\", etc.).
- Reads like a one-sentence bug report a developer would file on GitHub.

The output mirrors the input schema, with ``query_id`` re-suffixed ``_behavioral_q2``
and a new ``rewritten_from_query_id`` pointer. The emitted directory can be passed
straight to ``examples/eval_synthesized_queries.py --queries-dir`` for re-eval.

Predicted result per #130: file-recall@10 drops from 0.92 → ~0.65, symbol-recall@10
from 0.73 → ~0.40.

Usage
-----

    python scripts/synthesize_q2_short.py \\
        --input-dir synthesis_output/ \\
        --output-dir synthesis_output_q2_short/ \\
        --model vertex_ai/gemini-2.5-flash

    # Single-instance smoke test
    python scripts/synthesize_q2_short.py \\
        --input-dir synthesis_output/ \\
        --output-dir /tmp/q2_test/ \\
        --filter-instance "astral-sh__ruff-15309" \\
        --model vertex_ai/gemini-2.5-flash
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from pydantic import BaseModel, Field  # noqa: E402

from codeminer.llm.litellm_chat import (  # noqa: E402
    LiteLLMChat,
    human_message,
    system_message,
)
from codeminer.log_utils import get_logger  # noqa: E402

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schema for the structured output.
# ---------------------------------------------------------------------------


class ShortQueryRewrite(BaseModel):
    """One-shot short rewrite of a behavioral query."""

    rewritten_query: str = Field(
        description="A 5–30 word symptom-style developer query reusing no banned tokens.",
    )
    banned_tokens_used: List[str] = Field(
        default_factory=list,
        description="Banned tokens that still slipped into the rewrite (for QA).",
    )


# ---------------------------------------------------------------------------
# Field-name compatibility helpers (mirror diagnose_query_leak.py).
# ---------------------------------------------------------------------------


def _gt_files(entry: Dict[str, Any]) -> List[str]:
    return list(entry.get("gt_files") or entry.get("target_files") or [])


def _gt_symbols(entry: Dict[str, Any]) -> List[str]:
    return list(entry.get("gt_symbols") or entry.get("target_symbols") or [])


def _query_text(entry: Dict[str, Any]) -> str:
    return str(entry.get("query") or entry.get("question") or "")


# ---------------------------------------------------------------------------
# Banned-token extraction (lifted from diagnose_query_leak.py).
# ---------------------------------------------------------------------------


def _camel_split(token: str) -> List[str]:
    parts: List[str] = []
    for part in token.replace("-", "_").split("_"):
        if not part:
            continue
        sub = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", part)
        parts.extend(s.lower() for s in sub if s)
    return parts


def _tokens_from_symbol(symbol: str) -> List[str]:
    raw = re.split(r"[./:\\#]+", symbol)
    return [t for chunk in raw for t in _camel_split(chunk) if len(t) > 1]


def _tokens_from_path(path: str) -> List[str]:
    parts = re.split(r"[\\/]+", path)
    if parts:
        parts[-1] = re.sub(r"\.[A-Za-z0-9]+$", "", parts[-1])
    return [t for part in parts for t in _camel_split(part) if len(t) > 1]


def banned_tokens(entry: Dict[str, Any]) -> List[str]:
    """All sub-tokens drawn from GT file paths and GT symbol identifiers."""
    bans = set()
    for s in _gt_symbols(entry):
        bans.update(_tokens_from_symbol(s))
    for f in _gt_files(entry):
        bans.update(_tokens_from_path(f))
    return sorted(bans)


# ---------------------------------------------------------------------------
# Prompting.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You rewrite verbose technical bug descriptions as short, symptom-style developer
queries — the kind of one-sentence question a real engineer would file on GitHub
or post on Stack Overflow.

Constraints (must hold):
- 5 to 30 words.
- Do NOT use any of the BANNED_TOKENS (case-insensitive). Use plain English
  synonyms instead. If a banned token names a domain concept, paraphrase it.
- Describe the symptom, not the implementation. Avoid pointing at file paths,
  class names, or function names.
- Keep the rewrite grounded in the same defect — do not invent new behaviour.
- Output only the rewritten query and a list of any banned tokens that slipped
  through (should normally be empty).
"""


def build_user_prompt(query: str, bans: Sequence[str]) -> str:
    return (
        f"ORIGINAL_QUERY:\n{query}\n\n"
        f"BANNED_TOKENS:\n{', '.join(bans) if bans else '(none)'}\n\n"
        "Produce a single short rewrite obeying every constraint."
    )


# ---------------------------------------------------------------------------
# Per-entry rewrite.
# ---------------------------------------------------------------------------


def rewrite_entry(
    llm: LiteLLMChat,
    entry: Dict[str, Any],
    max_retries: int = 2,
) -> Optional[Dict[str, Any]]:
    bans = banned_tokens(entry)
    structured = llm.with_structured_output(ShortQueryRewrite)
    messages = [
        system_message(SYSTEM_PROMPT),
        human_message(build_user_prompt(_query_text(entry), bans)),
    ]
    last_err: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            result = structured.invoke(messages)
            new_entry = dict(entry)
            qid = entry.get("query_id") or entry.get("instance_id") or "unknown"
            base_qid = re.sub(r"_behavioral_q\d+$", "", str(qid))
            new_entry["query_id"] = f"{base_qid}_behavioral_q2"
            new_entry["query"] = result.rewritten_query
            new_entry["query_type"] = "behavioral_short"
            new_entry["rewritten_from_query_id"] = qid
            new_entry["banned_tokens"] = bans
            new_entry["banned_tokens_leaked"] = result.banned_tokens_used
            new_entry["n_words"] = len(result.rewritten_query.split())
            return new_entry
        except Exception as exc:
            last_err = exc
            logger.warning(
                "Rewrite attempt %d failed for %s: %s",
                attempt + 1,
                entry.get("query_id"),
                exc,
            )
            time.sleep(1.0 * (attempt + 1))
    logger.error(
        "Giving up on %s after %d retries: %s",
        entry.get("query_id"),
        max_retries,
        last_err,
    )
    return None


# ---------------------------------------------------------------------------
# IO helpers.
# ---------------------------------------------------------------------------


def load_input(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Return {instance_id: [entries]} from a directory of per-instance JSONs."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    if path.is_dir():
        for f in sorted(path.glob("*.json")):
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            entries = data if isinstance(data, list) else [data]
            if not entries:
                continue
            inst = str(entries[0].get("instance_id") or f.stem)
            out.setdefault(inst, []).extend(entries)
    else:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        entries = data if isinstance(data, list) else [data]
        for e in entries:
            out.setdefault(str(e.get("instance_id") or "unknown"), []).append(e)
    return out


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--input-dir",
        required=True,
        type=Path,
        help="Directory of per-instance synthesized query JSONs (q1 inputs).",
    )
    p.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Where to write per-instance q2 short variant JSONs.",
    )
    p.add_argument(
        "--model",
        default="vertex_ai/gemini-2.5-flash",
        help="litellm model id used for the rewrite.",
    )
    p.add_argument(
        "--temperature",
        type=float,
        default=0.3,
        help="Sampling temperature — small >0 keeps phrasing varied without drift.",
    )
    p.add_argument("--max-tokens", type=int, default=256)
    p.add_argument(
        "--filter-instance",
        type=str,
        default=".*",
        help="Regex over instance_id to restrict the rewrite set.",
    )
    p.add_argument(
        "--only-q1",
        action="store_true",
        default=True,
        help="Only rewrite entries whose query_id ends in _behavioral_q1.",
    )
    p.add_argument(
        "--all-queries",
        dest="only_q1",
        action="store_false",
        help="Rewrite every input entry, regardless of query_type.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    in_dir = args.input_dir.expanduser().resolve()
    out_dir = args.output_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    grouped = load_input(in_dir)
    pat = re.compile(args.filter_instance)
    grouped = {k: v for k, v in grouped.items() if pat.search(k)}
    if not grouped:
        print("No instances matched --filter-instance.", file=sys.stderr)
        return 1
    total = sum(len(v) for v in grouped.values())
    logger.info("Loaded %d entries across %d instances", total, len(grouped))

    llm = LiteLLMChat(
        model=args.model, temperature=args.temperature, max_tokens=args.max_tokens
    )

    n_done = 0
    n_failed = 0
    for inst, entries in grouped.items():
        rewritten: List[Dict[str, Any]] = []
        for entry in entries:
            qid = str(entry.get("query_id") or "")
            if args.only_q1 and not qid.endswith("_behavioral_q1"):
                continue
            new_entry = rewrite_entry(llm, entry)
            if new_entry is None:
                n_failed += 1
                continue
            rewritten.append(new_entry)
            n_done += 1
            logger.info(
                "[%s] q1(%d w) → q2(%d w) leaked=%s",
                new_entry["query_id"],
                len(_query_text(entry).split()),
                new_entry["n_words"],
                new_entry["banned_tokens_leaked"],
            )
        if not rewritten:
            continue
        safe = inst.replace("/", "__")
        out_path = out_dir / f"{safe}.json"
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(rewritten, fh, indent=2, ensure_ascii=False)
    logger.info("Done — %d rewritten, %d failed", n_done, n_failed)
    print(f"Wrote {n_done} rewritten queries to {out_dir} ({n_failed} failed)")
    return 0 if n_done > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
