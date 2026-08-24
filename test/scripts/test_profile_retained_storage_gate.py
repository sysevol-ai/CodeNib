# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from scripts.profiling import profile_retained_storage_gate as profiler

_SHA_A = "sha256:" + "a" * 64
_SHA_B = "sha256:" + "b" * 64
_SHA_C = "sha256:" + "c" * 64
_SHA_D = "sha256:" + "d" * 64
_SOURCE_V2 = "sha256-v2:" + "e" * 64
_SNAPSHOT_ID = "snapshot_" + "f" * 64


def _json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _make_subject(root: Path, *, repository: str, payload: str) -> tuple[str, str]:
    root.mkdir()
    (root / "package.py").write_text(payload, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "CodeNib Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", "package.py")
    _git(root, "commit", "-qm", "fixture")
    _git(root, "remote", "add", "origin", repository)
    revision = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    return revision, tree


def _subject_row(
    *,
    identifier: str,
    repository: str,
    revision: str,
    tree: str,
    payload_class: str,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "repository": repository,
        "revision": revision,
        "tree": tree,
        "payload_class": payload_class,
        "languages": ["python"],
        "repository_key": f"github.com/example/{identifier}",
        "source_selection": {
            "schema": "codenib.repository-source-selection.v1",
            "repository_filter_policy": 4,
            "exclude_subtrees": [],
        },
        "queries": [
            {"text": "package", "top_k": 5, "filter_test": False},
        ],
    }


def _manifest(subjects: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": profiler.MANIFEST_SCHEMA_VERSION,
        "benchmark": profiler.BENCHMARK_ID,
        "policy": {
            "status": "unratified",
            "canonical_iterations": profiler.DEFAULT_ITERATIONS,
            "canonical_warmups": profiler.DEFAULT_WARMUPS,
            "min_payload_classes": ["small", "medium", "large"],
            "min_media_classes": 2,
        },
        "cells": list(profiler.CELLS),
        "view_sets": [
            {
                "id": "bm25-fast",
                "views": ["bm25"],
                "index_args": ["--preset", "fast"],
            }
        ],
        "subjects": subjects,
    }


def _write_manifest(path: Path, manifest: Mapping[str, Any]) -> Path:
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _fixture(
    tmp_path: Path,
) -> tuple[Path, dict[str, Path], dict[str, Path]]:
    subject_roots: dict[str, Path] = {}
    subjects: list[dict[str, Any]] = []
    for index, payload_class in enumerate(("small", "medium", "large"), start=1):
        identifier = f"subject-{payload_class}"
        repository = f"https://github.com/example/{identifier}.git"
        root = tmp_path / identifier
        revision, tree = _make_subject(
            root,
            repository=repository,
            payload=f"def value_{index}():\n    return {index}\n",
        )
        subject_roots[identifier] = root
        subjects.append(
            _subject_row(
                identifier=identifier,
                repository=repository,
                revision=revision,
                tree=tree,
                payload_class=payload_class,
            )
        )

    media_roots = {
        "local-a": tmp_path / "media-a",
        "local-b": tmp_path / "media-b",
    }
    for root in media_roots.values():
        root.mkdir()
    manifest_path = _write_manifest(tmp_path / "subjects.json", _manifest(subjects))
    return manifest_path, subject_roots, media_roots


def _use_clean_benchmark_checkout(
    monkeypatch: pytest.MonkeyPatch, subject_roots: Mapping[str, Path]
) -> None:
    benchmark_root = next(iter(subject_roots.values())).parent / "benchmark-checkout"
    _make_subject(
        benchmark_root,
        repository="https://github.com/example/benchmark-checkout.git",
        payload="def benchmark_harness():\n    return True\n",
    )
    monkeypatch.setattr(profiler, "_PROJECT_ROOT", benchmark_root)


def _use_distinct_media_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    observe = profiler._media_identity

    def distinct(path: Path) -> dict[str, Any]:
        identity = observe(path)
        suffix = 1 if path.name.endswith("a") else 2
        return {
            **identity,
            "device": suffix,
            "filesystem": f"fixture-fs-{suffix}",
            "mount_source": f"fixture-media-{suffix}",
        }

    monkeypatch.setattr(profiler, "_media_identity", distinct)


def _parity_identity(request: Mapping[str, Any]) -> dict[str, Any]:
    subject = request["subject"]
    queries_sha256 = (
        _SHA_B
        if request["cell"] == "runtime-cold" and request["arm"] == "candidate"
        else _SHA_A
    )
    return {
        "manifest": {
            "commit": subject["revision"],
            "source_fingerprint": _SOURCE_V2,
            "source_selection_digest": _json_digest(subject["source_selection"]),
            "semantic_sha256": _SHA_B,
            "languages": list(subject["languages"]),
            "file_count": 1,
        },
        "view": {
            "documents_sha256": _SHA_C,
            "metadata_sha256": _SHA_D,
        },
        "queries": {
            "sha256": queries_sha256,
            "count": len(subject["queries"]),
            "nonempty": True,
        },
        "authority": _authority_identity(request),
    }


def _authority_identity(request: Mapping[str, Any]) -> dict[str, Any]:
    cell = request["cell"]
    arm = request["arm"]
    if cell in {"compiler-cold", "compiler-current"}:
        return {
            "context_kind": "compiler-portable-plan",
            "artifact": None,
            "source_verified": None,
            "source_verification_scope": None,
        }
    if cell == "runtime-cold" and arm == "legacy":
        return {
            "context_kind": "manifest-live-source",
            "artifact": None,
            "source_verified": True,
            "source_verification_scope": "content-bytes",
        }

    from codenib.artifacts.context import CONTEXT_ARTIFACT_SCHEMA

    return {
        "context_kind": "portable-artifact-query-only",
        "artifact": {
            "verified": True,
            "schema": CONTEXT_ARTIFACT_SCHEMA,
            "repository": request["subject"]["repository_key"],
            "commit": request["subject"]["revision"],
            "views": ["bm25"],
        },
        "source_verified": False,
        "source_verification_scope": None,
    }


def _sample_receipt(request: Mapping[str, Any], *, process_id: int) -> dict[str, Any]:
    parity = _parity_identity(request)
    result = deepcopy(parity)
    result["view"].update({"payload_bytes": 128, "payload_files": 2})
    result["retained_view"] = (
        deepcopy(result["view"]) if request["arm"] == "candidate" else None
    )
    if request["arm"] == "legacy":
        result["snapshot"] = {
            "snapshot_id": None,
            "ref_name": None,
            "generation": None,
            "changed": None,
        }
    else:
        changed = {
            "compiler-cold": True,
            "compiler-current": False,
            "runtime-cold": None,
            "runtime-cold-query-only": None,
        }[request["cell"]]
        result["snapshot"] = {
            "snapshot_id": _SNAPSHOT_ID,
            "ref_name": "main",
            "generation": 1,
            "changed": changed,
        }
    return {
        "schema_version": profiler.REPORT_SCHEMA_VERSION,
        "operation": request["operation"],
        "run_id": request["run_id"],
        "arm": request["arm"],
        "phase": request["phase"],
        "round_index": request["round_index"],
        "cell": request["cell"],
        "subject_id": request["subject"]["id"],
        "media_id": request["media_id"],
        "view_set_id": request["view_set"]["id"],
        "process_id": process_id,
        "metrics": {
            "route_wall_seconds": 2.0 if request["arm"] == "legacy" else 1.0,
            "process_wall_seconds": 2.0 if request["arm"] == "legacy" else 1.0,
            "cpu_seconds": 0.5,
            "peak_rss_bytes": 1024,
            "io_read_bytes": 64,
            "io_write_bytes": 32,
            "payload_bytes": 128,
            "payload_files": 2,
        },
        "parity_identity": parity,
        "result": result,
        "safety": {
            "subject_unchanged": True,
            "sample_root_fresh": True,
            "cleanup_complete": True,
            "storage_closed": True,
            "context_closed": True,
            "ref_stable": True,
            "retained_matches_raw": True,
        },
    }


def _fake_sample_runner(
    mutate: Callable[[dict[str, Any], Mapping[str, Any], int], None] | None = None,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    call_count = 0

    def run(request: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal call_count
        call_count += 1
        receipt = _sample_receipt(request, process_id=10_000 + call_count)
        if mutate is not None:
            mutate(receipt, request, call_count)
        return receipt

    return run


def _run_fake_cell(
    *,
    manifest_path: Path,
    subject_roots: Mapping[str, Path],
    media_roots: Mapping[str, Path],
    cell: str,
    sample_runner: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    manifest, _receipt = profiler.load_subject_manifest(manifest_path)
    subject = manifest["subjects"][0]
    media_id, media_root = next(iter(media_roots.items()))
    process_ids: list[int] = []
    result = profiler._run_cell(
        subject=subject,
        subject_root=subject_roots[subject["id"]],
        media_id=media_id,
        media_root=media_root,
        media_identity=profiler._media_identity(media_root),
        view_set=manifest["view_sets"][0],
        cell=cell,
        iterations=1,
        warmups=0,
        sample_runner=sample_runner,
        process_ids=process_ids,
    )
    assert len(process_ids) == 2
    assert len(set(process_ids)) == 2
    return result


def test_public_v2_contract_and_deterministic_paired_order() -> None:
    assert profiler.ARMS == ("legacy", "candidate")
    assert profiler.CELLS == (
        "compiler-cold",
        "compiler-current",
        "runtime-cold",
        "runtime-cold-query-only",
    )
    assert profiler.TRACKS == {
        "compiler": ("compiler-cold", "compiler-current"),
        "query-only-runtime": ("runtime-cold-query-only",),
        "manifest-runtime-compatibility": ("runtime-cold",),
    }
    assert profiler.CANONICAL_SAMPLE_COUNT == 1152
    assert [profiler.paired_arm_order(index) for index in range(4)] == [
        ("legacy", "candidate"),
        ("candidate", "legacy"),
        ("legacy", "candidate"),
        ("candidate", "legacy"),
    ]


def test_runtime_manifest_sentinel_is_negative_but_query_only_is_exact(
    tmp_path: Path,
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    manifest, _receipt = profiler.load_subject_manifest(manifest_path)
    subject = manifest["subjects"][0]

    sentinel = _run_fake_cell(
        manifest_path=manifest_path,
        subject_roots=subject_roots,
        media_roots=media_roots,
        cell="runtime-cold",
        sample_runner=_fake_sample_runner(),
    )
    query_only = _run_fake_cell(
        manifest_path=manifest_path,
        subject_roots=subject_roots,
        media_roots=media_roots,
        cell="runtime-cold-query-only",
        sample_runner=_fake_sample_runner(),
    )

    sentinel_pair = sentinel["runs"]["measured"][0]
    legacy_sentinel, candidate_sentinel = sentinel_pair["samples"]
    assert sentinel_pair["parity"] is False
    assert sentinel["parity"]["every_pair"] is False
    assert sentinel["parity"]["stable_across_runs"] is False
    assert sentinel["parity"]["passed"] is False
    assert len(sentinel["parity"]["identity_sha256"]) == 2
    assert (
        legacy_sentinel["result"]["queries"]["sha256"]
        != candidate_sentinel["result"]["queries"]["sha256"]
    )
    assert legacy_sentinel["result"]["authority"] == {
        "context_kind": "manifest-live-source",
        "artifact": None,
        "source_verified": True,
        "source_verification_scope": "content-bytes",
    }
    assert candidate_sentinel["result"]["authority"] == _authority_identity(
        {
            "cell": "runtime-cold",
            "arm": "candidate",
            "subject": subject,
        }
    )

    query_only_pair = query_only["runs"]["measured"][0]
    assert query_only_pair["parity"] is True
    assert query_only["parity"]["passed"] is True
    assert (
        query_only_pair["samples"][0]["parity_identity"]
        == query_only_pair["samples"][1]["parity_identity"]
    )


def test_runtime_manifest_authority_mismatch_blocks_equal_query_digest(
    tmp_path: Path,
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)

    def align_query_digest(
        receipt: dict[str, Any], request: Mapping[str, Any], _call: int
    ) -> None:
        if request["cell"] == "runtime-cold" and request["arm"] == "candidate":
            receipt["result"]["queries"]["sha256"] = _SHA_A
            receipt["parity_identity"]["queries"]["sha256"] = _SHA_A

    sentinel = _run_fake_cell(
        manifest_path=manifest_path,
        subject_roots=subject_roots,
        media_roots=media_roots,
        cell="runtime-cold",
        sample_runner=_fake_sample_runner(align_query_digest),
    )

    pair = sentinel["runs"]["measured"][0]
    legacy, candidate = pair["samples"]
    assert legacy["result"]["queries"] == candidate["result"]["queries"]
    assert legacy["result"]["authority"] != candidate["result"]["authority"]
    assert pair["parity"] is False
    assert sentinel["parity"]["passed"] is False


def test_track_aggregation_isolates_parity_and_completeness(
    tmp_path: Path,
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    cells = {
        cell: _run_fake_cell(
            manifest_path=manifest_path,
            subject_roots=subject_roots,
            media_roots=media_roots,
            cell=cell,
            sample_runner=_fake_sample_runner(),
        )
        for cell in profiler.CELLS
    }

    baseline = profiler._aggregate_tracks(
        cells,
        expected_instances_per_cell=1,
        iterations=1,
        warmups=0,
    )
    assert {name: track["passed"] for name, track in baseline.items()} == {
        "compiler": True,
        "query-only-runtime": True,
        "manifest-runtime-compatibility": False,
    }
    assert all(track["measurement_complete"] for track in baseline.values())

    query_parity_red = deepcopy(cells)
    query_parity_red["runtime-cold-query-only"]["parity"]["passed"] = False
    query_tracks = profiler._aggregate_tracks(
        query_parity_red,
        expected_instances_per_cell=1,
        iterations=1,
        warmups=0,
    )
    assert query_tracks["compiler"] == baseline["compiler"]
    assert query_tracks["query-only-runtime"]["measurement_complete"] is True
    assert query_tracks["query-only-runtime"]["parity_passed"] is False
    assert query_tracks["query-only-runtime"]["safety_passed"] is True
    assert query_tracks["query-only-runtime"]["passed"] is False
    assert (
        query_tracks["manifest-runtime-compatibility"]
        == baseline["manifest-runtime-compatibility"]
    )

    missing_query_cell = {
        name: value
        for name, value in cells.items()
        if name != "runtime-cold-query-only"
    }
    incomplete_tracks = profiler._aggregate_tracks(
        missing_query_cell,
        expected_instances_per_cell=1,
        iterations=1,
        warmups=0,
    )
    assert incomplete_tracks["compiler"] == baseline["compiler"]
    assert incomplete_tracks["query-only-runtime"] == {
        "cells": ["runtime-cold-query-only"],
        "measurement_complete": False,
        "parity_passed": False,
        "safety_passed": False,
        "passed": False,
        "policy_status": "unratified",
        "promotion_eligible": False,
    }
    assert (
        incomplete_tracks["manifest-runtime-compatibility"]
        == baseline["manifest-runtime-compatibility"]
    )


def test_summary_uses_median_and_nearest_rank_p95() -> None:
    summary = profiler.summarize_samples(range(1, 21))

    assert summary["p50"] == 10.5
    assert summary["p95"] == 19.0


def test_full_public_query_payload_keeps_optional_content_in_the_digest() -> None:
    subject = {"queries": [{"text": "package", "top_k": 5, "filter_test": False}]}
    live_source_payload = [
        {
            "query": dict(subject["queries"][0]),
            "results": [
                {
                    "file_path": "package.py",
                    "start_line": 1,
                    "end_line": 2,
                    "score": 1.0,
                    "content": "def package():\n    return True\n",
                }
            ],
        }
    ]
    source_disabled_payload = deepcopy(live_source_payload)
    source_disabled_payload[0]["results"][0]["content"] = None

    live_identity = profiler._query_identity_from_payload(  # noqa: SLF001
        live_source_payload,
        subject=subject,
    )
    source_disabled_identity = profiler._query_identity_from_payload(  # noqa: SLF001
        source_disabled_payload,
        subject=subject,
    )

    assert live_identity["sha256"] == _json_digest(live_source_payload)
    assert source_disabled_identity["sha256"] == _json_digest(source_disabled_payload)
    assert live_identity != source_disabled_identity
    assert (
        profiler._query_identity_from_payload(  # noqa: SLF001
            deepcopy(source_disabled_payload),
            subject=subject,
        )
        == source_disabled_identity
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda manifest: manifest.__setitem__("schema_version", 1),
        lambda manifest: manifest.__setitem__("unexpected", True),
        lambda manifest: manifest.__setitem__("benchmark", "wrong"),
        lambda manifest: manifest["cells"].reverse(),
        lambda manifest: manifest["view_sets"][0]["views"].append("vector"),
        lambda manifest: manifest["subjects"][0].__setitem__("tree", "A" * 40),
        lambda manifest: manifest["subjects"][0]["queries"].clear(),
    ],
)
def test_manifest_schema_is_exact_and_bm25_only(
    mutation: Callable[[dict[str, Any]], None], tmp_path: Path
) -> None:
    subjects = [
        _subject_row(
            identifier=f"fixture-{payload_class}",
            repository=f"https://github.com/example/fixture-{payload_class}.git",
            revision=str(index) * 40,
            tree=str(index + 3) * 40,
            payload_class=payload_class,
        )
        for index, payload_class in enumerate(("small", "medium", "large"), start=1)
    ]
    manifest = _manifest(subjects)
    mutation(manifest)
    path = _write_manifest(tmp_path / "manifest.json", manifest)

    with pytest.raises(ValueError):
        profiler.load_subject_manifest(path)


def test_v2_manifest_accepts_only_the_frozen_four_cell_order(tmp_path: Path) -> None:
    subjects = [
        _subject_row(
            identifier=f"fixture-{payload_class}",
            repository=f"https://github.com/example/fixture-{payload_class}.git",
            revision=str(index) * 40,
            tree=str(index + 3) * 40,
            payload_class=payload_class,
        )
        for index, payload_class in enumerate(("small", "medium", "large"), start=1)
    ]
    path = _write_manifest(tmp_path / "manifest.json", _manifest(subjects))

    manifest, receipt = profiler.load_subject_manifest(path)

    assert set(manifest) == {
        "schema_version",
        "benchmark",
        "policy",
        "cells",
        "view_sets",
        "subjects",
    }
    assert manifest["schema_version"] == 2
    assert manifest["benchmark"] == "retained_storage_explicit_route_gate_v2"
    assert manifest["cells"] == list(profiler.CELLS)
    metadata = path.stat()
    assert receipt == {
        "path": os.fspath(path.resolve()),
        "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": len(path.read_bytes()),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
    }


def test_atomic_writer_emits_canonical_json_and_preserves_old_report_on_nan(
    tmp_path: Path,
) -> None:
    output = tmp_path / "report.json"
    output.write_text("old-report", encoding="utf-8")
    report = {"z": [3, 2, 1], "a": {"value": 1}}

    profiler.write_report_atomic(output, report)

    assert output.read_bytes() == json.dumps(
        report,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

    with pytest.raises(ValueError):
        profiler.write_report_atomic(output, {"value": math.nan})
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert list(tmp_path.iterdir()) == [output]


def test_canonical_v2_report_has_exact_shape_tracks_and_process_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    _use_clean_benchmark_checkout(monkeypatch, subject_roots)
    _use_distinct_media_classes(monkeypatch)
    _manifest_value, manifest_receipt = profiler.load_subject_manifest(manifest_path)
    monkeypatch.setattr(profiler, "_DEFAULT_MANIFEST", manifest_path)
    monkeypatch.setattr(
        profiler, "_CANONICAL_MANIFEST_SHA256", manifest_receipt["sha256"]
    )
    monkeypatch.setattr(profiler, "_CANONICAL_MANIFEST_SIZE", manifest_receipt["size"])
    fake_runner = _fake_sample_runner()

    def isolated_runner(
        request: Mapping[str, Any], *, timeout_seconds: float
    ) -> Mapping[str, Any]:
        assert timeout_seconds == profiler.DEFAULT_WORKER_TIMEOUT_SECONDS
        return fake_runner(request)

    monkeypatch.setattr(profiler, "_run_isolated_sample", isolated_runner)

    report = profiler.profile_retained_storage_gate(
        subject_roots=subject_roots,
        media_roots=media_roots,
        manifest_path=manifest_path,
    )

    assert set(report) == {
        "schema_version",
        "benchmark",
        "status",
        "passed",
        "promotion_eligible",
        "failure",
        "policy",
        "protocol",
        "configuration",
        "benchmark_receipts",
        "subjects",
        "media",
        "cells",
        "tracks",
        "process_isolation",
        "decision",
    }
    assert report["schema_version"] == profiler.REPORT_SCHEMA_VERSION
    assert report["benchmark"] == profiler.BENCHMARK_ID
    assert report["status"] == "complete", report["failure"]
    assert report["passed"] is False
    assert report["promotion_eligible"] is False
    assert report["failure"] is None
    assert report["policy"] == {
        "status": "unratified",
        "performance_budgets": None,
        "promotion_eligible": False,
    }
    assert set(report["protocol"]) == {"expected", "observed", "canonical"}
    assert report["protocol"]["canonical"] is True
    assert report["protocol"]["expected"] == report["protocol"]["observed"]
    assert set(report["protocol"]["expected"]) == {
        "iterations_per_arm",
        "warmups_per_arm",
        "cells",
        "tracks",
        "cell_authority_contracts",
        "canonical_sample_count",
        "view_set_ids",
        "payload_classes",
        "minimum_media_classes",
        "fresh_inner_process_per_sample",
        "runner",
        "paired_arm_order",
        "peak_rss_source",
        "io_source",
        "fixed_manifest_sha256",
        "fixed_manifest_size",
        "subject_receipts",
    }
    assert report["protocol"]["expected"]["cells"] == list(profiler.CELLS)
    assert report["protocol"]["expected"]["tracks"] == {
        name: list(cells) for name, cells in profiler.TRACKS.items()
    }
    assert report["protocol"]["expected"]["cell_authority_contracts"] == (
        profiler.CELL_AUTHORITY_CONTRACTS
    )
    assert report["protocol"]["expected"]["canonical_sample_count"] == 1152
    assert report["configuration"]["cell_authority_contracts"] == (
        profiler.CELL_AUTHORITY_CONTRACTS
    )
    assert report["decision"] == {
        "policy_status": "unratified",
        "report_only": True,
        "promotion_eligible": False,
        "recommendation": "retain-explicit-routes",
        "reason": "one or more compatibility tracks are parity red",
    }
    assert report["benchmark_receipts"]["unchanged"] is True
    assert len(report["subjects"]) == 3
    assert len(report["media"]) == 2
    assert len(report["cells"]) == 24
    assert {
        cell: sum(item["cell"] == cell for item in report["cells"].values())
        for cell in profiler.CELLS
    } == {cell: 6 for cell in profiler.CELLS}
    assert list(report["tracks"]) == [
        "compiler",
        "query-only-runtime",
        "manifest-runtime-compatibility",
    ]
    assert report["tracks"] == {
        "compiler": {
            "cells": ["compiler-cold", "compiler-current"],
            "measurement_complete": True,
            "parity_passed": True,
            "safety_passed": True,
            "passed": True,
            "policy_status": "unratified",
            "promotion_eligible": False,
        },
        "query-only-runtime": {
            "cells": ["runtime-cold-query-only"],
            "measurement_complete": True,
            "parity_passed": True,
            "safety_passed": True,
            "passed": True,
            "policy_status": "unratified",
            "promotion_eligible": False,
        },
        "manifest-runtime-compatibility": {
            "cells": ["runtime-cold"],
            "measurement_complete": True,
            "parity_passed": False,
            "safety_passed": True,
            "passed": False,
            "policy_status": "unratified",
            "promotion_eligible": False,
        },
    }
    assert report["process_isolation"]["passed"] is True
    assert report["process_isolation"]["expected_samples"] == 1152
    assert report["process_isolation"]["observed_samples"] == 1152
    assert len(report["process_isolation"]["inner_process_ids"]) == 1152
    assert len(set(report["process_isolation"]["inner_process_ids"])) == 1152
    assert report["process_isolation"]["duplicate_process_ids"] == []
    for cell in report["cells"].values():
        assert {"runs", "summary", "parity", "safety", "performance"} <= set(cell)
        assert len(cell["runs"]["warmups"]) == profiler.DEFAULT_WARMUPS
        assert len(cell["runs"]["measured"]) == profiler.DEFAULT_ITERATIONS


def test_injected_sample_runner_cannot_claim_process_isolated_protocol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    _use_clean_benchmark_checkout(monkeypatch, subject_roots)
    _use_distinct_media_classes(monkeypatch)
    _manifest_value, manifest_receipt = profiler.load_subject_manifest(manifest_path)
    monkeypatch.setattr(profiler, "_DEFAULT_MANIFEST", manifest_path)
    monkeypatch.setattr(
        profiler, "_CANONICAL_MANIFEST_SHA256", manifest_receipt["sha256"]
    )
    monkeypatch.setattr(profiler, "_CANONICAL_MANIFEST_SIZE", manifest_receipt["size"])

    report = profiler.profile_retained_storage_gate(
        subject_roots=subject_roots,
        media_roots=media_roots,
        manifest_path=manifest_path,
        sample_runner=_fake_sample_runner(),
    )

    assert report["status"] == "failed"
    assert report["failure"]["stage"] == "protocol"
    assert report["process_isolation"]["passed"] is True
    assert report["protocol"]["canonical"] is False
    assert report["protocol"]["observed"]["fresh_inner_process_per_sample"] is False
    assert report["protocol"]["observed"]["runner"] == "injected-sample-runner"
    assert report["promotion_eligible"] is False


def test_custom_manifest_receipt_cannot_claim_the_canonical_protocol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    _use_clean_benchmark_checkout(monkeypatch, subject_roots)
    _use_distinct_media_classes(monkeypatch)

    report = profiler.profile_retained_storage_gate(
        subject_roots=subject_roots,
        media_roots=media_roots,
        manifest_path=manifest_path,
        sample_runner=_fake_sample_runner(),
    )

    assert report["status"] == "failed"
    assert report["passed"] is False
    assert report["failure"]["stage"] == "protocol"
    assert report["protocol"]["canonical"] is False
    assert report["promotion_eligible"] is False


def test_mutating_default_manifest_bytes_cannot_move_the_canonical_anchor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    default_bytes = profiler._DEFAULT_MANIFEST.read_bytes()
    mutated = tmp_path / "mutated-default.json"
    mutated.write_bytes(b" \n" + default_bytes)
    manifest, receipt = profiler.load_subject_manifest(mutated)
    monkeypatch.setattr(profiler, "_DEFAULT_MANIFEST", mutated)
    media = {
        "first": {
            "device": 1,
            "filesystem": "fixture-fs-1",
            "mount_source": "fixture-media-1",
        },
        "second": {
            "device": 2,
            "filesystem": "fixture-fs-2",
            "mount_source": "fixture-media-2",
        },
    }

    try:
        protocol = profiler._protocol(
            manifest,
            manifest_receipt=receipt,
            iterations=profiler.DEFAULT_ITERATIONS,
            warmups=profiler.DEFAULT_WARMUPS,
            media=media,
            built_in_runner=True,
        )
    except (RuntimeError, ValueError):
        return

    assert protocol["canonical"] is False


def test_override_protocol_is_an_operational_failure_after_measurement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    _use_clean_benchmark_checkout(monkeypatch, subject_roots)

    report = profiler.profile_retained_storage_gate(
        subject_roots=subject_roots,
        media_roots=media_roots,
        manifest_path=manifest_path,
        iterations=1,
        warmups=0,
        worker_timeout_seconds=1.0,
        sample_runner=_fake_sample_runner(),
    )

    assert report["status"] == "failed"
    assert report["passed"] is False
    assert report["failure"]["stage"] == "protocol"
    assert report["process_isolation"]["observed_samples"] == 48
    assert report["protocol"]["canonical"] is False
    assert report["protocol"]["observed"]["iterations_per_arm"] == 1
    assert report["protocol"]["observed"]["warmups_per_arm"] == 0
    assert report["promotion_eligible"] is False


def test_sample_requests_are_exact_and_use_ab_ba_round_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    _use_clean_benchmark_checkout(monkeypatch, subject_roots)
    requests: list[dict[str, Any]] = []

    def runner(request: Mapping[str, Any]) -> Mapping[str, Any]:
        request_copy = deepcopy(dict(request))
        requests.append(request_copy)
        return _sample_receipt(request_copy, process_id=20_000 + len(requests))

    report = profiler.profile_retained_storage_gate(
        subject_roots=subject_roots,
        media_roots=media_roots,
        manifest_path=manifest_path,
        iterations=2,
        warmups=1,
        sample_runner=runner,
    )

    assert report["status"] == "failed"
    assert report["failure"]["stage"] == "protocol"
    assert all(
        set(request)
        == {
            "operation",
            "arm",
            "phase",
            "round_index",
            "cell",
            "subject",
            "subject_root",
            "media_id",
            "media_root",
            "media_identity",
            "view_set",
            "run_id",
        }
        for request in requests
    )
    first_cell = requests[:6]
    assert [(row["phase"], row["round_index"], row["arm"]) for row in first_cell] == [
        ("warmup", 0, "legacy"),
        ("warmup", 0, "candidate"),
        ("measured", 0, "legacy"),
        ("measured", 0, "candidate"),
        ("measured", 1, "candidate"),
        ("measured", 1, "legacy"),
    ]
    assert all(row["operation"] == "sample" for row in requests)
    assert all(
        set(row["media_identity"])
        == {"path", "device", "filesystem", "mount_source", "block_size"}
        for row in requests
    )


@pytest.mark.parametrize(
    ("error", "error_type"),
    [
        (RuntimeError("synthetic worker crash"), "RuntimeError"),
        (TimeoutError("synthetic worker timeout"), "TimeoutError"),
    ],
)
def test_worker_crash_and_timeout_produce_negative_reports(
    error: Exception,
    error_type: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    _use_clean_benchmark_checkout(monkeypatch, subject_roots)

    def broken_runner(_request: Mapping[str, Any]) -> Mapping[str, Any]:
        raise error

    report = profiler.profile_retained_storage_gate(
        subject_roots=subject_roots,
        media_roots=media_roots,
        manifest_path=manifest_path,
        iterations=1,
        warmups=0,
        sample_runner=broken_runner,
    )

    assert report["status"] == "failed"
    assert report["passed"] is False
    assert report["promotion_eligible"] is False
    assert report["failure"] == {
        "stage": "measurement",
        "error_type": error_type,
        "message": str(error),
    }
    assert report["process_isolation"]["passed"] is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda receipt, _request, _call: receipt["metrics"].__setitem__(
            "route_wall_seconds", math.nan
        ),
        lambda receipt, _request, _call: receipt["metrics"].__setitem__(
            "peak_rss_bytes", True
        ),
        lambda receipt, _request, _call: receipt.pop("result"),
        lambda receipt, _request, _call: receipt["safety"].__setitem__(
            "cleanup_complete", False
        ),
        lambda receipt, _request, _call: receipt["safety"].__setitem__(
            "subject_unchanged", False
        ),
        lambda receipt, _request, _call: receipt["result"]["manifest"].__setitem__(
            "source_fingerprint", _SHA_D
        ),
        lambda receipt, request, _call: (
            receipt["parity_identity"]["view"].__setitem__("documents_sha256", _SHA_B)
            if request["arm"] == "candidate"
            else None
        ),
    ],
)
def test_invalid_partial_unsafe_and_parity_mismatched_samples_fail_closed(
    mutate: Callable[[dict[str, Any], Mapping[str, Any], int], None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    _use_clean_benchmark_checkout(monkeypatch, subject_roots)

    report = profiler.profile_retained_storage_gate(
        subject_roots=subject_roots,
        media_roots=media_roots,
        manifest_path=manifest_path,
        iterations=1,
        warmups=0,
        sample_runner=_fake_sample_runner(mutate),
    )

    assert report["status"] == "failed"
    assert report["passed"] is False
    assert report["promotion_eligible"] is False
    assert report["failure"]["stage"] == "measurement"
    assert report["process_isolation"]["passed"] is False


def test_source_mutation_after_preflight_fails_postflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    _use_clean_benchmark_checkout(monkeypatch, subject_roots)
    mutated = False

    def runner(request: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal mutated
        if not mutated:
            mutated = True
            root = Path(request["subject_root"])
            (root / "package.py").write_text(
                "def changed_during_measurement():\n    return False\n",
                encoding="utf-8",
            )
        return _sample_receipt(request, process_id=30_000)

    report = profiler.profile_retained_storage_gate(
        subject_roots=subject_roots,
        media_roots=media_roots,
        manifest_path=manifest_path,
        iterations=1,
        warmups=0,
        sample_runner=runner,
    )

    assert report["status"] == "failed"
    assert report["passed"] is False
    assert report["promotion_eligible"] is False
    assert report["failure"]["stage"] in {"measurement", "postflight"}


def test_missing_subject_and_non_directory_media_fail_before_measurement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    _use_clean_benchmark_checkout(monkeypatch, subject_roots)
    calls = 0

    def runner(request: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal calls
        calls += 1
        return _sample_receipt(request, process_id=40_000 + calls)

    missing_subject = dict(subject_roots)
    missing_subject.pop("subject-small")
    media_file = tmp_path / "not-a-media-directory"
    media_file.write_text("not a directory", encoding="utf-8")
    invalid_media = {**media_roots, "broken": media_file}

    reports = [
        profiler.profile_retained_storage_gate(
            subject_roots=missing_subject,
            media_roots=media_roots,
            manifest_path=manifest_path,
            iterations=1,
            warmups=0,
            sample_runner=runner,
        ),
        profiler.profile_retained_storage_gate(
            subject_roots=subject_roots,
            media_roots=invalid_media,
            manifest_path=manifest_path,
            iterations=1,
            warmups=0,
            sample_runner=runner,
        ),
    ]

    assert calls == 0
    assert all(report["status"] == "failed" for report in reports)
    assert all(report["passed"] is False for report in reports)
    assert all(report["promotion_eligible"] is False for report in reports)
    assert all(
        report["failure"]["stage"] in {"controller-validation", "preflight"}
        for report in reports
    )


def test_each_cell_uses_its_exact_prep_and_runtime_cli_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    manifest, _receipt = profiler.load_subject_manifest(manifest_path)
    subject = manifest["subjects"][0]
    subject_root = subject_roots[subject["id"]]
    media_id, media_root = next(iter(media_roots.items()))
    sample_root = media_root / f"{profiler._TEMP_PREFIX}{'7' * 32}"
    paths = profiler._sample_paths(sample_root, "7" * 32)
    calls: list[tuple[str, Any]] = []

    monkeypatch.setattr(
        profiler,
        "_provision_storage",
        lambda actual_paths: calls.append(("provision", dict(actual_paths))),
    )
    monkeypatch.setattr(
        profiler,
        "_invoke_cli",
        lambda arguments: calls.append(("cli", list(arguments))),
    )

    def request(cell: str, arm: str) -> dict[str, Any]:
        return {
            "operation": "sample",
            "arm": arm,
            "phase": "measured",
            "round_index": 0,
            "cell": cell,
            "subject": subject,
            "subject_root": os.fspath(subject_root.resolve()),
            "media_id": media_id,
            "media_root": os.fspath(media_root.resolve()),
            "media_identity": profiler._media_identity(media_root.resolve()),
            "view_set": manifest["view_sets"][0],
            "run_id": "7" * 32,
        }

    expected: dict[tuple[str, str], list[tuple[str, Any]]] = {
        ("compiler-cold", "legacy"): [],
        ("compiler-cold", "candidate"): [("provision", paths)],
        ("compiler-current", "legacy"): [
            (
                "cli",
                profiler._index_arguments(
                    request("compiler-current", "legacy"), paths, candidate=False
                ),
            )
        ],
        ("compiler-current", "candidate"): [
            ("provision", paths),
            (
                "cli",
                profiler._index_arguments(
                    request("compiler-current", "candidate"), paths, candidate=True
                ),
            ),
        ],
        ("runtime-cold", "legacy"): [
            (
                "cli",
                profiler._index_arguments(
                    request("runtime-cold", "legacy"), paths, candidate=False
                ),
            )
        ],
        ("runtime-cold", "candidate"): [
            ("provision", paths),
            (
                "cli",
                profiler._index_arguments(
                    request("runtime-cold", "candidate"), paths, candidate=True
                ),
            ),
        ],
        ("runtime-cold-query-only", "legacy"): [
            (
                "cli",
                profiler._index_arguments(
                    request("runtime-cold-query-only", "legacy"),
                    paths,
                    candidate=False,
                ),
            ),
            (
                "cli",
                [
                    "artifact",
                    "pack",
                    os.fspath(subject_root.resolve()),
                    "--output",
                    paths["direct_artifact"],
                    "--repository",
                    subject["repository_key"],
                    "--view",
                    "bm25",
                ],
            ),
        ],
        ("runtime-cold-query-only", "candidate"): [
            ("provision", paths),
            (
                "cli",
                profiler._index_arguments(
                    request("runtime-cold-query-only", "candidate"),
                    paths,
                    candidate=True,
                ),
            ),
        ],
    }

    for cell in profiler.CELLS:
        for arm in profiler.ARMS:
            calls.clear()
            sample_request = request(cell, arm)
            profiler._prepare_sample(sample_request, paths)
            assert calls == expected[(cell, arm)]

    assert profiler._runtime_arguments(
        request("runtime-cold", "legacy"), paths, generation=None
    ) == ["mcp", os.fspath(profiler._cache_manifest_path(subject_root))]
    assert profiler._runtime_arguments(
        request("runtime-cold-query-only", "legacy"), paths, generation=None
    ) == [
        "mcp",
        "--artifact",
        paths["direct_artifact"],
        "--repository",
        subject["repository_key"],
    ]
    retained_arguments = profiler._runtime_arguments(
        request("runtime-cold-query-only", "candidate"), paths, generation=1
    )
    assert retained_arguments == [
        "mcp",
        "--catalog",
        paths["catalog"],
        "--cas-root",
        paths["cas_root"],
        "--workspace-root",
        paths["workspace_root"],
        "--repository",
        subject["repository_key"],
        "--ref",
        "main",
        "--expected-generation",
        "1",
        "--output",
        paths["runtime_output"],
    ]
    assert "--repo" not in retained_arguments


def test_outer_sample_hides_prep_and_reports_the_inner_route_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    manifest, _receipt = profiler.load_subject_manifest(manifest_path)
    subject = manifest["subjects"][0]
    media_id, media_root = next(iter(media_roots.items()))
    request = {
        "operation": "sample",
        "arm": "legacy",
        "phase": "measured",
        "round_index": 0,
        "cell": "compiler-cold",
        "subject": subject,
        "subject_root": os.fspath(subject_roots[subject["id"]].resolve()),
        "media_id": media_id,
        "media_root": os.fspath(media_root.resolve()),
        "media_identity": profiler._media_identity(media_root.resolve()),
        "view_set": manifest["view_sets"][0],
        "run_id": "1" * 32,
    }
    inner_pid = os.getpid() + 10_000
    captured: dict[str, Any] = {}

    def run_inner(
        route_request: Mapping[str, Any],
        *,
        timeout_seconds: float,
        process_group: bool,
    ) -> tuple[dict[str, Any], float]:
        captured.update(deepcopy(dict(route_request)))
        assert timeout_seconds == profiler.DEFAULT_WORKER_TIMEOUT_SECONDS
        assert process_group is False
        result = _sample_receipt(request, process_id=inner_pid)["result"]
        return (
            {
                "operation": "route",
                "process_id": inner_pid,
                "metrics": {
                    "route_wall_seconds": 1.0,
                    "process_wall_seconds": 0.0,
                    "cpu_seconds": 0.5,
                    "peak_rss_bytes": 1024,
                    "io_read_bytes": 64,
                    "io_write_bytes": 32,
                    "payload_bytes": 128,
                    "payload_files": 2,
                },
                "result": result,
                "context_closed": True,
            },
            1.25,
        )

    monkeypatch.setattr(profiler, "_prepare_sample", lambda *_args: None)
    monkeypatch.setattr(profiler, "_run_worker_process", run_inner)

    receipt = profiler._sample_worker(request)

    assert captured["operation"] == "route"
    assert captured["request"] == request
    assert receipt["operation"] == "sample"
    assert receipt["process_id"] == inner_pid
    assert receipt["process_id"] != os.getpid()
    assert receipt["metrics"]["process_wall_seconds"] == 1.25
    assert receipt["safety"]["cleanup_complete"] is True
    assert not any(media_root.iterdir())


def test_query_only_direct_artifact_prep_cancellation_cleans_exact_sample_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    manifest, _receipt = profiler.load_subject_manifest(manifest_path)
    subject = manifest["subjects"][0]
    media_id, media_root = next(iter(media_roots.items()))
    request = {
        "operation": "sample",
        "arm": "legacy",
        "phase": "measured",
        "round_index": 0,
        "cell": "runtime-cold-query-only",
        "subject": subject,
        "subject_root": os.fspath(subject_roots[subject["id"]].resolve()),
        "media_id": media_id,
        "media_root": os.fspath(media_root.resolve()),
        "media_identity": profiler._media_identity(media_root.resolve()),
        "view_set": manifest["view_sets"][0],
        "run_id": "8" * 32,
    }
    primary = KeyboardInterrupt("synthetic direct-artifact interruption")
    observed_direct_artifact: Path | None = None

    def interrupted_prep(_request: Mapping[str, Any], paths: Mapping[str, str]) -> None:
        nonlocal observed_direct_artifact
        observed_direct_artifact = Path(paths["direct_artifact"])
        observed_direct_artifact.mkdir()
        (observed_direct_artifact / "partial").write_text(
            "partial",
            encoding="utf-8",
        )
        raise primary

    monkeypatch.setattr(profiler, "_prepare_sample", interrupted_prep)

    with pytest.raises(KeyboardInterrupt) as raised:
        profiler._sample_worker(request)

    assert raised.value is primary
    assert observed_direct_artifact is not None
    assert observed_direct_artifact == (
        profiler._owned_sample_root(request) / f"direct-artifact-{request['run_id']}"
    )
    assert not observed_direct_artifact.exists()
    assert not any(media_root.iterdir())


def test_inner_route_requires_the_exact_derived_authority_paths(
    tmp_path: Path,
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    manifest, _receipt = profiler.load_subject_manifest(manifest_path)
    subject = manifest["subjects"][0]
    media_id, media_root = next(iter(media_roots.items()))
    request = {
        "operation": "sample",
        "arm": "legacy",
        "phase": "measured",
        "round_index": 0,
        "cell": "compiler-cold",
        "subject": subject,
        "subject_root": os.fspath(subject_roots[subject["id"]].resolve()),
        "media_id": media_id,
        "media_root": os.fspath(media_root.resolve()),
        "media_identity": profiler._media_identity(media_root.resolve()),
        "view_set": manifest["view_sets"][0],
        "run_id": "2" * 32,
    }
    sample_root = profiler._owned_sample_root(request)
    sample_root.mkdir(mode=0o700)
    paths = profiler._sample_paths(sample_root, request["run_id"])

    normalized_request, normalized_paths = profiler._validate_route_config(
        {"operation": "route", "request": request, "paths": paths}
    )

    assert normalized_request == request
    assert normalized_paths == paths
    for path_name, replacement in (
        ("catalog", sample_root / "redirected.sqlite"),
        ("direct_artifact", sample_root / "redirected-artifact"),
    ):
        redirected = dict(paths)
        redirected[path_name] = os.fspath(replacement)
        with pytest.raises(ValueError, match="exact binding"):
            profiler._validate_route_config(
                {"operation": "route", "request": request, "paths": redirected}
            )


def test_runtime_context_authority_contract_is_cell_and_arm_specific() -> None:
    from codenib.artifacts.context import CONTEXT_ARTIFACT_SCHEMA

    subject = {
        "repository_key": "benchmark/fixture",
        "revision": "a" * 40,
    }
    legacy = {
        "loaded_views": ["bm25"],
        "errors": {},
        "artifact": None,
        "source_verified": True,
        "source_verification_scope": "content-bytes",
        "vector_loaded": False,
    }
    candidate = {
        **legacy,
        "artifact": {
            "verified": True,
            "schema": CONTEXT_ARTIFACT_SCHEMA,
            "repository": subject["repository_key"],
            "commit": subject["revision"],
            "views": ["bm25"],
        },
        "source_verified": False,
        "source_verification_scope": None,
    }

    legacy_authority = profiler._validate_runtime_context_state(
        legacy,
        request={"cell": "runtime-cold", "arm": "legacy", "subject": subject},
    )
    candidate_authority = profiler._validate_runtime_context_state(
        candidate,
        request={"cell": "runtime-cold", "arm": "candidate", "subject": subject},
    )
    direct_authority = profiler._validate_runtime_context_state(
        candidate,
        request={
            "cell": "runtime-cold-query-only",
            "arm": "legacy",
            "subject": subject,
        },
    )

    assert legacy_authority == {
        "context_kind": "manifest-live-source",
        "artifact": None,
        "source_verified": True,
        "source_verification_scope": "content-bytes",
    }
    assert direct_authority == candidate_authority
    assert candidate_authority["context_kind"] == "portable-artifact-query-only"
    assert candidate_authority["source_verified"] is False

    with pytest.raises(RuntimeError, match="exact per-cell contract"):
        profiler._validate_runtime_context_state(
            {**legacy, "source_verified": False},
            request={
                "cell": "runtime-cold",
                "arm": "legacy",
                "subject": subject,
            },
        )
    with pytest.raises(RuntimeError, match="exact per-cell contract"):
        profiler._validate_runtime_context_state(
            {**candidate, "source_verified": True},
            request={
                "cell": "runtime-cold-query-only",
                "arm": "legacy",
                "subject": subject,
            },
        )


def test_worker_process_accepts_only_stdout_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"operation": "route", "process_id": 4242}
    instances: list[Any] = []

    class FakeProcess:
        pid = 4242
        returncode = 0

        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            self.kwargs = kwargs
            instances.append(self)

        def communicate(
            self, *, input: str | None = None, timeout: float | None = None
        ) -> tuple[str, str]:
            assert json.loads(input or "{}") == {"operation": "route"}
            assert timeout == 1.0
            return json.dumps(payload, separators=(",", ":")), ""

    monkeypatch.setattr(profiler.subprocess, "Popen", FakeProcess)

    result, elapsed = profiler._run_worker_process(
        {"operation": "route"}, timeout_seconds=1.0, process_group=False
    )

    assert result == payload
    assert elapsed >= 0
    assert instances[0].kwargs["stdout"] is subprocess.PIPE
    assert instances[0].kwargs["stderr"] is subprocess.PIPE
    assert instances[0].kwargs["start_new_session"] is False


def test_worker_request_encoding_failure_never_launches_a_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = KeyboardInterrupt("synthetic encoding interruption")
    launches: list[object] = []

    def fail_encoding(_value: object) -> bytes:
        raise primary

    monkeypatch.setattr(profiler, "_canonical_json_bytes", fail_encoding)
    monkeypatch.setattr(
        profiler.subprocess,
        "Popen",
        lambda *_args, **_kwargs: launches.append(object()),
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        profiler._run_worker_process(
            {"operation": "sample"},
            timeout_seconds=1.0,
            process_group=True,
        )

    assert raised.value is primary
    assert launches == []


def test_worker_timeout_kills_the_whole_process_group_without_an_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, signal.Signals]] = []
    instances: list[Any] = []

    class TimedOutProcess:
        pid = 5151
        returncode = -signal.SIGKILL

        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.communicate_calls = 0
            self.completed = False
            instances.append(self)

        def communicate(
            self, *, input: str | None = None, timeout: float | None = None
        ) -> tuple[str, str]:
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired("synthetic-worker", timeout or 0)
            self.completed = True
            return "", "synthetic timeout"

    monkeypatch.setattr(profiler.subprocess, "Popen", TimedOutProcess)
    monkeypatch.setattr(
        profiler.os,
        "killpg",
        lambda process_id, sent_signal: signals.append((process_id, sent_signal)),
    )

    with pytest.raises(RuntimeError, match="worker timed out"):
        profiler._run_worker_process(
            {"operation": "sample"},
            timeout_seconds=0.01,
            process_group=True,
        )

    assert signals == [
        (5151, signal.SIGTERM),
        (5151, signal.SIGKILL),
    ]
    assert instances[0].kwargs["start_new_session"] is True
    assert instances[0].communicate_calls == 3
    assert instances[0].completed is True


def test_worker_interruption_reaps_the_process_group_and_preserves_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = KeyboardInterrupt("synthetic interruption")
    signals: list[tuple[int, signal.Signals]] = []
    instances: list[Any] = []

    class InterruptedProcess:
        pid = 6161
        returncode = -signal.SIGKILL

        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.communicate_calls = 0
            self.completed = False
            instances.append(self)

        def communicate(
            self, *, input: str | None = None, timeout: float | None = None
        ) -> tuple[str, str]:
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise primary
            self.completed = True
            return "", ""

    monkeypatch.setattr(profiler.subprocess, "Popen", InterruptedProcess)
    monkeypatch.setattr(
        profiler.os,
        "killpg",
        lambda process_id, sent_signal: signals.append((process_id, sent_signal)),
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        profiler._run_worker_process(
            {"operation": "sample"},
            timeout_seconds=1.0,
            process_group=True,
        )

    assert raised.value is primary
    assert signals == [
        (6161, signal.SIGTERM),
        (6161, signal.SIGKILL),
    ]
    assert instances[0].kwargs["start_new_session"] is True
    assert instances[0].communicate_calls == 3
    assert instances[0].completed is True


@pytest.mark.parametrize(
    ("expected_status", "expected_exit"),
    [
        ("complete", 0),
        ("failed", 1),
    ],
)
def test_cli_atomically_writes_complete_parity_red_and_operational_failure_reports(
    expected_status: str,
    expected_exit: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    output = tmp_path / "gate-report.json"
    output.write_text("old report must be replaced", encoding="utf-8")
    report = profiler._base_report(
        manifest_path=manifest_path,
        iterations=1,
        warmups=0,
        worker_timeout_seconds=1.0,
        subject_roots=subject_roots,
        media_roots=media_roots,
    )
    if expected_status == "complete":
        report.update({"status": "complete", "passed": False, "failure": None})
    else:
        profiler._failure(report, "measurement", RuntimeError("synthetic failure"))
    assert report["promotion_eligible"] is False
    monkeypatch.setattr(
        profiler,
        "profile_retained_storage_gate",
        lambda **_kwargs: deepcopy(report),
    )
    arguments = [
        "--subject-manifest",
        os.fspath(manifest_path),
        "--iterations",
        "1",
        "--warmups",
        "0",
        "--worker-timeout-seconds",
        "1",
        "--output",
        os.fspath(output),
    ]
    for identifier, root in subject_roots.items():
        arguments.extend(("--subject-root", f"{identifier}={root}"))
    for identifier, root in media_roots.items():
        arguments.extend(("--media-root", f"{identifier}={root}"))

    exit_code = profiler.main(arguments)

    assert exit_code == expected_exit
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written == report
    assert written["status"] == expected_status
    assert written["promotion_eligible"] is False
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
