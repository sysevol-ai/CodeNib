#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Partition SWE-bench Multilingual instances into a fitting / held-out split.

Issue #133 Phase 0 requires a stable split:

* Repos (not individual instances) are assigned to a side to avoid
  intra-repo leakage between fit and held-out.
* Within each language, the fit side is filled until it reaches its
  per-language quota (≈``fit_size / num_languages``).
* The scenario coverage check requires each ``(language, has_stacktrace)``
  cell to carry ≥ 2 fitting instances; if not, reseed up to 3× then
  accept the imbalance and record a warning.

The partition algorithm is a pure function (:func:`build_partition`) so
``scripts/agent_compile/partition.py`` itself is just CLI glue.

Input format (``--instances``):
    A JSON list of objects with at minimum ``instance_id``, ``repo``,
    ``language``, and ``problem_statement`` keys. This matches what
    ``scripts/collect_swebench.py`` already produces and what the eval
    drivers feed into ``SwebenchMultilingualDataset.process_instance``.

Output format (``--output``):
    ``{seed, fit_size, heldout_size, fit, heldout, cells, attempts,
    warnings}`` written as JSON.

Usage::

    python scripts/agent_compile/partition.py \\
        --instances data/swebench_multilingual_candidates.json \\
        --seed 20260514 \\
        --fit-size 30 \\
        --heldout-size 70 \\
        --output data/agent_compile/partition.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Imports from the package (so this module is testable as
# ``scripts.agent_compile.partition``).
from codeminer.agent.compile import detect_stacktrace, normalize_language

# Maximum reseed attempts when a (language, has_stacktrace) cell is empty
# or under-represented. Per RFC v2.
DEFAULT_MAX_RESEED = 3

# Minimum instances per scenario cell on the fit side. Cells below this
# threshold trigger a reseed; if all attempts fail, the partition is
# accepted with a warning.
MIN_CELL_INSTANCES = 2


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstanceRow:
    """The minimum fields the partition algorithm cares about.

    Anything else from the upstream JSON (commit hash, patch, etc.) is
    ignored at partition time. The eval driver re-reads the full row from
    the dataset later, keyed by ``instance_id``.
    """

    instance_id: str
    repo: str
    language: Optional[str]
    has_stacktrace: bool


@dataclass
class Partition:
    seed: int
    fit_size: int
    heldout_size: int
    fit: List[str] = field(default_factory=list)
    heldout: List[str] = field(default_factory=list)
    cells: Dict[str, int] = field(default_factory=dict)
    attempts: int = 1
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Pure-function partition algorithm
# ---------------------------------------------------------------------------


def rows_from_json(payload: Sequence[Dict[str, Any]]) -> List[InstanceRow]:
    """Project arbitrary JSON rows into ``InstanceRow`` records.

    Skips rows missing ``instance_id`` / ``repo`` and logs the count of
    discarded entries to stderr (no logger here — this is a script-level
    utility).
    """
    out: List[InstanceRow] = []
    skipped = 0
    for row in payload:
        iid = row.get("instance_id")
        repo = row.get("repo")
        if not iid or not repo:
            skipped += 1
            continue
        lang_raw = row.get("language") or row.get("Language")
        problem = row.get("problem_statement", "") or ""
        out.append(
            InstanceRow(
                instance_id=str(iid),
                repo=str(repo),
                language=normalize_language(lang_raw),
                has_stacktrace=detect_stacktrace(problem),
            )
        )
    if skipped:
        print(
            f"partition: skipped {skipped} rows missing instance_id/repo",
            file=sys.stderr,
        )
    return out


def _bucket_by_repo(
    rows: Sequence[InstanceRow],
) -> Dict[str, List[InstanceRow]]:
    """Group rows by (language, repo). Languages stay separate even if a
    repo accidentally appears under more than one language string."""
    buckets: Dict[str, List[InstanceRow]] = defaultdict(list)
    for r in rows:
        buckets[r.repo].append(r)
    return buckets


def _repos_by_language(
    repo_buckets: Dict[str, List[InstanceRow]],
) -> Dict[Optional[str], List[str]]:
    """For each language, list repos that have at least one instance in it."""
    by_lang: Dict[Optional[str], List[str]] = defaultdict(list)
    for repo, items in repo_buckets.items():
        # A repo's language is the modal language of its instances. In
        # practice SWE-bench Multilingual repos are mono-lingual; this
        # guards against stray rows.
        langs = [r.language for r in items if r.language is not None]
        if langs:
            primary = max(set(langs), key=langs.count)
        else:
            primary = None
        by_lang[primary].append(repo)
    for lst in by_lang.values():
        lst.sort()  # deterministic ordering before shuffle
    return by_lang


def _cell_counts(rows: Iterable[InstanceRow]) -> Dict[str, int]:
    """Count instances per ``(language, has_stacktrace)`` scenario key."""
    counts: Counter[str] = Counter()
    for r in rows:
        lang = r.language or "unknown"
        flag = "stacktrace" if r.has_stacktrace else "no_stacktrace"
        counts[f"{lang}:{flag}"] += 1
    return dict(counts)


def _attempt(
    rows: Sequence[InstanceRow],
    seed: int,
    fit_size: int,
    heldout_size: int,
) -> Tuple[List[InstanceRow], List[InstanceRow]]:
    """One round of repo-level random assignment.

    Per language, repos are shuffled deterministically and consumed until
    the language's fit quota is met. Remaining repos in that language go
    to held-out. The held-out side is truncated to ``heldout_size``.
    """
    rng = random.Random(seed)
    repo_buckets = _bucket_by_repo(rows)
    by_lang = _repos_by_language(repo_buckets)

    languages = sorted(
        [lang for lang in by_lang.keys() if lang is not None],
        key=lambda x: x or "",
    )
    if None in by_lang:
        languages.append(None)

    if not languages:
        return [], []

    # Per-language fit quota — divide fit_size as evenly as possible.
    base = fit_size // len(languages)
    remainder = fit_size - base * len(languages)
    quotas: Dict[Optional[str], int] = {}
    for i, lang in enumerate(languages):
        quotas[lang] = base + (1 if i < remainder else 0)

    fit: List[InstanceRow] = []
    heldout: List[InstanceRow] = []

    for lang in languages:
        repos = list(by_lang[lang])
        rng.shuffle(repos)
        quota = quotas[lang]
        used = 0
        for repo in repos:
            instances = repo_buckets[repo]
            # Whole-repo assignment: if we haven't filled this
            # language's quota yet, take the entire repo into fit,
            # accepting a slight overshoot. Otherwise it goes to
            # held-out. This preserves the repo-level partitioning
            # the RFC requires.
            if used < quota:
                fit.extend(instances)
                used += len(instances)
            else:
                heldout.extend(instances)

    if heldout_size and len(heldout) > heldout_size:
        # Deterministic trim with the same rng to keep partitions
        # reproducible.
        rng.shuffle(heldout)
        heldout = heldout[:heldout_size]

    return fit, heldout


def build_partition(
    rows: Sequence[InstanceRow],
    seed: int,
    *,
    fit_size: int = 30,
    heldout_size: int = 70,
    max_reseed: int = DEFAULT_MAX_RESEED,
    min_cell_instances: int = MIN_CELL_INSTANCES,
) -> Partition:
    """Run the partition algorithm with reseed-on-cell-imbalance.

    Coverage is checked against cells the *corpus* can actually fill:
    a cell with fewer than ``min_cell_instances`` rows corpus-wide
    cannot be improved by reseeding, so we don't burn attempts on it —
    we accept and warn at the end. Cells the corpus can fill but the
    current shuffle under-fills do trigger reseed.

    Returns a :class:`Partition`. The caller serialises to JSON.
    """
    # Expected cells: the cross of {observed languages} × {True, False}.
    # We then split them into two groups:
    #   - reachable: corpus has ≥ min_cell_instances of this cell, so
    #     reseeding can plausibly fix an under-filled fit
    #   - scarce: corpus has fewer than min_cell_instances, so no
    #     reseed can manifest more instances
    corpus_cells = _cell_counts(rows)
    observed_langs = {r.language or "unknown" for r in rows}
    expected_keys: List[str] = []
    scarce_keys: List[str] = []
    for lang in sorted(observed_langs):
        for flag in ("stacktrace", "no_stacktrace"):
            key = f"{lang}:{flag}"
            n = corpus_cells.get(key, 0)
            if n >= min_cell_instances:
                expected_keys.append(key)
            else:
                scarce_keys.append(f"{key} (corpus n={n})")

    fit: List[InstanceRow] = []
    heldout: List[InstanceRow] = []
    cells: Dict[str, int] = {}
    warnings: List[str] = []

    current_seed = seed
    attempts = 0
    under: List[str] = []
    for attempt in range(max_reseed):
        attempts = attempt + 1
        fit, heldout = _attempt(rows, current_seed, fit_size, heldout_size)
        cells = _cell_counts(fit)
        under = [
            f"{key} (n={cells.get(key, 0)})"
            for key in expected_keys
            if cells.get(key, 0) < min_cell_instances
        ]
        if not under:
            break
        current_seed = current_seed * 1_000_003 + 1
    if under:
        warnings.append(
            "Cell coverage check failed after "
            f"{max_reseed} attempts: {', '.join(under)}"
        )
    if scarce_keys:
        warnings.append("Corpus too thin to populate cells: " + ", ".join(scarce_keys))

    return Partition(
        seed=seed,
        fit_size=fit_size,
        heldout_size=heldout_size,
        fit=[r.instance_id for r in fit],
        heldout=[r.instance_id for r in heldout],
        cells=cells,
        attempts=attempts,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# CLI glue
# ---------------------------------------------------------------------------


def _read_instances_json(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if isinstance(payload, dict) and "instances" in payload:
        payload = payload["instances"]
    if not isinstance(payload, list):
        raise ValueError(
            f"{path} must be a JSON list (or {{'instances': [...]}}), "
            f"got {type(payload).__name__}"
        )
    return payload


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Partition SWE-bench Multilingual into fit / held-out.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--instances",
        required=True,
        type=Path,
        help=(
            "Path to a JSON list of instance rows (each row carries "
            "instance_id, repo, language, problem_statement)."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        required=True,
        help="Random seed (frozen in the Phase 1 ADR).",
    )
    parser.add_argument(
        "--fit-size",
        type=int,
        default=30,
        help="Number of instances in the fitting pool.",
    )
    parser.add_argument(
        "--heldout-size",
        type=int,
        default=70,
        help="Cap on held-out instances (extras are discarded).",
    )
    parser.add_argument(
        "--max-reseed",
        type=int,
        default=DEFAULT_MAX_RESEED,
        help="Max reseed attempts before accepting cell imbalance.",
    )
    parser.add_argument(
        "--min-cell-instances",
        type=int,
        default=MIN_CELL_INSTANCES,
        help="Minimum instances per (language, has_stacktrace) cell.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Where to write partition.json.",
    )
    args = parser.parse_args(argv)

    payload = _read_instances_json(args.instances)
    rows = rows_from_json(payload)
    if not rows:
        print("partition: no usable instance rows; aborting", file=sys.stderr)
        return 2

    partition = build_partition(
        rows,
        args.seed,
        fit_size=args.fit_size,
        heldout_size=args.heldout_size,
        max_reseed=args.max_reseed,
        min_cell_instances=args.min_cell_instances,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(partition.to_dict(), f, indent=2, ensure_ascii=False)

    print(
        "partition: fit={fit} heldout={ho} cells={cells} attempts={a}"
        " warnings={w}".format(
            fit=len(partition.fit),
            ho=len(partition.heldout),
            cells=len(partition.cells),
            a=partition.attempts,
            w=len(partition.warnings),
        )
    )
    for warning in partition.warnings:
        print(f"partition: WARN {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
