# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codenib.compiler.manifest import RepoManifest
from codenib.eval.benchmarks.policy_compat import (
    PolicyBenchmarkCase,
    PolicyCaseSet,
    RevisionSource,
    assess_policy_coverage,
    audit_policy_provenance,
    build_policy_provenance,
    inspect_case_eligibility,
    inspect_policy_run_preflight,
    load_policy_results,
    policy_result_path,
    source_files,
    write_json_atomic,
)

COMMIT = "a" * 40


def _case_record(root: Path, *, instance_id: str = "demo__repo-1") -> dict:
    return {
        "instance_id": instance_id,
        "repo": "demo/repo",
        "base_commit": COMMIT,
        "problem_statement": "Find the parser entry point",
        "repo_path": str(root / "repo"),
        "manifest_path": str(root / "manifest.json"),
        "orcaloca_native_repo_path": str(root / "native"),
        "ground_truth": {"gold_files": ["src/parser.py"], "gold_functions": []},
        "custom_label": "preserved",
    }


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_case_set_loads_metadata_selects_stably_and_hashes_semantics(
    tmp_path: Path,
) -> None:
    first = _case_record(tmp_path, instance_id="demo__repo-1")
    second = _case_record(tmp_path, instance_id="demo__repo-2")
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            {"dataset": "fixture", "metric": "locations", "cases": [first, second]}
        ),
        encoding="utf-8",
    )

    case_set = PolicyCaseSet.load(path)
    selected = case_set.select(["demo__repo-2", "demo__repo-1"])

    assert case_set.metadata == {"dataset": "fixture", "metric": "locations"}
    assert [case.instance_id for case in selected] == ["demo__repo-1", "demo__repo-2"]
    assert selected[0].checkout_for(agent="orcaloca", provider="native") == (
        tmp_path / "native"
    )
    assert selected[0].extra == {"custom_label": "preserved"}
    assert len(case_set.selection_sha256(selected)) == 64
    with pytest.raises(ValueError, match="unknown instance ids"):
        case_set.select(["missing"])


def test_case_set_rejects_duplicates_and_non_commit_refs(tmp_path: Path) -> None:
    record = _case_record(tmp_path)
    duplicate_path = tmp_path / "duplicate.json"
    duplicate_path.write_text(json.dumps([record, record]), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate case"):
        PolicyCaseSet.load(duplicate_path)

    record["base_commit"] = "main"
    with pytest.raises(ValueError, match="full Git object id"):
        PolicyBenchmarkCase.from_mapping(record)


def test_eligibility_checks_checkout_manifest_commit_and_capabilities(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    commit = _init_repo(repo)
    record = _case_record(tmp_path)
    record["base_commit"] = commit
    record["orcaloca_native_repo_path"] = str(repo)
    manifest = RepoManifest(
        repo_path=str(repo),
        commit=commit,
        last_indexed_commit=commit,
        capabilities={"symbol_navigation": True},
    )
    manifest.save(record["manifest_path"])
    case = PolicyBenchmarkCase.from_mapping(record)

    eligible = inspect_case_eligibility(
        case,
        agent="orcaloca",
        provider="codenib",
        required_capabilities=("symbol_navigation",),
    )
    assert eligible.eligible
    assert eligible.repo_commit == commit
    assert eligible.manifest_commit == commit

    manifest.capabilities.clear()
    manifest.commit = "b" * 40
    manifest.save(record["manifest_path"])
    rejected = inspect_case_eligibility(
        case,
        agent="orcaloca",
        provider="codenib",
        required_capabilities=("symbol_navigation",),
    )
    assert not rejected.eligible
    assert any("manifest commit mismatch" in reason for reason in rejected.reasons)
    assert any("lacks capability" in reason for reason in rejected.reasons)


def test_coverage_keeps_missing_and_failed_cells_in_the_denominator(
    tmp_path: Path,
) -> None:
    cases = tuple(
        PolicyBenchmarkCase.from_mapping(
            _case_record(tmp_path, instance_id=f"demo__repo-{index}")
        )
        for index in (1, 2)
    )
    results = [
        {"instance_id": "demo__repo-1", "provider": "native"},
        {"instance_id": "demo__repo-1", "provider": "codenib"},
        {"instance_id": "demo__repo-2", "provider": "native", "error": "timeout"},
    ]

    coverage = assess_policy_coverage(cases, results)
    record = coverage.to_record()

    assert record["requested_instance_count"] == 2
    assert record["expected_cell_count"] == 4
    assert record["observed_cell_count"] == 3
    assert record["missing_cells"] == [
        {"instance_id": "demo__repo-2", "provider": "codenib"}
    ]
    assert record["failed_cells"] == [
        {"instance_id": "demo__repo-2", "provider": "native"}
    ]
    assert record["paired_present_count"] == 1
    assert record["paired_successful_count"] == 1


def test_provenance_records_revisions_without_checkout_paths(tmp_path: Path) -> None:
    code_root = tmp_path / "code"
    upstream_root = tmp_path / "upstream"
    code_commit = _init_repo(code_root)
    upstream_commit = _init_repo(upstream_root)
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([_case_record(tmp_path)]), encoding="utf-8")
    case_set = PolicyCaseSet.load(cases_path)

    provenance = build_policy_provenance(
        agent="locagent",
        provider="codenib",
        model="gpt-test",
        model_revision="model-rev",
        code_root=code_root,
        case_set=case_set,
        selected_cases=case_set.cases,
        revision_sources=(
            RevisionSource(
                name="locagent",
                repository="https://example.test/locagent",
                expected_commit=upstream_commit,
                checkout=upstream_root,
            ),
        ),
        run_options={"max_iterations": 10},
    )

    assert provenance["codenib"] == {"commit": code_commit, "dirty": False}
    assert provenance["upstream"][0]["matches_expected"] is True
    assert provenance["upstream"][0]["contains_expected"] is True
    assert provenance["cases"]["instance_ids"] == ["demo__repo-1"]
    assert provenance["model"]["revision"] == "model-rev"
    serialized = json.dumps(provenance)
    assert str(tmp_path) not in serialized


def test_revision_source_accepts_a_pinned_ancestor(tmp_path: Path) -> None:
    checkout = tmp_path / "upstream"
    expected_commit = _init_repo(checkout)
    (checkout / "adapter.py").write_text("ENABLED = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "adapter.py"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-qm", "adapter"], cwd=checkout, check=True)

    record = RevisionSource(
        name="upstream",
        repository="https://example.test/upstream",
        expected_commit=expected_commit,
        checkout=checkout,
    ).to_record()

    assert record["matches_expected"] is False
    assert record["contains_expected"] is True
    assert record["dirty"] is False

    with pytest.raises(ValueError, match="full Git object id"):
        RevisionSource(
            name="upstream",
            repository="https://example.test/upstream",
            expected_commit="main",
        )


def test_run_preflight_audits_every_provider_before_execution(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    commit = _init_repo(repo)
    record = _case_record(tmp_path)
    record["base_commit"] = commit
    record["orcaloca_native_repo_path"] = str(repo)
    RepoManifest(
        repo_path=str(repo),
        commit=commit,
        last_indexed_commit=commit,
        capabilities={"symbol_navigation": False},
    ).save(record["manifest_path"])
    case = PolicyBenchmarkCase.from_mapping(record)

    preflight = inspect_policy_run_preflight(
        (case,),
        agent="orcaloca",
        providers=("native", "codenib"),
        required_capabilities={"codenib": ("symbol_navigation",)},
    )

    assert not preflight.eligible
    assert len(preflight.cells) == 2
    assert preflight.cells[0].eligible
    assert not preflight.cells[1].eligible
    with pytest.raises(RuntimeError, match="manifest lacks capability"):
        preflight.require_eligible()


def test_result_path_and_atomic_json_write(tmp_path: Path) -> None:
    path = policy_result_path(
        tmp_path,
        instance_id="demo__repo-1",
        agent="OrcaLoca",
        provider="NATIVE",
    )
    assert path.name == "demo__repo-1.orcaloca.native.json"
    write_json_atomic(path, {"ok": True})
    assert json.loads(path.read_text()) == {"ok": True}
    assert not path.with_suffix(".json.tmp").exists()

    with pytest.raises(ValueError, match="invalid instance_id"):
        policy_result_path(
            tmp_path,
            instance_id="../escape",
            agent="orcaloca",
            provider="native",
        )


def test_result_loading_validates_identity_and_source_listing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/code.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "README.md").write_text("docs\n", encoding="utf-8")
    case = PolicyBenchmarkCase.from_mapping(_case_record(tmp_path))
    native_path = policy_result_path(
        tmp_path / "results",
        instance_id=case.instance_id,
        agent="locagent",
        provider="native",
    )
    write_json_atomic(
        native_path,
        {
            "instance_id": case.instance_id,
            "agent": "locagent",
            "provider": "native",
        },
    )

    loaded = load_policy_results(tmp_path / "results", agent="locagent", cases=(case,))
    assert len(loaded) == 1
    assert source_files(repo) == ("src/code.py",)

    write_json_atomic(
        native_path,
        {
            "instance_id": "wrong",
            "agent": "locagent",
            "provider": "native",
        },
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        load_policy_results(tmp_path / "results", agent="locagent", cases=(case,))


def test_provenance_audit_distinguishes_legacy_and_invalid_cells() -> None:
    results = [
        {
            "instance_id": "demo__repo-1",
            "agent": "orcaloca",
            "provider": "native",
        },
        {
            "instance_id": "demo__repo-1",
            "agent": "orcaloca",
            "provider": "codenib",
            "provenance": {
                "schema_version": 1,
                "policy": {"agent": "orcaloca", "provider": "codenib"},
            },
        },
        {
            "instance_id": "demo__repo-2",
            "agent": "orcaloca",
            "provider": "native",
            "provenance": {
                "schema_version": 99,
                "policy": {"agent": "orcaloca", "provider": "native"},
            },
        },
    ]

    record = audit_policy_provenance(results).to_record()

    assert record["observed_cell_count"] == 3
    assert record["valid_cell_count"] == 1
    assert record["unrecorded_cells"] == [
        {"instance_id": "demo__repo-1", "provider": "native"}
    ]
    assert record["invalid_cells"][0]["reason"] == (
        "unsupported provenance schema_version"
    )
