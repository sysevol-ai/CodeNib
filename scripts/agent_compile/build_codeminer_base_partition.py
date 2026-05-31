#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Freeze the codeminer-base 30/70 partition + per-instance scenario labels.

Implements issue #151 (sub-issue of the Agent Router RFC #133): emit the single
frozen fitting/held-out partition that every Phase 0/2 experiment cell consumes.

This is a thin *driver*: it reuses the existing repo-level split algorithm
(:func:`scripts.agent_compile.partition.build_partition`) and the canonical
stack-trace detector (:func:`codeminer.agent.compile.detect_stacktrace`). It
does **not** reimplement the partition logic and it does **not** collect a new
corpus -- it reuses the existing ~100-instance ``codeminer-base`` corpus that
the retrieval baselines already load (HuggingFace id
``fishmingyu/codeminer-base-dataset``).

Algorithm (delegated to ``build_partition``)
--------------------------------------------
Per language, whole *repos* (never instances) are deterministically shuffled
with a fixed integer seed and assigned to ``fit`` until the per-language quota
(~``fit_size / num_languages``) is met; the rest go to ``heldout``. Repo-level
assignment keeps a repository wholly inside one split so no held-out repo leaks
into fitting. Scenario coverage is then verified: each
``(language, has_stacktrace)`` cell that the corpus can actually fill must hold
>= 2 fitting instances; otherwise the algorithm reseeds up to 3 times, then
accepts and records a warning. Cells the corpus cannot fill (e.g. Rust has no
stack-trace instances) are flagged as scarce rather than failing.

Per-instance scenario labels
----------------------------
- ``has_stacktrace`` -- ``detect_stacktrace`` over ``problem_statement`` +
  ``hints_text`` (Python ``Traceback``, Go ``panic:`` / goroutine dump, Rust
  ``thread '..' panicked``, JS/Node ``at fn (file:line)``, C/C++ backtrace).
- ``language`` -- the corpus ``language_group`` field (repo primary language).
  The codeminer-base corpus groups C/C++ and TS/JS, so its own grouping is
  used verbatim as the per-language partition axis (5 groups).

Output (both committed)
-----------------------
- ``--out`` (default ``data/agent_compile/partition.json``): the partition in
  the exact schema ``run_sweep.py`` consumes (``fit`` / ``heldout`` /
  ``cells`` / ``warnings`` / ...), augmented with a ``labels`` table and a
  ``scenario`` summary.
- ``--csv`` (default ``data/agent_compile/partition_labels.csv``): a flat
  per-instance label table for the eval harness + CAR runtime.

Determinism: the only randomness is the seed passed to ``build_partition``
(default fixed literal ``DEFAULT_SEED``), so re-running emits identical output.

Corpus loading is stdlib-only against the local JSON-lines cache; ``--source
hf`` opts into the HuggingFace loader when the cache is absent.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# Reuse the partition algorithm + stack-trace detector. ``partition`` lives
# under scripts/ (a package via ``scripts/agent_compile/__init__.py``), so a
# relative import works when run as a module and the path shim below covers
# direct ``python scripts/agent_compile/build_codeminer_base_partition.py``.
try:  # pragma: no cover - exercised via both entry styles
    from .partition import InstanceRow, build_partition
except ImportError:  # pragma: no cover - direct-script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.agent_compile.partition import InstanceRow, build_partition

#: Fixed integer seed literal -- deterministic. Frozen in the Phase 1 ADR;
#: change only via a new ADR. Never replace with a nondeterministic source.
#: Chosen because it lands both *satisfiable* scenario cells the corpus can
#: fill -- ``C++/C:stacktrace`` (corpus n=3) and ``Go:stacktrace`` (corpus
#: n=2) -- with >= 2 fitting instances. The remaining empty stacktrace cells
#: (Python n=1, Rust n=0, TS/JS n=1) are corpus-structural and flagged as
#: scarce, not fixable by reseeding.
DEFAULT_SEED = 12

#: HuggingFace id of the reused corpus (never re-collected).
DATASET_ID = "fishmingyu/codeminer-base-dataset"

#: Default on-disk JSON-lines cache written by ``CodeMinerBaseDataset``.
DEFAULT_CACHE = (
    Path.home() / ".codeminer" / "fishmingyu__codeminer-base-dataset_test.json"
)

#: Target split sizes for the ~100-instance corpus (~30 fit / ~70 held-out).
DEFAULT_FIT_SIZE = 30
DEFAULT_HELDOUT_SIZE = 70

#: Where the frozen artifacts are committed (consumed by ``run_sweep.py``).
DEFAULT_OUT = Path("data/agent_compile/partition.json")
DEFAULT_CSV = Path("data/agent_compile/partition_labels.csv")


# ---------------------------------------------------------------------------
# Corpus loading.
# ---------------------------------------------------------------------------


def load_corpus(
    source: str = "auto",
    cache_path: Optional[Path] = None,
    dataset_id: str = DATASET_ID,
    split: str = "test",
) -> List[Dict[str, Any]]:
    """Load the reused corpus as a list of plain dict rows.

    ``source``: ``cache`` (local JSON-lines, stdlib only), ``hf``
    (``CodeMinerBaseDataset``, needs ``datasets``), or ``auto`` (cache then
    hf).
    """
    cache_path = Path(cache_path) if cache_path else DEFAULT_CACHE
    if source in ("auto", "cache") and cache_path.exists():
        return _load_cache(cache_path)
    if source == "cache":
        raise FileNotFoundError(
            f"Corpus cache not found at {cache_path}. Run a codeminer_base "
            f"baseline once to populate it, or pass --source hf."
        )
    return _load_hf(dataset_id, split)


def _load_cache(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if not rows:
        raise ValueError(f"Corpus cache at {path} is empty.")
    return rows


def _load_hf(dataset_id: str, split: str) -> List[Dict[str, Any]]:
    from codeminer.dataset.codeminer_base import CodeMinerBaseDataset

    ds = CodeMinerBaseDataset(dataset=dataset_id, split=split).load()
    return [dict(row) for row in ds]


# ---------------------------------------------------------------------------
# Labeling -> InstanceRow.
# ---------------------------------------------------------------------------


def _label_text(row: Dict[str, Any]) -> str:
    """Concatenate the fields a stack trace can appear in."""
    return "\n".join(
        str(row.get(field) or "") for field in ("problem_statement", "hints_text")
    )


def rows_to_instances(rows: Sequence[Dict[str, Any]]) -> List[InstanceRow]:
    """Project codeminer-base rows into ``InstanceRow`` partition records.

    Unlike ``partition.rows_from_json`` (which normalizes single-language
    strings and drops compound ones), this keeps the corpus's own
    ``language_group`` as the per-language axis -- the codeminer-base grouping
    (``C++/C``, ``TypeScript/JavaScript``, ...) is exactly the split axis we
    want. Stack-trace detection reuses the canonical ``detect_stacktrace``.
    """
    from codeminer.agent.compile import detect_stacktrace

    out: List[InstanceRow] = []
    for row in rows:
        iid = row.get("instance_id")
        repo = row.get("repo")
        if not iid or not repo:
            continue
        out.append(
            InstanceRow(
                instance_id=str(iid),
                repo=str(repo),
                language=row.get("language_group"),
                has_stacktrace=detect_stacktrace(_label_text(row)),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Result assembly + emission.
# ---------------------------------------------------------------------------


def _labels_table(
    instances: Sequence[InstanceRow], fit: Sequence[str], heldout: Sequence[str]
) -> List[Dict[str, Any]]:
    """Per-instance label rows annotated with their split assignment."""
    fit_set = set(fit)
    heldout_set = set(heldout)
    table: List[Dict[str, Any]] = []
    for inst in instances:
        if inst.instance_id in fit_set:
            split = "fit"
        elif inst.instance_id in heldout_set:
            split = "heldout"
        else:
            split = "unassigned"
        table.append(
            {
                "instance_id": inst.instance_id,
                "repo": inst.repo,
                "language": inst.language,
                "has_stacktrace": inst.has_stacktrace,
                "split": split,
            }
        )
    table.sort(key=lambda r: r["instance_id"])
    return table


def assemble_result(
    instances: Sequence[InstanceRow],
    seed: int = DEFAULT_SEED,
    fit_size: int = DEFAULT_FIT_SIZE,
    heldout_size: int = DEFAULT_HELDOUT_SIZE,
) -> Dict[str, Any]:
    """Run the frozen partition and assemble the committed JSON payload."""
    partition = build_partition(
        instances, seed, fit_size=fit_size, heldout_size=heldout_size
    )
    payload = partition.to_dict()

    labels = _labels_table(instances, partition.fit, partition.heldout)
    payload["dataset"] = DATASET_ID
    payload["total"] = len(instances)
    payload["imbalanced"] = bool(partition.warnings)
    payload["labels"] = labels
    payload["scenario"] = _scenario_summary(labels)
    return payload


def _scenario_summary(labels: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-(language, has_stacktrace) fit/heldout/corpus counts."""
    from collections import defaultdict

    cells: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"fit": 0, "heldout": 0, "corpus": 0}
    )
    for row in labels:
        key = f"{row['language']}:" + (
            "stacktrace" if row["has_stacktrace"] else "no_stacktrace"
        )
        cells[key]["corpus"] += 1
        if row["split"] == "fit":
            cells[key]["fit"] += 1
        elif row["split"] == "heldout":
            cells[key]["heldout"] += 1
    return {k: cells[k] for k in sorted(cells)}


def write_outputs(payload: Dict[str, Any], out_path: Path, csv_path: Path) -> None:
    """Write the partition JSON and the flat per-instance label CSV."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["instance_id", "repo", "language", "has_stacktrace", "split"])
        for row in payload["labels"]:
            writer.writerow(
                [
                    row["instance_id"],
                    row["repo"],
                    row["language"],
                    int(row["has_stacktrace"]),
                    row["split"],
                ]
            )


def _print_summary(payload: Dict[str, Any]) -> None:
    print(
        f"partition: seed={payload['seed']} attempts={payload['attempts']} "
        f"fit={len(payload['fit'])} heldout={len(payload['heldout'])} "
        f"total={payload['total']} imbalanced={payload['imbalanced']}"
    )
    for key, c in payload["scenario"].items():
        print(
            f"  {key:34s} fit={c['fit']:2d} heldout={c['heldout']:2d} "
            f"corpus={c['corpus']:2d}"
        )
    for warning in payload.get("warnings", []):
        print(f"  WARN {warning}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Freeze the codeminer-base 30/70 partition + labels.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--source",
        choices=["auto", "cache", "hf"],
        default="auto",
        help="Corpus source: cache (local JSON-lines), hf, or auto.",
    )
    p.add_argument("--cache-path", type=Path, default=DEFAULT_CACHE)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Frozen seed.")
    p.add_argument("--fit-size", type=int, default=DEFAULT_FIT_SIZE)
    p.add_argument("--heldout-size", type=int, default=DEFAULT_HELDOUT_SIZE)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    rows = load_corpus(
        source=args.source, cache_path=args.cache_path, dataset_id=DATASET_ID
    )
    instances = rows_to_instances(rows)
    if not instances:
        print("partition: no usable instance rows; aborting", file=sys.stderr)
        return 2
    payload = assemble_result(
        instances,
        seed=args.seed,
        fit_size=args.fit_size,
        heldout_size=args.heldout_size,
    )
    write_outputs(payload, args.out, args.csv)
    _print_summary(payload)
    print(f"\nWrote {args.out}")
    print(f"Wrote {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
