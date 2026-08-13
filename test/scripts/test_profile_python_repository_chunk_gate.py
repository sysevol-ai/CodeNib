# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path
from typing import Any

import pytest

from codenib.code_chunking.base import CodeChunk
from scripts.profiling import profile_python_repository_chunk_gate as profiler


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _make_subject(root: Path, *, repository: str, source: str) -> str:
    root.mkdir()
    (root / "package.py").write_text(source, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "CodeNib Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", "package.py")
    _git(root, "commit", "-qm", "fixture")
    _git(root, "remote", "add", "origin", repository)
    revision = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "--detach", "-q", revision)
    return revision


def _write_manifest(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    roots = {
        "codenib": tmp_path / "codenib-subject",
        "httpie": tmp_path / "httpie-subject",
    }
    repositories = {
        "codenib": "https://github.com/sysevol-ai/CodeNib.git",
        "httpie": "https://github.com/httpie/cli.git",
    }
    sources = {
        "codenib": "class Alpha:\n    def method(self):\n        return 1\n",
        "httpie": "async def request():\n    return 'ok'\n",
    }
    subjects = []
    for identifier, root in roots.items():
        revision = _make_subject(
            root, repository=repositories[identifier], source=sources[identifier]
        )
        selected_sources = {}
        for configuration in profiler.CONFIGURATIONS:
            receipt = profiler._selected_source_receipt(root, configuration)
            selected_sources[configuration["id"]] = {
                key: receipt[key]
                for key in (
                    "selected_source_sha256",
                    "contract_sha256",
                    "file_count",
                    "total_bytes",
                    "first_path",
                    "last_path",
                )
            }
        subjects.append(
            {
                "id": identifier,
                "repository": repositories[identifier],
                "revision": revision,
                "selected_sources": selected_sources,
            }
        )
    manifest = tmp_path / "subjects.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository_filter_policy": 3,
                "configurations": [row["id"] for row in profiler.CONFIGURATIONS],
                "subjects": subjects,
            }
        ),
        encoding="utf-8",
    )
    return manifest, roots


def _contract() -> dict[str, Any]:
    return {
        **profiler._CANDIDATE_CONTRACT_SCHEMA,
        "max_file_count": 10000,
        "max_source_bytes": 1_000_000_000,
        "max_row_count": 10_000_000,
        "max_arena_bytes": 1_000_000_000,
    }


def _candidate_receipt() -> dict[str, Any]:
    build_dir = profiler._DEFAULT_BUILD_DIR.resolve()
    adapter = (
        profiler._PROJECT_ROOT / "codenib/code_chunking/python_repository_batch_poc.py"
    ).resolve()
    native = build_dir / "codenib_core.so"
    return {
        "available": True,
        "adapter": {
            "path": str(adapter),
            "size_bytes": 1,
            "sha256": "sha256:" + "1" * 64,
        },
        "native_binary": {
            "path": str(native),
            "size_bytes": 1,
            "sha256": "sha256:" + "2" * 64,
        },
        "native_module_origin": str(native),
        "contract": _contract(),
        "contract_sha256": profiler._json_digest(_contract()),
        "build_dir": str(build_dir),
    }


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("Name:\tpython\nVmHWM:\t123 kB\n", 123 * 1024),
        ("VmHWM:\t0 kB\n", None),
        ("VmHWM:\t-1 kB\n", None),
        ("VmHWM:\tnot-an-int kB\n", None),
        ("VmHWM:\t123 bytes\n", None),
        ("VmHWM:\t123 kB extra\n", None),
        ("VmRSS:\t123 kB\n", None),
    ],
)
def test_linux_peak_rss_reads_current_process_vmhwm(
    tmp_path: Path, contents: str, expected: int | None
) -> None:
    status = tmp_path / "status"
    status.write_text(contents, encoding="ascii")

    assert profiler._linux_peak_rss_bytes(status) == expected


def test_linux_peak_rss_does_not_fall_back_to_inherited_getrusage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(profiler.sys, "platform", "linux")
    monkeypatch.setattr(profiler, "_linux_peak_rss_bytes", lambda: None)

    assert profiler._peak_rss_bytes() is None
    assert profiler._peak_rss_source() == "proc-self-status-vmhwm-kib-v1"


def test_canonical_protocol_requires_process_scoped_linux_peak_rss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = {
        "manifest": {"repository_filter_policy": 3},
        "subjects": [{"id": "codenib"}, {"id": "httpie"}],
    }
    monkeypatch.setattr(
        profiler,
        "_peak_rss_source",
        lambda: "getrusage-self-ru-maxrss-kib-v1",
    )

    protocol = profiler._canonical_protocol(
        prepared,
        iterations=profiler.DEFAULT_ITERATIONS,
        warmups=profiler.DEFAULT_WARMUPS,
        threshold=profiler.DEFAULT_PROMOTION_THRESHOLD,
        rss_ratio_limit=profiler.DEFAULT_RSS_RATIO_LIMIT,
        worker_counts=profiler.DEFAULT_WORKER_COUNTS,
    )

    assert protocol["passed"] is False
    assert protocol["expected"]["peak_rss_source"] == ("proc-self-status-vmhwm-kib-v1")
    assert protocol["observed"]["peak_rss_source"] == (
        "getrusage-self-ru-maxrss-kib-v1"
    )


def _fake_sample_runner(
    *,
    candidate_ratio: float = 0.75,
    candidate_rss_ratio: float = 1.2,
    mismatch: bool = False,
):
    process_id = 1000

    def run(config: dict[str, Any]) -> dict[str, Any]:
        nonlocal process_id
        process_id += 1
        arm = config["arm"]
        content = "different" if mismatch and arm == "candidate" else "return 1"
        chunk = CodeChunk(
            content=content,
            start_line=0,
            end_line=0,
            chunk_type="function",
            name="run",
            file="package.py",
            node_id="package.py:run",
        )
        chunk_identity, chunk_records = profiler._chunk_identity([chunk])
        raw_nodes = (
            ["package.py:run", "package.py"]
            if arm == "legacy"
            else ["package.py", "package.py:run", "package.py"]
        )
        node_identity, raw_nodes, canonical_nodes = profiler._node_identity(raw_nodes)
        source = config["selected_source_receipt"]
        legacy_wall = 10.0
        legacy_rss = 1000
        diagnostics = {
            "backend": "python",
            "fallback_count": 0,
            "batch_call_count": 0,
            "worker_count": None,
            "contract": None,
            "stages": {},
        }
        if arm == "candidate":
            diagnostics = {
                "backend": "native-repository-batch-poc",
                "fallback_count": 0,
                "batch_call_count": 1,
                "worker_count": config["worker_count"],
                "contract": config["candidate_contract"],
                "counters": {
                    "file_count": source["file_count"],
                    "source_bytes": source["total_bytes"],
                    "span_count": 1,
                    "chunk_count": 1,
                    "node_count": 2,
                },
                "stages": {field: 0.01 for field in profiler._CANDIDATE_STAGE_FIELDS},
            }
        return {
            "arm": arm,
            "subject_id": config["subject_id"],
            "configuration_id": config["configuration_id"],
            "worker_count": config["worker_count"],
            "process_id": process_id,
            "wall_seconds": (
                legacy_wall if arm == "legacy" else legacy_wall * candidate_ratio
            ),
            "cpu_seconds": 1.0,
            "peak_rss_start_bytes": 100,
            "peak_rss_bytes": (
                legacy_rss if arm == "legacy" else int(legacy_rss * candidate_rss_ratio)
            ),
            "file_count": source["file_count"],
            "source_bytes": source["total_bytes"],
            "chunk_count": 1,
            "node_count": 2,
            "chunk_identity": chunk_identity,
            "node_identity": node_identity,
            "diagnostics": diagnostics,
            "source_receipt_before": source,
            "source_receipt_after": source,
            "gc_policy": "collect-then-disabled-during-stopwatch-v1",
            "_chunk_records": chunk_records,
            "_raw_nodes": raw_nodes,
            "_canonical_nodes": canonical_nodes,
        }

    return run


def _benchmark_receipt() -> dict[str, Any]:
    return {"git": {"dirty": False}, "harness": "fixed", "manifest": "fixed"}


def _candidate_sample() -> tuple[dict[str, Any], dict[str, Any]]:
    source = {
        "schema_version": 1,
        "configuration_id": profiler.CONFIGURATIONS[0]["id"],
        "contract_sha256": "sha256:" + "3" * 64,
        "selected_source_sha256": "sha256:" + "4" * 64,
        "materialization_identity_sha256": "sha256:" + "5" * 64,
        "file_count": 1,
        "total_bytes": 10,
        "first_path": "package.py",
        "last_path": "package.py",
    }
    config = {
        "arm": "candidate",
        "subject_id": "codenib",
        "configuration_id": profiler.CONFIGURATIONS[0]["id"],
        "worker_count": 1,
        "selected_source_receipt": source,
        "candidate_contract": _contract(),
    }
    return _fake_sample_runner()(config), config


def _validate_candidate_sample(sample: dict[str, Any], config: dict[str, Any]) -> None:
    profiler._validate_sample(
        sample,
        arm="candidate",
        subject_id="codenib",
        configuration_id=profiler.CONFIGURATIONS[0]["id"],
        worker_count=1,
        expected_source=config["selected_source_receipt"],
        expected_candidate_contract=config["candidate_contract"],
    )


def test_summary_uses_median_and_nearest_rank_p95() -> None:
    summary = profiler.summarize_samples(range(1, 21))

    assert summary["p50"] == 10.5
    assert summary["p95"] == 19.0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda contract: contract.clear(),
        lambda contract: contract.__setitem__("max_file_count", 0),
        lambda contract: contract.__setitem__("max_source_bytes", True),
        lambda contract: contract.__setitem__("ordering", "unordered"),
        lambda contract: contract.__setitem__("supported_worker_counts", [1, 4]),
    ],
)
def test_candidate_contract_schema_and_caps_fail_closed(mutate) -> None:
    contract = _contract()
    mutate(contract)

    with pytest.raises((RuntimeError, ValueError)):
        profiler._validate_candidate_contract(contract)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("backend",), "python"),
        (("fallback_count",), 1),
        (("batch_call_count",), 0),
        (("contract",), {}),
        (("counters", "file_count"), 0),
        (("counters", "source_bytes"), 9),
        (("stages", "binding_wall_seconds"), float("nan")),
        (("stages", "native_parse_work_seconds"), float("inf")),
    ],
)
def test_candidate_diagnostics_mismatch_fails_closed(
    path: tuple[str, ...], value: Any
) -> None:
    sample, config = _candidate_sample()
    target = sample["diagnostics"]
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value

    with pytest.raises((RuntimeError, ValueError)):
        _validate_candidate_sample(sample, config)


def test_candidate_missing_stage_fails_closed() -> None:
    sample, config = _candidate_sample()
    del sample["diagnostics"]["stages"]["buffer_decode_wall_seconds"]

    with pytest.raises(RuntimeError, match="stage telemetry schema"):
        _validate_candidate_sample(sample, config)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("wall_seconds", float("nan")),
        ("wall_seconds", float("inf")),
        ("wall_seconds", True),
        ("cpu_seconds", None),
        ("peak_rss_bytes", float("nan")),
        ("peak_rss_bytes", True),
        ("peak_rss_bytes", None),
    ],
)
def test_sample_metrics_must_be_finite_and_well_typed(field: str, value: Any) -> None:
    sample, config = _candidate_sample()
    sample[field] = value

    with pytest.raises((RuntimeError, ValueError)):
        _validate_candidate_sample(sample, config)


@pytest.mark.parametrize(
    ("candidate_p50", "candidate_p95", "candidate_rss", "passed"),
    [
        (8.0, 16.0, 125.0, True),
        (8.0000001, 16.0, 125.0, False),
        (7.9, 16.0000001, 125.0, False),
        (7.9, 15.9, 125.0000001, False),
        (11.0, 21.0, 100.0, False),
    ],
)
def test_cell_decision_has_exact_time_and_rss_boundaries(
    candidate_p50: float,
    candidate_p95: float,
    candidate_rss: float,
    passed: bool,
) -> None:
    decision = profiler.cell_decision(
        legacy_wall={"p50": 10.0, "p95": 20.0},
        candidate_wall={"p50": candidate_p50, "p95": candidate_p95},
        legacy_rss={"p95": 100.0},
        candidate_rss={"p95": candidate_rss},
        threshold=0.2,
        rss_ratio_limit=1.25,
        chunks_equal=True,
        nodes_equal=True,
        candidate_contract=True,
        identity_unchanged=True,
        process_ids_unique=True,
        canonical_protocol=True,
    )

    assert decision["passed"] is passed


def test_arm_order_is_one_pair_per_round_and_alternates() -> None:
    assert [profiler.paired_arm_order(index) for index in range(4)] == [
        ("legacy", "candidate"),
        ("candidate", "legacy"),
        ("legacy", "candidate"),
        ("candidate", "legacy"),
    ]


def test_complete_four_cell_matrix_selects_smallest_global_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, roots = _write_manifest(tmp_path)
    monkeypatch.setattr(
        profiler,
        "_peak_rss_source",
        lambda: profiler.CANONICAL_PEAK_RSS_SOURCE,
    )

    report = profiler.profile_python_repository_chunk_gate(
        subject_roots=roots,
        manifest_path=manifest,
        _sample_runner=_fake_sample_runner(),
        _candidate_observer=_candidate_receipt,
        _benchmark_observer=_benchmark_receipt,
    )

    assert report["passed"] is True
    assert report["promotion_eligible"] is True
    assert report["decision"]["qualifying_worker_counts"] == [1, 2, 4]
    assert report["decision"]["selected_worker_count"] == 1
    for worker in report["workers"].values():
        assert len(worker["cells"]) == 4
        assert worker["passed_all_four_cells"] is True
        for cell in worker["cells"].values():
            assert len(cell["raw"]["warmups"]) == 4
            assert len(cell["raw"]["measured"]) == 20
            assert cell["parity"]["ordered_seven_field_chunks_every_pair"]
            assert cell["parity"]["canonical_sorted_unique_nodes_every_pair"]
            assert "stage_telemetry" in cell["candidate"]


def test_one_failed_cell_rejects_a_global_worker(tmp_path: Path) -> None:
    manifest, roots = _write_manifest(tmp_path)

    report = profiler.profile_python_repository_chunk_gate(
        subject_roots=roots,
        manifest_path=manifest,
        iterations=1,
        warmups=0,
        worker_counts=(1,),
        _sample_runner=_fake_sample_runner(mismatch=True),
        _candidate_observer=_candidate_receipt,
        _benchmark_observer=_benchmark_receipt,
    )

    assert report["passed"] is False
    assert report["promotion_eligible"] is False
    assert report["decision"]["selected_worker_count"] is None
    cells = report["workers"]["1"]["cells"]
    assert all(
        not cell["parity"]["ordered_seven_field_chunks_every_pair"]
        for cell in cells.values()
    )


def test_noncanonical_green_matrix_is_never_promotion_eligible(
    tmp_path: Path,
) -> None:
    manifest, roots = _write_manifest(tmp_path)

    report = profiler.profile_python_repository_chunk_gate(
        subject_roots=roots,
        manifest_path=manifest,
        iterations=1,
        warmups=0,
        worker_counts=(1,),
        _sample_runner=_fake_sample_runner(),
        _candidate_observer=_candidate_receipt,
        _benchmark_observer=_benchmark_receipt,
    )

    assert report["canonical_protocol"]["passed"] is False
    assert report["promotion_eligible"] is False
    assert report["passed"] is False
    assert report["decision"]["selected_worker_count"] is None
    for cell in report["workers"]["1"]["cells"].values():
        assert cell["decision"]["gates"]["p50"] is True
        assert cell["decision"]["gates"]["p95"] is True
        assert cell["decision"]["gates"]["canonical_protocol"] is False


def test_canonical_slow_matrix_is_not_promotion_eligible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, roots = _write_manifest(tmp_path)
    monkeypatch.setattr(
        profiler,
        "_peak_rss_source",
        lambda: profiler.CANONICAL_PEAK_RSS_SOURCE,
    )

    report = profiler.profile_python_repository_chunk_gate(
        subject_roots=roots,
        manifest_path=manifest,
        _sample_runner=_fake_sample_runner(candidate_ratio=0.9),
        _candidate_observer=_candidate_receipt,
        _benchmark_observer=_benchmark_receipt,
    )

    assert report["canonical_protocol"]["passed"] is True
    assert report["passed"] is False
    assert report["promotion_eligible"] is False
    assert report["decision"]["selected_worker_count"] is None
    for worker in report["workers"].values():
        for cell in worker["cells"].values():
            assert cell["decision"]["gates"]["p50"] is False
            assert cell["decision"]["gates"]["p95"] is False


def test_pid_reuse_across_workers_rejects_every_worker(tmp_path: Path) -> None:
    manifest, roots = _write_manifest(tmp_path)
    base_runner = _fake_sample_runner()
    calls = 0

    def reusing_runner(config: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        sample = base_runner(config)
        # Reuse one PID only after worker 1 has otherwise qualified.  The
        # global fold-back must retroactively reject worker 1 as well.
        if calls == 193:
            sample["process_id"] = 1001
        return sample

    report = profiler.profile_python_repository_chunk_gate(
        subject_roots=roots,
        manifest_path=manifest,
        _sample_runner=reusing_runner,
        _candidate_observer=_candidate_receipt,
        _benchmark_observer=_benchmark_receipt,
    )

    assert report["process_isolation"]["passed"] is False
    assert report["process_isolation"]["duplicate_process_ids"] == [1001]
    assert report["decision"]["qualifying_worker_counts"] == []
    assert all(
        not worker["passed_all_four_cells"] for worker in report["workers"].values()
    )


@pytest.mark.parametrize("drift", ["benchmark", "candidate", "subject-source"])
def test_final_identity_drift_rejects_every_qualifier(
    drift: str, tmp_path: Path
) -> None:
    manifest, roots = _write_manifest(tmp_path)
    benchmark_calls = 0
    candidate_calls = 0

    def benchmark_observer() -> dict[str, Any]:
        nonlocal benchmark_calls
        benchmark_calls += 1
        receipt = _benchmark_receipt()
        if drift == "benchmark" and benchmark_calls > 1:
            receipt["harness"] = "changed"
        return receipt

    def candidate_observer() -> dict[str, Any]:
        nonlocal candidate_calls
        candidate_calls += 1
        receipt = _candidate_receipt()
        if drift == "candidate" and candidate_calls > 1:
            receipt["native_binary"]["sha256"] = "sha256:" + "9" * 64
        return receipt

    runner = _fake_sample_runner()
    calls = 0

    def sample_runner(config: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        sample = runner(config)
        if drift == "subject-source" and calls == 576:
            root = roots["codenib"]
            _git(root, "update-index", "--assume-unchanged", "package.py")
            (root / "package.py").write_text(
                "def changed_after_measurement():\n    pass\n", encoding="utf-8"
            )
        return sample

    report = profiler.profile_python_repository_chunk_gate(
        subject_roots=roots,
        manifest_path=manifest,
        _sample_runner=sample_runner,
        _candidate_observer=candidate_observer,
        _benchmark_observer=benchmark_observer,
    )

    assert report["passed"] is False
    assert report["decision"]["qualifying_worker_counts"] == []
    assert report["preflight"]["unchanged"] is False
    assert all(
        not cell["decision"]["gates"]["identity_unchanged"]
        for worker in report["workers"].values()
        for cell in worker["cells"].values()
    )


def test_missing_adapter_fails_after_complete_preflight(tmp_path: Path) -> None:
    manifest, roots = _write_manifest(tmp_path)

    def missing() -> dict[str, Any]:
        raise RuntimeError("candidate adapter is unavailable")

    report = profiler.profile_python_repository_chunk_gate(
        subject_roots=roots,
        manifest_path=manifest,
        _candidate_observer=missing,
        _benchmark_observer=_benchmark_receipt,
    )

    assert report["passed"] is False
    assert report["failure"]["stage"] == "candidate-preflight"
    assert len(report["preflight"]["subjects"]) == 2
    assert all(
        len(subject["selected_sources_before"]) == 2
        for subject in report["preflight"]["subjects"]
    )
    assert report["workers"] == {}


def test_measurement_exception_remains_failed_not_performance_rejected(
    tmp_path: Path,
) -> None:
    manifest, roots = _write_manifest(tmp_path)

    def broken_sample(_config: dict[str, Any]) -> dict[str, Any]:
        raise TimeoutError("synthetic worker timeout")

    report = profiler.profile_python_repository_chunk_gate(
        subject_roots=roots,
        manifest_path=manifest,
        iterations=1,
        warmups=0,
        worker_counts=(1,),
        _sample_runner=broken_sample,
        _candidate_observer=_candidate_receipt,
        _benchmark_observer=_benchmark_receipt,
    )

    assert report["status"] == "failed"
    assert report["failure"] == {
        "stage": "measurement",
        "error_type": "TimeoutError",
        "message": "synthetic worker timeout",
    }
    assert report["passed"] is False
    assert report["promotion_eligible"] is False
    assert report["decision"]["selected_worker_count"] is None


def test_late_measurement_failure_clears_partial_worker_qualification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, roots = _write_manifest(tmp_path)
    monkeypatch.setattr(
        profiler,
        "_peak_rss_source",
        lambda: profiler.CANONICAL_PEAK_RSS_SOURCE,
    )
    green_sample = _fake_sample_runner()
    call_count = 0

    def fail_after_first_worker(config: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count > 192:
            raise TimeoutError("synthetic late worker timeout")
        return green_sample(config)

    report = profiler.profile_python_repository_chunk_gate(
        subject_roots=roots,
        manifest_path=manifest,
        _sample_runner=fail_after_first_worker,
        _candidate_observer=_candidate_receipt,
        _benchmark_observer=_benchmark_receipt,
    )

    assert report["status"] == "failed"
    assert report["failure"]["stage"] == "measurement"
    assert report["process_isolation"] == {
        "sample_count": 192,
        "expected_sample_count": 576,
        "sample_set_complete": False,
        "unique_process_count": 192,
        "duplicate_process_ids": [],
        "passed": False,
    }
    assert report["decision"]["qualifying_worker_counts"] == []
    assert report["decision"]["selected_worker_count"] is None
    assert report["workers"]["1"]["passed_all_four_cells"] is False
    assert all(
        not cell["decision"]["gates"]["process_ids_unique"]
        for cell in report["workers"]["1"]["cells"].values()
    )


def test_selector_drift_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "module.py").write_text("def run():\n    pass\n", encoding="utf-8")
    configuration = profiler.CONFIGURATIONS[0]
    monkeypatch.setattr(
        profiler, "_discover_selected_python_sources", lambda *_args: []
    )

    with pytest.raises(RuntimeError, match="filter drifted"):
        profiler._assert_selector_alignment(root, configuration)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("attached", "detached"),
        ("dirty", "clean"),
        ("remote", "remote"),
        ("hidden-source", "selected-source receipt changed"),
    ],
)
def test_subject_identity_and_source_mutations_fail_preflight(
    mutation: str, message: str, tmp_path: Path
) -> None:
    manifest, roots = _write_manifest(tmp_path)
    root = roots["codenib"]
    if mutation == "attached":
        _git(root, "switch", "-q", "-c", "fixture-branch")
    elif mutation == "dirty":
        (root / "untracked.py").write_text("value = 1\n", encoding="utf-8")
    elif mutation == "remote":
        _git(root, "remote", "set-url", "origin", "https://example.invalid/wrong.git")
    else:
        _git(root, "update-index", "--assume-unchanged", "package.py")
        (root / "package.py").write_text("def changed():\n    pass\n", encoding="utf-8")
        assert _git(root, "status", "--porcelain=v1") == ""

    with pytest.raises(ValueError, match=message):
        profiler._preflight_subjects(manifest, roots)


def test_candidate_lazy_result_properties_are_inside_stopwatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    receipt = {
        "schema_version": 1,
        "configuration_id": profiler.CONFIGURATIONS[0]["id"],
        "contract_sha256": "sha256:" + "1" * 64,
        "selected_source_sha256": "sha256:" + "2" * 64,
        "materialization_identity_sha256": "sha256:" + "3" * 64,
        "file_count": 1,
        "total_bytes": 1,
        "first_path": "a.py",
        "last_path": "a.py",
    }
    chunk = CodeChunk("x", 0, 0, "function", "f", "a.py", "a.py:f")

    class Result:
        @property
        def chunks(self):
            events.append("chunks")
            return [chunk]

        @property
        def nodes(self):
            events.append("nodes")
            return ["a.py:f"]

        @property
        def diagnostics(self):
            events.append("diagnostics")
            return {}

    adapter_module = types.SimpleNamespace(
        run_python_repository_batch=lambda *_args, **_kwargs: Result()
    )
    real_import = profiler.importlib.import_module

    def import_module(name: str):
        if name == profiler._ADAPTER_MODULE:
            events.append("adapter-import")
            return adapter_module
        return real_import(name)

    ticks = iter((1.0, 2.0))

    def clock() -> float:
        events.append("clock")
        return next(ticks)

    monkeypatch.setattr(profiler.importlib, "import_module", import_module)
    monkeypatch.setattr(profiler, "_selected_source_receipt", lambda *_args: receipt)
    monkeypatch.setattr(profiler, "_peak_rss_bytes", lambda: 100)
    monkeypatch.setattr(profiler.time, "perf_counter", clock)
    monkeypatch.setattr(profiler.time, "process_time", lambda: 1.0)

    result = profiler._worker(
        {
            "arm": "candidate",
            "subject_id": "subject",
            "configuration_id": profiler.CONFIGURATIONS[0]["id"],
            "worker_count": 1,
            "root": str(tmp_path),
            "selected_source_receipt": receipt,
        }
    )

    first_clock, second_clock = [
        index for index, event in enumerate(events) if event == "clock"
    ]
    assert first_clock < events.index("adapter-import")
    assert events.index("chunks") < second_clock
    assert events.index("nodes") < second_clock
    assert events.index("diagnostics") > second_clock
    assert result["wall_seconds"] == 1.0


@pytest.mark.parametrize("invalid", ["tuple-chunks", "fake-chunk", "tuple-nodes"])
def test_worker_rejects_lazy_or_non_codechunk_candidate_output(
    invalid: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    receipt = {
        "schema_version": 1,
        "configuration_id": profiler.CONFIGURATIONS[0]["id"],
        "contract_sha256": "sha256:" + "1" * 64,
        "selected_source_sha256": "sha256:" + "2" * 64,
        "materialization_identity_sha256": "sha256:" + "3" * 64,
        "file_count": 1,
        "total_bytes": 1,
        "first_path": "a.py",
        "last_path": "a.py",
    }
    chunk: Any = CodeChunk("x", 0, 0, "function", "f", "a.py", "a.py:f")
    chunks: Any = [chunk]
    nodes: Any = ["a.py:f"]
    if invalid == "tuple-chunks":
        chunks = tuple(chunks)
    elif invalid == "fake-chunk":
        chunks = [types.SimpleNamespace(**chunk._asdict())]
    else:
        nodes = tuple(nodes)
    result = types.SimpleNamespace(chunks=chunks, nodes=nodes, diagnostics={})
    adapter_module = types.SimpleNamespace(
        run_python_repository_batch=lambda *_args, **_kwargs: result
    )
    real_import = profiler.importlib.import_module

    def import_module(name: str):
        return adapter_module if name == profiler._ADAPTER_MODULE else real_import(name)

    monkeypatch.setattr(profiler.importlib, "import_module", import_module)
    monkeypatch.setattr(profiler, "_selected_source_receipt", lambda *_args: receipt)
    monkeypatch.setattr(profiler, "_peak_rss_bytes", lambda: 100)

    with pytest.raises(TypeError):
        profiler._worker(
            {
                "arm": "candidate",
                "subject_id": "subject",
                "configuration_id": profiler.CONFIGURATIONS[0]["id"],
                "worker_count": 1,
                "root": str(tmp_path),
                "selected_source_receipt": receipt,
            }
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"iterations": True},
        {"iterations": 1.5},
        {"warmups": True},
        {"warmups": 1.5},
        {"threshold": float("nan")},
        {"threshold": True},
        {"rss_ratio_limit": float("inf")},
        {"rss_ratio_limit": True},
        {"worker_counts": (True,)},
        {"worker_counts": ("1",)},
        {"worker_timeout_seconds": float("nan")},
        {"worker_timeout_seconds": True},
    ],
)
def test_controller_rejects_malformed_protocol_values(
    overrides: dict[str, Any]
) -> None:
    arguments = {"subject_roots": {}}
    arguments.update(overrides)

    with pytest.raises(ValueError):
        profiler.profile_python_repository_chunk_gate(**arguments)


@pytest.mark.parametrize("worker_count", [True, "1"])
def test_worker_rejects_non_integer_worker_count(
    worker_count: Any, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="worker_count"):
        profiler._worker(
            {
                "arm": "candidate",
                "configuration_id": profiler.CONFIGURATIONS[0]["id"],
                "worker_count": worker_count,
                "root": str(tmp_path),
            }
        )


def test_malformed_cli_value_still_atomically_writes_standard_json(
    tmp_path: Path,
) -> None:
    output = tmp_path / "report.json"
    output.write_text("old", encoding="utf-8")

    return_code = profiler.main(
        ["--promotion-threshold", "nan", "--output", str(output)]
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert return_code == 1
    assert report["failure"]["stage"] == "controller"
    assert report["configuration"]["promotion_threshold"] == "nan"
    assert list(tmp_path.glob(".report.json.*.tmp")) == []


def test_parser_has_default_atomic_output() -> None:
    args = profiler._parser().parse_args([])

    assert args.output == profiler._DEFAULT_OUTPUT
