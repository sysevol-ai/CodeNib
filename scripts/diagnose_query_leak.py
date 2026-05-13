#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Diagnose vocabulary / lexical leak in synthesized retrieval queries.

Operationalizes the Tier-3 checks from issue #130:

- Per-query leak signals (`fn_leak`, `sym_leak`, `path_leak`, `any_leak`).
- Recall slices by leaked-vs-clean and by length bucket (≤30 / 30–60 / >60 words).
- Optional emission of a masked query variant — any token appearing in any GT
  symbol or path is stripped — that the user can re-feed through
  ``examples/eval_synthesized_queries.py``.

Inputs:

- ``--queries`` : a directory or single JSON file in the same format the
  synthesizer emits / ``eval_synthesized_queries.py`` consumes.
- ``--eval-result`` (optional) : the ``--result-path`` JSON produced by
  ``eval_synthesized_queries.py``. When present, recall slices are reported.

Outputs:

- A printed table to stdout.
- ``--emit-masked-queries DIR`` : write per-instance masked query JSONs ready
  for a re-run.
- ``--emit-leak-report PATH`` : write the per-query leak signals as JSON.

The script is deliberately dependency-free (stdlib only) so it can be run
without an environment install.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

# ---------------------------------------------------------------------------
# Field-name normalization — synthesizer emits target_*, older artifacts use gt_*.
# ---------------------------------------------------------------------------


def _gt_files(entry: Dict[str, Any]) -> List[str]:
    return list(entry.get("gt_files") or entry.get("target_files") or [])


def _gt_symbols(entry: Dict[str, Any]) -> List[str]:
    return list(entry.get("gt_symbols") or entry.get("target_symbols") or [])


def _query_text(entry: Dict[str, Any]) -> str:
    return str(entry.get("query") or entry.get("question") or "")


def _query_id(entry: Dict[str, Any]) -> str:
    return str(entry.get("query_id") or entry.get("instance_id") or "")


# ---------------------------------------------------------------------------
# Tokenization (case-insensitive, snake/camel split).
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def _camel_split(token: str) -> List[str]:
    """Split CamelCase / snake_case into lowercase sub-tokens."""
    parts: List[str] = []
    for part in token.replace("-", "_").split("_"):
        if not part:
            continue
        # Split on lower->Upper boundaries.
        sub = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", part)
        parts.extend(s.lower() for s in sub if s)
    return parts


def _tokenize_query(text: str) -> Set[str]:
    return {m.group(0).lower() for m in _WORD_RE.finditer(text)}


def _tokens_from_symbol(symbol: str) -> List[str]:
    """All meaningful sub-tokens from a symbol id.

    Splits on ``.`` / ``::`` / ``/`` / ``\\`` and then on snake / camel case.
    Single-character tokens are dropped.
    """
    raw = re.split(r"[./:\\#]+", symbol)
    out: List[str] = []
    for chunk in raw:
        for tok in _camel_split(chunk):
            if len(tok) > 1:
                out.append(tok)
    return out


def _tokens_from_path(path: str) -> List[str]:
    parts = re.split(r"[\\/]+", path)
    if parts:
        parts[-1] = re.sub(r"\.[A-Za-z0-9]+$", "", parts[-1])  # drop extension
    out: List[str] = []
    for part in parts:
        for tok in _camel_split(part):
            if len(tok) > 1:
                out.append(tok)
    return out


# ---------------------------------------------------------------------------
# Leak detection.
# ---------------------------------------------------------------------------


@dataclass
class LeakSignals:
    fn_leak: bool
    sym_leak: bool
    path_leak: bool

    @property
    def any_leak(self) -> bool:
        return self.fn_leak or self.sym_leak or self.path_leak


def detect_leaks(entry: Dict[str, Any]) -> LeakSignals:
    """Match the three leak signals from issue #130."""
    query_text = _query_text(entry)
    qtokens = _tokenize_query(query_text)
    qlower = query_text.lower()

    fn_leak = False
    for f in _gt_files(entry):
        base = Path(f).name.lower()
        if base and base in qlower:
            fn_leak = True
            break

    sym_leak = False
    for s in _gt_symbols(entry):
        sym_tokens = _tokens_from_symbol(s)
        if not sym_tokens:
            continue
        # Last sub-token is the most distinctive.
        last_part = re.split(r"[./:#\\]+", s)[-1]
        # If any case-split sub-token of the last part appears in the query, flag.
        if any(tok in qtokens for tok in _camel_split(last_part) if len(tok) > 1):
            sym_leak = True
            break

    path_leak = False
    for f in _gt_files(entry):
        if any(tok in qtokens for tok in _tokens_from_path(f)):
            path_leak = True
            break

    return LeakSignals(fn_leak=fn_leak, sym_leak=sym_leak, path_leak=path_leak)


# ---------------------------------------------------------------------------
# Length stratification.
# ---------------------------------------------------------------------------


def length_bucket(text: str) -> str:
    n = len(text.split())
    if n <= 30:
        return "<=30"
    if n <= 60:
        return "31-60"
    return ">60"


# ---------------------------------------------------------------------------
# Masked-query emission.
# ---------------------------------------------------------------------------


def mask_query(
    text: str, ban_tokens: Iterable[str], placeholder: str = "[MASK]"
) -> str:
    """Replace each banned sub-token (case-insensitive, word-boundary) with placeholder."""
    bans = {t.lower() for t in ban_tokens if t and len(t) > 1}
    if not bans:
        return text
    # Single regex; \b only matches word boundaries on ASCII letters/digits.
    pattern = re.compile(
        r"\b("
        + "|".join(re.escape(t) for t in sorted(bans, key=len, reverse=True))
        + r")\b",
        re.IGNORECASE,
    )
    return pattern.sub(placeholder, text)


def make_masked_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of the entry with the query string masked of GT vocabulary."""
    bans: Set[str] = set()
    for s in _gt_symbols(entry):
        bans.update(_tokens_from_symbol(s))
    for f in _gt_files(entry):
        bans.update(_tokens_from_path(f))

    new_entry = dict(entry)
    new_entry["query"] = mask_query(_query_text(entry), bans)
    qid = new_entry.get("query_id")
    if qid:
        new_entry["query_id"] = f"{qid}_masked"
    new_entry["masked_from_query_id"] = entry.get("query_id")
    new_entry["masked_banned_tokens"] = sorted(bans)
    return new_entry


# ---------------------------------------------------------------------------
# Loading queries + eval results.
# ---------------------------------------------------------------------------


def load_queries(path: Path) -> List[Dict[str, Any]]:
    """Accept either a directory of per-instance JSONs or a single file."""
    if path.is_dir():
        out: List[Dict[str, Any]] = []
        for f in sorted(path.glob("*.json")):
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                out.extend(data)
            elif isinstance(data, dict):
                out.append(data)
        return out
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, list) else [data]


def load_eval_results(path: Path) -> Dict[str, Dict[str, Any]]:
    """Index ``eval_synthesized_queries.py --result-path`` JSON by query_id."""
    with open(path, encoding="utf-8") as fh:
        records = json.load(fh)
    return {str(r.get("query_id") or r.get("instance_id")): r for r in records}


# ---------------------------------------------------------------------------
# Recall extraction from result records.
# ---------------------------------------------------------------------------


def _scope_recall_at_k(record: Dict[str, Any], scope: str, k: int) -> Optional[float]:
    metrics = record.get("metrics") or {}
    bucket = metrics.get(scope) or {}
    cell = bucket.get(str(k)) or bucket.get(k)
    if not cell:
        return None
    val = cell.get("recall")
    return float(val) if val is not None else None


# ---------------------------------------------------------------------------
# Reporting.
# ---------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float:
    vs = [v for v in values if v is not None]
    return sum(vs) / len(vs) if vs else 0.0


def _format_table(
    title: str, header: Sequence[str], rows: Sequence[Sequence[str]]
) -> str:
    cols = list(zip(header, *rows, strict=False))
    widths = [max(len(str(c)) for c in col) for col in cols]
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"

    def fmt_row(row: Sequence[Any]) -> str:
        cells = " | ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=False))
        return f"| {cells} |"

    lines = [f"\n### {title}", sep, fmt_row(header), sep]
    for r in rows:
        lines.append(fmt_row(r))
    lines.append(sep)
    return "\n".join(lines)


def report(
    entries: List[Dict[str, Any]],
    results: Optional[Dict[str, Dict[str, Any]]],
    ks: Sequence[int],
) -> str:
    """Build the printable diagnostic report."""
    out: List[str] = []

    # 1. Per-query leak signals (always available).
    leaks = [detect_leaks(e) for e in entries]
    n = len(entries)
    n_fn = sum(1 for L in leaks if L.fn_leak)
    n_sym = sum(1 for L in leaks if L.sym_leak)
    n_path = sum(1 for L in leaks if L.path_leak)
    n_any = sum(1 for L in leaks if L.any_leak)

    out.append(
        _format_table(
            f"Leak signals — {n} queries",
            ["signal", "count", "prevalence"],
            [
                ("fn_leak", n_fn, f"{n_fn / max(n, 1):.0%}"),
                ("sym_leak", n_sym, f"{n_sym / max(n, 1):.0%}"),
                ("path_leak", n_path, f"{n_path / max(n, 1):.0%}"),
                ("any_leak", n_any, f"{n_any / max(n, 1):.0%}"),
            ],
        )
    )

    # 2. Length distribution.
    buckets: Counter[str] = Counter(length_bucket(_query_text(e)) for e in entries)
    out.append(
        _format_table(
            "Query length distribution (words)",
            ["bucket", "count", "share"],
            [
                (b, buckets.get(b, 0), f"{buckets.get(b, 0) / max(n, 1):.0%}")
                for b in ("<=30", "31-60", ">60")
            ],
        )
    )

    # 3. Recall slices — only when eval results are supplied.
    if results:
        # Slice A: leaked vs clean.
        scope_rows = []
        for scope in ("files", "symbols"):
            for k in ks:
                lk, cl = [], []
                for e, L in zip(entries, leaks, strict=False):
                    rec = results.get(_query_id(e))
                    if not rec:
                        continue
                    r = _scope_recall_at_k(rec, scope, k)
                    if r is None:
                        continue
                    (lk if L.any_leak else cl).append(r)
                scope_rows.append(
                    (
                        scope,
                        k,
                        f"{_mean(lk):.3f} ({len(lk)})",
                        f"{_mean(cl):.3f} ({len(cl)})",
                        f"{_mean(lk) - _mean(cl):+.3f}",
                    )
                )
        out.append(
            _format_table(
                "Recall@k — leaked vs clean",
                ["scope", "k", "leaked", "clean", "delta"],
                scope_rows,
            )
        )

        # Slice B: length bucket.
        bucket_rows = []
        for scope in ("files", "symbols"):
            for k in ks:
                cells: Dict[str, List[float]] = defaultdict(list)
                for e in entries:
                    rec = results.get(_query_id(e))
                    if not rec:
                        continue
                    r = _scope_recall_at_k(rec, scope, k)
                    if r is None:
                        continue
                    cells[length_bucket(_query_text(e))].append(r)
                bucket_rows.append(
                    (
                        scope,
                        k,
                        f"{_mean(cells['<=30']):.3f} ({len(cells['<=30'])})",
                        f"{_mean(cells['31-60']):.3f} ({len(cells['31-60'])})",
                        f"{_mean(cells['>60']):.3f} ({len(cells['>60'])})",
                    )
                )
        out.append(
            _format_table(
                "Recall@k — length-stratified",
                ["scope", "k", "<=30 w", "31-60 w", ">60 w"],
                bucket_rows,
            )
        )

    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--queries",
        required=True,
        type=Path,
        help="Synthesized queries: directory of per-instance JSONs or a single file.",
    )
    p.add_argument(
        "--eval-result",
        type=Path,
        default=None,
        help="--result-path JSON from eval_synthesized_queries.py (enables recall slices).",
    )
    p.add_argument(
        "--metrics-k",
        type=int,
        nargs="+",
        default=[1, 5, 10],
        help="Which k values to slice recall at.",
    )
    p.add_argument(
        "--emit-masked-queries",
        type=Path,
        default=None,
        help="If set, write per-instance masked JSONs into this directory.",
    )
    p.add_argument(
        "--emit-leak-report",
        type=Path,
        default=None,
        help="If set, write per-query leak signals (JSON list) to this path.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    entries = load_queries(args.queries.expanduser().resolve())
    if not entries:
        print("No queries loaded — check --queries path.", file=sys.stderr)
        return 1

    results = (
        load_eval_results(args.eval_result.expanduser().resolve())
        if args.eval_result
        else None
    )

    print(report(entries, results, args.metrics_k))

    if args.emit_leak_report:
        out_path = args.emit_leak_report.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "query_id": _query_id(e),
                "instance_id": e.get("instance_id"),
                "n_words": len(_query_text(e).split()),
                "length_bucket": length_bucket(_query_text(e)),
                "leak": {
                    "fn_leak": L.fn_leak,
                    "sym_leak": L.sym_leak,
                    "path_leak": L.path_leak,
                    "any_leak": L.any_leak,
                },
            }
            for e, L in zip(entries, (detect_leaks(e) for e in entries), strict=False)
        ]
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        print(f"Leak report → {out_path}")

    if args.emit_masked_queries:
        out_dir = args.emit_masked_queries.expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        per_instance: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for e in entries:
            per_instance[str(e.get("instance_id") or "unknown")].append(
                make_masked_entry(e)
            )
        for inst_id, items in per_instance.items():
            safe = inst_id.replace("/", "__")
            with open(out_dir / f"{safe}.json", "w", encoding="utf-8") as fh:
                json.dump(items, fh, indent=2, ensure_ascii=False)
        print(
            f"Masked queries → {out_dir} ({sum(len(v) for v in per_instance.values())} entries)"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
