# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the codeminer-base partition driver (issue #151).

Two layers:

1. Pure-function tests of the driver's labeling / assembly helpers on a small
   synthetic corpus (no I/O, deterministic).
2. Invariant tests on the *committed* frozen artifacts
   (``data/agent_compile/partition.json`` + ``partition_labels.csv``) so the
   frozen 30/70 split stays valid: disjoint splits, ~30/70 sizes, labels
   present for every instance, and every *satisfiable*
   ``(language, has_stacktrace)`` fitting cell >= 2 (or flagged).
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

# Load the driver as a module (it lives under scripts/, not on the package
# path by default). Mirrors test/agent/test_partition.py.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DRIVER_PATH = (
    _PROJECT_ROOT / "scripts" / "agent_compile" / "build_codeminer_base_partition.py"
)
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_SPEC = importlib.util.spec_from_file_location("_cmb_partition_for_tests", _DRIVER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
driver = importlib.util.module_from_spec(_SPEC)
sys.modules["_cmb_partition_for_tests"] = driver
_SPEC.loader.exec_module(driver)


PARTITION_JSON = _PROJECT_ROOT / "data" / "agent_compile" / "partition.json"
PARTITION_CSV = _PROJECT_ROOT / "data" / "agent_compile" / "partition_labels.csv"

MIN_CELL = 2


# ---------------------------------------------------------------------------
# Synthetic corpus helpers
# ---------------------------------------------------------------------------


def _row(iid, repo, lang, problem=""):
    return {
        "instance_id": iid,
        "repo": repo,
        "language_group": lang,
        "problem_statement": problem,
        "hints_text": "",
    }


def _synthetic_corpus():
    """5 languages x 5 repos x 4 instances, plus a couple of stack traces."""
    rows = []
    langs = {
        "C++/C": ["cpp/a", "cpp/b", "cpp/c", "cpp/d", "cpp/e"],
        "Go": ["go/a", "go/b", "go/c", "go/d", "go/e"],
        "Python": ["py/a", "py/b", "py/c", "py/d", "py/e"],
        "Rust": ["rs/a", "rs/b", "rs/c", "rs/d", "rs/e"],
        "TypeScript/JavaScript": ["ts/a", "ts/b", "ts/c", "ts/d", "ts/e"],
    }
    panic = "panic: runtime error\n\ngoroutine 1 [running]:"
    tb = 'Traceback (most recent call last):\n  File "x.py", line 1'
    for lang, repos in langs.items():
        for r_i, repo in enumerate(repos):
            for k in range(4):
                iid = f"{repo.replace('/', '__')}-{k}"
                problem = ""
                # Seed a few stack traces so satisfiable cells exist.
                if lang == "Go" and r_i < 2 and k == 0:
                    problem = panic
                if lang == "Python" and r_i < 2 and k == 0:
                    problem = tb
                rows.append(_row(iid, repo, lang, problem))
    return rows


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


def test_rows_to_instances_labels_language_and_stacktrace():
    rows = [
        _row("a-1", "go/a", "Go", "panic: boom\n\ngoroutine 5 [running]:"),
        _row("b-1", "go/a", "Go", "just a normal description"),
        _row("c-1", "py/a", "Python", "Traceback (most recent call last):\n  File"),
    ]
    insts = driver.rows_to_instances(rows)
    by_id = {i.instance_id: i for i in insts}
    assert by_id["a-1"].language == "Go"
    assert by_id["a-1"].has_stacktrace is True
    assert by_id["b-1"].has_stacktrace is False
    assert by_id["c-1"].language == "Python"
    assert by_id["c-1"].has_stacktrace is True


def test_rows_to_instances_skips_rows_missing_keys():
    rows = [
        _row("a-1", "go/a", "Go"),
        {"instance_id": "no-repo", "language_group": "Go"},
        {"repo": "go/b", "language_group": "Go"},
    ]
    insts = driver.rows_to_instances(rows)
    assert [i.instance_id for i in insts] == ["a-1"]


def test_assemble_result_is_deterministic_and_disjoint():
    insts = driver.rows_to_instances(_synthetic_corpus())
    a = driver.assemble_result(insts, seed=12, fit_size=30, heldout_size=70)
    b = driver.assemble_result(insts, seed=12, fit_size=30, heldout_size=70)
    # Same seed -> identical fit/heldout assignment.
    assert a["fit"] == b["fit"]
    assert a["heldout"] == b["heldout"]
    fit, ho = set(a["fit"]), set(a["heldout"])
    assert fit.isdisjoint(ho)
    assert len(fit | ho) == a["total"] == len(insts)


def test_assemble_result_repos_do_not_span_splits():
    insts = driver.rows_to_instances(_synthetic_corpus())
    res = driver.assemble_result(insts, seed=12, fit_size=30, heldout_size=70)
    by_id_repo = {r["instance_id"]: r["repo"] for r in res["labels"]}
    fit_repos = {by_id_repo[i] for i in res["fit"]}
    ho_repos = {by_id_repo[i] for i in res["heldout"]}
    assert fit_repos.isdisjoint(ho_repos)


def test_assemble_result_satisfiable_cells_covered_or_flagged():
    # The algorithm guarantees that every *satisfiable* cell (corpus n >=
    # MIN_CELL) is either filled to >= MIN_CELL in fit, or the partition is
    # flagged imbalanced after exhausting the reseed budget -- never silently
    # under-filled.
    insts = driver.rows_to_instances(_synthetic_corpus())
    res = driver.assemble_result(insts, seed=12, fit_size=30, heldout_size=70)
    fit_cells = Counter()
    corpus_cells = Counter()
    for row in res["labels"]:
        key = (row["language"], row["has_stacktrace"])
        corpus_cells[key] += 1
        if row["split"] == "fit":
            fit_cells[key] += 1
    for key, corpus_n in corpus_cells.items():
        if corpus_n >= MIN_CELL and fit_cells[key] < MIN_CELL:
            assert res[
                "imbalanced"
            ], f"satisfiable cell {key} under-filled and not flagged"


def test_scenario_summary_counts_consistent():
    insts = driver.rows_to_instances(_synthetic_corpus())
    res = driver.assemble_result(insts, seed=12, fit_size=30, heldout_size=70)
    for c in res["scenario"].values():
        assert c["fit"] + c["heldout"] == c["corpus"]
        assert c["fit"] >= 0 and c["heldout"] >= 0


# ---------------------------------------------------------------------------
# Frozen-artifact invariant tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def frozen_partition():
    if not PARTITION_JSON.exists():
        pytest.skip(f"committed partition not found at {PARTITION_JSON}")
    with open(PARTITION_JSON, "r", encoding="utf-8") as fh:
        return json.load(fh)


def test_frozen_splits_disjoint_and_complete(frozen_partition):
    fit = set(frozen_partition["fit"])
    ho = set(frozen_partition["heldout"])
    assert fit.isdisjoint(ho), "fit and held-out overlap"
    assert len(fit) + len(ho) == len(fit | ho) == frozen_partition["total"]
    # No duplicate instance ids within a split.
    assert len(fit) == len(frozen_partition["fit"])
    assert len(ho) == len(frozen_partition["heldout"])


def test_frozen_sizes_are_roughly_30_70(frozen_partition):
    fit = len(frozen_partition["fit"])
    ho = len(frozen_partition["heldout"])
    total = frozen_partition["total"]
    assert total == 100
    # ~30/70 with whole-repo granularity tolerance (repos are ~4 instances,
    # so a per-language quota of 6 overshoots to ~8). Allow +/- 12.
    assert 18 <= fit <= 42, f"fit={fit} outside ~30 band"
    assert 58 <= ho <= 82, f"heldout={ho} outside ~70 band"


def test_frozen_labels_present_for_every_instance(frozen_partition):
    labels = frozen_partition["labels"]
    assert len(labels) == frozen_partition["total"]
    fit = set(frozen_partition["fit"])
    ho = set(frozen_partition["heldout"])
    seen = set()
    for row in labels:
        assert row["instance_id"]
        assert row["language"], f"missing language for {row['instance_id']}"
        assert isinstance(row["has_stacktrace"], bool)
        assert row["split"] in ("fit", "heldout")
        # split label matches the fit/heldout id lists
        if row["split"] == "fit":
            assert row["instance_id"] in fit
        else:
            assert row["instance_id"] in ho
        seen.add(row["instance_id"])
    assert seen == fit | ho


def test_frozen_repos_do_not_span_splits(frozen_partition):
    by_id_repo = {r["instance_id"]: r["repo"] for r in frozen_partition["labels"]}
    fit_repos = {by_id_repo[i] for i in frozen_partition["fit"]}
    ho_repos = {by_id_repo[i] for i in frozen_partition["heldout"]}
    assert fit_repos.isdisjoint(ho_repos), "a repo leaks across fit/held-out"


def test_frozen_satisfiable_cells_covered_or_flagged(frozen_partition):
    fit_cells = Counter()
    corpus_cells = Counter()
    for row in frozen_partition["labels"]:
        key = (row["language"], row["has_stacktrace"])
        corpus_cells[key] += 1
        if row["split"] == "fit":
            fit_cells[key] += 1

    flagged = frozen_partition["imbalanced"]
    for key, corpus_n in corpus_cells.items():
        if corpus_n < MIN_CELL:
            # Structurally scarce -> must be flagged imbalanced.
            if key[1]:  # only stacktrace=True scarce cells expected
                assert flagged, f"scarce cell {key} not flagged"
            continue
        # Satisfiable cell: must be covered in fit, else partition flagged.
        if fit_cells[key] < MIN_CELL:
            assert flagged, f"satisfiable cell {key} under-filled and not flagged"


def test_frozen_csv_matches_json(frozen_partition):
    if not PARTITION_CSV.exists():
        pytest.skip(f"committed CSV not found at {PARTITION_CSV}")
    with open(PARTITION_CSV, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == frozen_partition["total"]
    json_by_id = {r["instance_id"]: r for r in frozen_partition["labels"]}
    for r in rows:
        jr = json_by_id[r["instance_id"]]
        assert r["repo"] == jr["repo"]
        assert r["language"] == jr["language"]
        assert r["split"] == jr["split"]
        assert (r["has_stacktrace"] == "1") == bool(jr["has_stacktrace"])
