# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``scripts/agent_compile/partition.py``."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# Load the script as a module (it lives under scripts/ which is not on
# the package path by default).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PARTITION_PATH = _PROJECT_ROOT / "scripts" / "agent_compile" / "partition.py"
_SPEC = importlib.util.spec_from_file_location("_partition_for_tests", _PARTITION_PATH)
assert _SPEC is not None and _SPEC.loader is not None
partition_mod = importlib.util.module_from_spec(_SPEC)
sys.modules["_partition_for_tests"] = partition_mod
_SPEC.loader.exec_module(partition_mod)


InstanceRow = partition_mod.InstanceRow
build_partition = partition_mod.build_partition
rows_from_json = partition_mod.rows_from_json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rows(
    spec: Dict[str, Dict[str, int]],
) -> List[InstanceRow]:
    """Build a synthetic row list.

    ``spec`` maps ``repo -> {"language": str, "stacktrace": int,
    "no_stacktrace": int}``. Instance IDs are generated deterministically.
    """
    rows: List[InstanceRow] = []
    for repo, conf in spec.items():
        lang = conf["language"]
        for i in range(conf.get("stacktrace", 0)):
            rows.append(
                InstanceRow(
                    instance_id=f"{repo}-trace-{i}",
                    repo=repo,
                    language=lang,
                    has_stacktrace=True,
                )
            )
        for i in range(conf.get("no_stacktrace", 0)):
            rows.append(
                InstanceRow(
                    instance_id=f"{repo}-clean-{i}",
                    repo=repo,
                    language=lang,
                    has_stacktrace=False,
                )
            )
    return rows


def _balanced_corpus() -> List[InstanceRow]:
    """Roughly mirrors the SWE-bench Multilingual shape: 2 repos per lang,
    a mix of stacktrace / no-stacktrace instances."""
    return _make_rows(
        {
            "owner/py1": {"language": "python", "stacktrace": 5, "no_stacktrace": 8},
            "owner/py2": {"language": "python", "stacktrace": 6, "no_stacktrace": 7},
            "owner/go1": {"language": "go", "stacktrace": 4, "no_stacktrace": 6},
            "owner/go2": {"language": "go", "stacktrace": 3, "no_stacktrace": 5},
            "owner/rs1": {"language": "rust", "stacktrace": 3, "no_stacktrace": 4},
            "owner/rs2": {"language": "rust", "stacktrace": 2, "no_stacktrace": 4},
            "owner/cpp1": {"language": "cpp", "stacktrace": 4, "no_stacktrace": 4},
            "owner/cpp2": {"language": "cpp", "stacktrace": 3, "no_stacktrace": 4},
            "owner/ts1": {
                "language": "typescript",
                "stacktrace": 4,
                "no_stacktrace": 7,
            },
            "owner/ts2": {
                "language": "typescript",
                "stacktrace": 3,
                "no_stacktrace": 6,
            },
        }
    )


# ---------------------------------------------------------------------------
# rows_from_json
# ---------------------------------------------------------------------------


class TestRowsFromJson:
    def test_basic_projection(self):
        payload = [
            {
                "instance_id": "foo-1",
                "repo": "owner/foo",
                "language": "Python",
                "problem_statement": (
                    "Test fails:\nTraceback (most recent call last):\n"
                    "  File 'x.py', line 1\n"
                ),
            },
            {
                "instance_id": "bar-1",
                "repo": "owner/bar",
                "language": "Rust",
                "problem_statement": "Need a refactor.",
            },
        ]
        rows = rows_from_json(payload)
        assert len(rows) == 2
        assert rows[0].language == "python" and rows[0].has_stacktrace is True
        assert rows[1].language == "rust" and rows[1].has_stacktrace is False

    def test_skips_rows_missing_id_or_repo(self, capsys):
        payload: List[Dict[str, Any]] = [
            {"instance_id": "ok", "repo": "owner/ok", "language": "Go"},
            {"repo": "owner/no_id"},
            {"instance_id": "no_repo"},
        ]
        rows = rows_from_json(payload)
        assert [r.instance_id for r in rows] == ["ok"]
        err = capsys.readouterr().err
        assert "skipped 2 rows" in err

    def test_alt_language_keys(self):
        rows = rows_from_json(
            [
                {"instance_id": "a", "repo": "x", "Language": "TypeScript"},
                {"instance_id": "b", "repo": "y", "language": "python3"},
            ]
        )
        assert rows[0].language == "typescript"
        assert rows[1].language == "python"


# ---------------------------------------------------------------------------
# build_partition — determinism + shape
# ---------------------------------------------------------------------------


class TestBuildPartitionDeterminism:
    def test_same_seed_same_partition(self):
        rows = _balanced_corpus()
        a = build_partition(rows, seed=42, fit_size=30, heldout_size=70)
        b = build_partition(rows, seed=42, fit_size=30, heldout_size=70)
        assert a.fit == b.fit
        assert a.heldout == b.heldout
        assert a.cells == b.cells
        assert a.attempts == b.attempts

    def test_different_seed_different_partition(self):
        rows = _balanced_corpus()
        a = build_partition(rows, seed=1, fit_size=30, heldout_size=70)
        b = build_partition(rows, seed=999, fit_size=30, heldout_size=70)
        # With 10 repos × 5 languages, two random shuffles disagreeing on
        # at least one assignment is overwhelmingly likely. A theoretical
        # collision is harmless; if this flakes in CI, weaken to "either
        # fit or heldout differs by at least one ID".
        assert set(a.fit) != set(b.fit) or set(a.heldout) != set(b.heldout)


class TestBuildPartitionRepoLevel:
    def test_repos_are_not_split(self):
        """A given repo's instances must all land on the same side."""
        rows = _balanced_corpus()
        partition = build_partition(rows, seed=7, fit_size=30, heldout_size=70)
        fit_set = set(partition.fit)
        heldout_set = set(partition.heldout)

        repo_to_side: Dict[str, str] = {}
        for r in rows:
            if r.instance_id in fit_set:
                side = "fit"
            elif r.instance_id in heldout_set:
                side = "heldout"
            else:
                continue  # dropped by heldout_size cap is acceptable
            if r.repo in repo_to_side:
                assert (
                    repo_to_side[r.repo] == side
                ), f"Repo {r.repo} split across fit / heldout"
            else:
                repo_to_side[r.repo] = side


class TestBuildPartitionCellCoverage:
    def test_cells_meet_minimum_on_balanced_corpus(self):
        rows = _balanced_corpus()
        partition = build_partition(
            rows,
            seed=2026,
            fit_size=30,
            heldout_size=70,
            min_cell_instances=2,
        )
        # On a balanced corpus we expect to succeed without exhausting reseeds.
        assert partition.attempts <= 3
        if not partition.warnings:
            for key, count in partition.cells.items():
                assert count >= 2, f"Cell {key} = {count} < 2"

    def test_warns_on_corpus_scarce_cell(self):
        """If a language has zero stacktrace instances in the whole corpus,
        no reseeding can fix it. We still produce a partition + a clear
        warning so the ADR can flag the imbalance.
        """
        rows = _make_rows(
            {
                "x/py1": {"language": "python", "stacktrace": 0, "no_stacktrace": 20},
                "x/go1": {"language": "go", "stacktrace": 5, "no_stacktrace": 5},
            }
        )
        partition = build_partition(
            rows,
            seed=1,
            fit_size=10,
            heldout_size=20,
            max_reseed=3,
            min_cell_instances=2,
        )
        # Reseeding cannot manifest python:stacktrace instances that
        # don't exist — algorithm stops on first attempt and warns.
        assert any("Corpus too thin" in w for w in partition.warnings)

    def test_reseed_exhausts_then_warns(self):
        """Force the reseed path with a corpus where only some repos
        carry stacktraces. With one all-trace repo, one all-clean repo
        per language, and the per-language quota = 1 repo, the shuffle
        decides which cell is filled. A bad-luck seed misses
        ``rust:stacktrace`` 3 attempts in a row.
        """
        rows = _make_rows(
            {
                "x/py-trace": {"language": "python", "stacktrace": 4},
                "x/py-clean": {"language": "python", "no_stacktrace": 4},
                "x/rs-trace": {"language": "rust", "stacktrace": 4},
                "x/rs-clean": {"language": "rust", "no_stacktrace": 4},
            }
        )
        # Try a range of seeds; expect at least one to fail coverage
        # across 3 attempts. The algorithm is deterministic per seed,
        # so this proves the warning path works.
        found_failure = False
        for seed in range(50):
            partition = build_partition(
                rows,
                seed=seed,
                fit_size=8,
                heldout_size=16,
                max_reseed=3,
                min_cell_instances=2,
            )
            if any("Cell coverage check failed" in w for w in partition.warnings):
                found_failure = True
                assert partition.attempts == 3
                break
        assert found_failure, "No seed in [0..50) triggered the warning path"


class TestBuildPartitionHeldoutCap:
    def test_heldout_truncated_to_cap(self):
        rows = _balanced_corpus()
        partition = build_partition(rows, seed=11, fit_size=20, heldout_size=15)
        assert len(partition.heldout) <= 15


# ---------------------------------------------------------------------------
# End-to-end: CLI roundtrip
# ---------------------------------------------------------------------------


class TestPartitionCli:
    def test_main_writes_partition_json(self, tmp_path: Path):
        payload = [
            {
                "instance_id": f"{repo}-{i}",
                "repo": repo,
                "language": lang,
                "problem_statement": (
                    "Traceback (most recent call last):\n  File 'x.py', line 1\n"
                    if i % 2 == 0
                    else "Need a refactor."
                ),
            }
            for repo, lang in [
                ("owner/py1", "python"),
                ("owner/py2", "python"),
                ("owner/go1", "go"),
                ("owner/go2", "go"),
                ("owner/rs1", "rust"),
                ("owner/rs2", "rust"),
            ]
            for i in range(8)
        ]
        input_path = tmp_path / "instances.json"
        output_path = tmp_path / "partition.json"
        input_path.write_text(json.dumps(payload))

        rc = partition_mod.main(
            [
                "--instances",
                str(input_path),
                "--seed",
                "20260514",
                "--fit-size",
                "12",
                "--heldout-size",
                "30",
                "--output",
                str(output_path),
            ]
        )
        assert rc == 0

        result = json.loads(output_path.read_text())
        assert result["seed"] == 20260514
        assert result["fit_size"] == 12
        assert set(result["fit"]).isdisjoint(set(result["heldout"]))
        assert sum(result["cells"].values()) == len(result["fit"])

    def test_main_rejects_empty_input(self, tmp_path: Path):
        input_path = tmp_path / "empty.json"
        output_path = tmp_path / "partition.json"
        input_path.write_text(json.dumps([]))
        rc = partition_mod.main(
            [
                "--instances",
                str(input_path),
                "--seed",
                "1",
                "--output",
                str(output_path),
            ]
        )
        assert rc == 2
        assert not output_path.exists()
