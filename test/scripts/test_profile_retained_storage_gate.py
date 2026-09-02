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
import sys
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


def _bm25_plan_identity(request: Mapping[str, Any]) -> dict[str, Any]:
    subject = request["subject"]
    return {
        "commit": subject["revision"],
        "source_fingerprint": _SOURCE_V2,
        "source_selection_digest": _json_digest(subject["source_selection"]),
        "semantic_sha256": _SHA_B,
        "languages": list(subject["languages"]),
        "file_count": 1,
    }


def _public_manifest_evidence(request: Mapping[str, Any]) -> dict[str, Any] | None:
    if request["cell"] in {"compiler-cold", "compiler-current"}:
        return None
    portable = request["arm"] == "candidate" or (
        request["arm"] == "legacy" and request["cell"] == "runtime-cold-query-only"
    )
    return {
        "raw_sha256": _SHA_D if portable else _SHA_C,
        "parity_sha256": _SHA_D if portable else _SHA_C,
        "parity_schema": "codenib.retained-storage-public-manifest-parity.v1",
    }


def _source_read_evidence(request: Mapping[str, Any]) -> dict[str, Any]:
    cell = request["cell"]
    if cell in {"compiler-cold", "compiler-current"}:
        return {
            "status": "not-exercised",
            "payload_sha256": None,
            "content_sha256": None,
            "error_sha256": None,
            "count": 0,
        }
    source_enabled = (
        request["arm"] == "legacy" and cell != "runtime-cold-query-only"
    ) or (request["arm"] == "candidate" and cell == "runtime-cold-source-bound")
    if source_enabled:
        return {
            "status": "verified",
            "payload_sha256": _SHA_C,
            "content_sha256": _SHA_D,
            "error_sha256": None,
            "count": 1,
        }
    return {
        "status": "source-disabled",
        "payload_sha256": None,
        "content_sha256": None,
        "error_sha256": _SHA_C,
        "count": 1,
    }


def _runtime_identity(request: Mapping[str, Any]) -> dict[str, Any]:
    runtime = profiler._expected_runtime_identity(request)  # noqa: SLF001
    capabilities = runtime["capabilities"]
    if capabilities is None:
        return runtime
    portable = request["arm"] == "candidate" or (
        request["arm"] == "legacy" and request["cell"] == "runtime-cold-query-only"
    )
    capabilities["lsp_provider"] = {
        "provider": "codenib_static_index",
        "backend": "unavailable",
        "status": "unavailable",
        "index_snapshot": None,
        "fallback_reason": (
            "portable_artifact_uses_persisted_graph"
            if portable
            else "repository_source_policy_requires_persisted_graph"
        ),
        "capabilities": {
            "definition": False,
            "references": False,
            "route": False,
        },
    }
    return runtime


def _sample_receipt(request: Mapping[str, Any], *, process_id: int) -> dict[str, Any]:
    queries_sha256 = (
        _SHA_B
        if request["cell"] == "runtime-cold" and request["arm"] == "candidate"
        else _SHA_A
    )
    result = {
        "bm25_plan": _bm25_plan_identity(request),
        "public_manifest": _public_manifest_evidence(request),
        "view": {
            "documents_sha256": _SHA_C,
            "metadata_sha256": _SHA_D,
            "payload_bytes": 128,
            "payload_files": 2,
        },
        "queries": {
            "sha256": queries_sha256,
            "count": len(request["subject"]["queries"]),
            "nonempty": True,
        },
        "source_read": _source_read_evidence(request),
        "runtime": _runtime_identity(request),
    }
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
            "runtime-cold-source-bound": None,
        }[request["cell"]]
        result["snapshot"] = {
            "snapshot_id": _SNAPSHOT_ID,
            "ref_name": "main",
            "generation": 1,
            "changed": changed,
        }
    parity = profiler._parity_identities_from_result(result)  # noqa: SLF001
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
        "parity_identities": parity,
        "result": result,
        "safety": {
            "subject_unchanged": True,
            "sample_root_fresh": True,
            "cleanup_complete": True,
            "storage_closed": True,
            "context_closed": True,
            "source_closed": True,
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
    iterations: int = 1,
    warmups: int = 0,
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
        iterations=iterations,
        warmups=warmups,
        sample_runner=sample_runner,
        process_ids=process_ids,
    )
    expected_samples = (iterations + warmups) * len(profiler.ARMS)
    assert len(process_ids) == expected_samples
    assert len(set(process_ids)) == expected_samples
    return result


def test_public_v4_contract_and_deterministic_paired_order() -> None:
    assert profiler.ARMS == ("legacy", "candidate")
    assert profiler.CELLS == (
        "compiler-cold",
        "compiler-current",
        "runtime-cold",
        "runtime-cold-query-only",
        "runtime-cold-source-bound",
    )
    assert profiler.TRACKS == {
        "compiler": {
            "cells": ("compiler-cold", "compiler-current"),
            "projection": "compiler",
        },
        "query-only-runtime": {
            "cells": ("runtime-cold-query-only",),
            "projection": "full-runtime",
        },
        "manifest-runtime-compatibility": {
            "cells": ("runtime-cold",),
            "projection": "full-runtime",
        },
        "source-bound-runtime": {
            "cells": ("runtime-cold-source-bound",),
            "projection": "content-authority",
        },
        "source-bound-manifest-compatibility": {
            "cells": ("runtime-cold-source-bound",),
            "projection": "full-runtime",
        },
    }
    assert profiler.PARITY_PROJECTIONS == (
        "compiler",
        "content-authority",
        "full-runtime",
    )
    assert profiler.CELL_PRIMARY_PROJECTIONS == {
        "compiler-cold": "compiler",
        "compiler-current": "compiler",
        "runtime-cold": "full-runtime",
        "runtime-cold-query-only": "full-runtime",
        "runtime-cold-source-bound": "content-authority",
    }
    expected_compiler_delivery = {
        "legacy": {
            "route": "compiler-cache-index",
            "context_provenance": "compiler-cache-manifest",
        },
        "candidate": {
            "route": "compiler-cache-index-and-retained-publication",
            "context_provenance": "compiler-cache-manifest",
        },
    }
    for cell in ("compiler-cold", "compiler-current"):
        assert {
            arm: {
                "route": contract["route"],
                "context_provenance": contract["context_provenance"],
            }
            for arm, contract in profiler.CELL_ROUTE_CONTRACTS[cell].items()
        } == expected_compiler_delivery
        for arm, delivery in expected_compiler_delivery.items():
            assert _runtime_identity({"cell": cell, "arm": arm})["delivery"] == delivery
    assert profiler.CANONICAL_SAMPLE_COUNT == 1440
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
    assert sentinel_pair["parity"] == {
        "compiler": False,
        "content-authority": False,
        "full-runtime": False,
    }
    assert sentinel["primary_projection"] == "full-runtime"
    assert sentinel["parity"]["full-runtime"]["every_pair_equal"] is False
    assert sentinel["parity"]["full-runtime"]["stable_within_arm"] == {
        "legacy": True,
        "candidate": True,
    }
    assert sentinel["parity"]["full-runtime"]["passed"] is False
    assert sentinel["parity"]["full-runtime"]["identity_sha256"].keys() == {
        "legacy",
        "candidate",
    }
    assert (
        legacy_sentinel["result"]["queries"]["sha256"]
        != candidate_sentinel["result"]["queries"]["sha256"]
    )
    assert legacy_sentinel["result"]["runtime"] == {
        "delivery": {
            "route": "manifest-path",
            "context_provenance": "ordinary-manifest",
        },
        "artifact": None,
        "source_authority": {
            "kind": "content-bytes-v2",
            "verified": True,
            "verification_scope": "content-bytes",
        },
        "capabilities": _runtime_identity(
            {
                "cell": "runtime-cold",
                "arm": "legacy",
                "subject": subject,
            }
        )["capabilities"],
    }
    assert candidate_sentinel["result"]["runtime"] == _runtime_identity(
        {
            "cell": "runtime-cold",
            "arm": "candidate",
            "subject": subject,
        }
    )

    query_only_pair = query_only["runs"]["measured"][0]
    assert query_only_pair["parity"] == {
        "compiler": True,
        "content-authority": True,
        "full-runtime": True,
    }
    assert query_only["parity"]["full-runtime"]["passed"] is True
    assert (
        query_only_pair["samples"][0]["parity_identities"]
        == query_only_pair["samples"][1]["parity_identities"]
    )


def test_source_bound_runtime_has_content_authority_parity_not_manifest_parity(
    tmp_path: Path,
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)

    source_bound = _run_fake_cell(
        manifest_path=manifest_path,
        subject_roots=subject_roots,
        media_roots=media_roots,
        cell="runtime-cold-source-bound",
        sample_runner=_fake_sample_runner(),
    )

    pair = source_bound["runs"]["measured"][0]
    legacy, candidate = pair["samples"]
    assert source_bound["primary_projection"] == "content-authority"
    assert pair["parity"] == {
        "compiler": True,
        "content-authority": True,
        "full-runtime": False,
    }
    assert source_bound["parity"]["content-authority"] == {
        "every_pair_equal": True,
        "stable_within_arm": {"legacy": True, "candidate": True},
        "identity_sha256": {
            "legacy": [_json_digest(legacy["parity_identities"]["content-authority"])],
            "candidate": [
                _json_digest(candidate["parity_identities"]["content-authority"])
            ],
        },
        "passed": True,
    }
    assert source_bound["parity"]["full-runtime"]["every_pair_equal"] is False
    assert source_bound["parity"]["full-runtime"]["stable_within_arm"] == {
        "legacy": True,
        "candidate": True,
    }
    assert source_bound["passed"] is True
    assert legacy["result"]["source_read"] == candidate["result"]["source_read"]
    assert (
        legacy["result"]["runtime"]["source_authority"]
        == candidate["result"]["runtime"]["source_authority"]
    )
    assert legacy["result"]["public_manifest"] != candidate["result"]["public_manifest"]
    assert legacy["result"]["runtime"]["delivery"] == {
        "route": "manifest-path",
        "context_provenance": "ordinary-manifest",
    }
    assert candidate["result"]["runtime"]["delivery"] == {
        "route": "retained-materialized-artifact",
        "context_provenance": "portable-context-artifact",
    }


def test_content_authority_projection_keeps_only_source_capability_evidence(
    tmp_path: Path,
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    manifest, _receipt = profiler.load_subject_manifest(manifest_path)
    subject = manifest["subjects"][0]
    media_id, media_root = next(iter(media_roots.items()))

    def result(arm: str) -> dict[str, Any]:
        request = {
            "operation": "sample",
            "arm": arm,
            "phase": "measured",
            "round_index": 0,
            "cell": "runtime-cold-source-bound",
            "subject": subject,
            "subject_root": os.fspath(subject_roots[subject["id"]]),
            "media_id": media_id,
            "media_root": os.fspath(media_root),
            "media_identity": profiler._media_identity(media_root),
            "view_set": manifest["view_sets"][0],
            "run_id": "1" * 32,
        }
        return _sample_receipt(request, process_id=1234)["result"]

    legacy = profiler._parity_identities_from_result(result("legacy"))  # noqa: SLF001
    candidate_result = result("candidate")
    candidate = profiler._parity_identities_from_result(  # noqa: SLF001
        candidate_result
    )
    assert set(candidate["content-authority"]) == {
        "compiler",
        "source_read",
        "source_authority",
        "source_capabilities",
    }
    assert (
        legacy["content-authority"]["source_capabilities"]
        == candidate["content-authority"]["source_capabilities"]
        == {
            "loaded_views": ["bm25"],
            "read_source": True,
            "commit_verified": False,
            "checkout_state": "not-attested",
        }
    )
    assert legacy["content-authority"] == candidate["content-authority"]
    compiler_result = deepcopy(candidate_result)
    compiler_result["runtime"] = _runtime_identity(
        {"cell": "compiler-cold", "arm": "legacy", "subject": subject}
    )
    compiler_result["source_read"] = {
        "status": "not-exercised",
        "payload_sha256": None,
        "content_sha256": None,
        "error_sha256": None,
        "count": 0,
    }
    assert (
        profiler._parity_identities_from_result(compiler_result)[  # noqa: SLF001
            "content-authority"
        ]["source_capabilities"]
        is None
    )

    for field, replacement in (
        ("loaded_views", []),
        ("read_source", False),
        ("commit_verified", True),
        ("checkout_state", "attested"),
    ):
        changed = deepcopy(candidate_result)
        changed["runtime"]["capabilities"][field] = replacement
        projection = profiler._parity_identities_from_result(changed)  # noqa: SLF001
        assert projection["content-authority"] != candidate["content-authority"]
        assert projection["full-runtime"] != candidate["full-runtime"]

    excluded_changes: list[tuple[Callable[[dict[str, Any]], None], bool]] = [
        (
            lambda changed: changed["runtime"]["capabilities"].__setitem__(
                "native_vector_authorized", True
            ),
            True,
        ),
        (
            lambda changed: changed["runtime"]["capabilities"].__setitem__(
                "native_lsp_allowed", True
            ),
            True,
        ),
        (
            lambda changed: changed["runtime"]["capabilities"][
                "lsp_provider"
            ].__setitem__("status", "changed"),
            True,
        ),
        (
            lambda changed: changed["runtime"]["delivery"].__setitem__(
                "route", "treatment-route"
            ),
            False,
        ),
        (
            lambda changed: changed["runtime"]["delivery"].__setitem__(
                "context_provenance", "changed-provenance"
            ),
            True,
        ),
        (
            lambda changed: changed["runtime"]["artifact"].__setitem__(
                "commit", "b" * 40
            ),
            True,
        ),
    ]
    for mutate, full_changes in excluded_changes:
        changed = deepcopy(candidate_result)
        mutate(changed)
        projection = profiler._parity_identities_from_result(changed)  # noqa: SLF001
        assert projection["content-authority"] == candidate["content-authority"]
        assert (projection["full-runtime"] != candidate["full-runtime"]) is full_changes


def test_each_projection_reports_pair_equality_and_per_arm_stability(
    tmp_path: Path,
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)

    def vary_only_full_runtime(
        receipt: dict[str, Any], request: Mapping[str, Any], call: int
    ) -> None:
        if request["arm"] == "candidate" and call == 3:
            receipt["result"]["source_read"]["error_sha256"] = _SHA_D
            receipt["parity_identities"] = (
                profiler._parity_identities_from_result(  # noqa: SLF001
                    receipt["result"]
                )
            )

    query_only = _run_fake_cell(
        manifest_path=manifest_path,
        subject_roots=subject_roots,
        media_roots=media_roots,
        cell="runtime-cold-query-only",
        sample_runner=_fake_sample_runner(vary_only_full_runtime),
        iterations=2,
    )

    for projection in ("compiler", "content-authority"):
        assert query_only["parity"][projection] == {
            "every_pair_equal": True,
            "stable_within_arm": {"legacy": True, "candidate": True},
            "identity_sha256": {
                "legacy": query_only["parity"][projection]["identity_sha256"]["legacy"],
                "candidate": query_only["parity"][projection]["identity_sha256"][
                    "candidate"
                ],
            },
            "passed": True,
        }
        assert all(
            len(query_only["parity"][projection]["identity_sha256"][arm]) == 1
            for arm in profiler.ARMS
        )
    assert query_only["parity"]["full-runtime"]["every_pair_equal"] is False
    assert query_only["parity"]["full-runtime"]["stable_within_arm"] == {
        "legacy": True,
        "candidate": False,
    }
    assert {
        arm: len(query_only["parity"]["full-runtime"]["identity_sha256"][arm])
        for arm in profiler.ARMS
    } == {"legacy": 1, "candidate": 2}
    assert query_only["parity"]["full-runtime"]["passed"] is False


def test_runtime_manifest_authority_mismatch_blocks_equal_query_digest(
    tmp_path: Path,
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)

    def align_query_digest(
        receipt: dict[str, Any], request: Mapping[str, Any], _call: int
    ) -> None:
        if request["cell"] == "runtime-cold" and request["arm"] == "candidate":
            receipt["result"]["queries"]["sha256"] = _SHA_A
            receipt["parity_identities"] = (
                profiler._parity_identities_from_result(  # noqa: SLF001
                    receipt["result"]
                )
            )

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
    assert legacy["result"]["runtime"] != candidate["result"]["runtime"]
    assert pair["parity"]["compiler"] is True
    assert pair["parity"]["content-authority"] is False
    assert pair["parity"]["full-runtime"] is False
    assert sentinel["parity"]["full-runtime"]["passed"] is False


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
        "source-bound-runtime": True,
        "source-bound-manifest-compatibility": False,
    }
    assert all(track["measurement_complete"] for track in baseline.values())
    assert baseline["source-bound-runtime"] == {
        "cells": ["runtime-cold-source-bound"],
        "projection": "content-authority",
        "measurement_complete": True,
        "parity_passed": True,
        "safety_passed": True,
        "scope_complete": True,
        "decision": "passed",
        "passed": True,
        "policy_status": "unratified",
        "promotion_eligible": False,
    }
    assert baseline["source-bound-manifest-compatibility"] == {
        "cells": ["runtime-cold-source-bound"],
        "projection": "full-runtime",
        "measurement_complete": True,
        "parity_passed": False,
        "safety_passed": True,
        "scope_complete": False,
        "decision": "blocked",
        "passed": False,
        "policy_status": "unratified",
        "promotion_eligible": False,
    }

    parity_laundered = deepcopy(cells)
    parity_laundered["runtime-cold"]["parity"]["full-runtime"]["passed"] = True
    parity_laundered["runtime-cold-source-bound"]["parity"]["full-runtime"][
        "passed"
    ] = True
    laundered_tracks = profiler._aggregate_tracks(
        parity_laundered,
        expected_instances_per_cell=1,
        iterations=1,
        warmups=0,
    )
    for track in (
        "manifest-runtime-compatibility",
        "source-bound-manifest-compatibility",
    ):
        assert laundered_tracks[track]["parity_passed"] is True
        assert laundered_tracks[track]["scope_complete"] is False
        assert laundered_tracks[track]["decision"] == "blocked"
        assert laundered_tracks[track]["passed"] is False
    assert all(track["passed"] for track in laundered_tracks.values()) is False

    query_parity_red = deepcopy(cells)
    query_parity_red["runtime-cold-query-only"]["parity"]["full-runtime"][
        "passed"
    ] = False
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
    assert query_tracks["source-bound-runtime"] == baseline["source-bound-runtime"]
    assert (
        query_tracks["source-bound-manifest-compatibility"]
        == baseline["source-bound-manifest-compatibility"]
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
        "projection": "full-runtime",
        "measurement_complete": False,
        "parity_passed": False,
        "safety_passed": False,
        "scope_complete": False,
        "decision": "blocked",
        "passed": False,
        "policy_status": "unratified",
        "promotion_eligible": False,
    }
    assert (
        incomplete_tracks["manifest-runtime-compatibility"]
        == baseline["manifest-runtime-compatibility"]
    )
    assert incomplete_tracks["source-bound-runtime"] == baseline["source-bound-runtime"]


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


def test_public_manifest_normalizer_masks_only_times_and_isolated_paths(
    tmp_path: Path,
) -> None:
    subject_root = tmp_path / "subject"
    ordinary = {
        "schema_version": 3,
        "repo": {
            "path": os.fspath(subject_root),
            "commit": "a" * 40,
            "source_fingerprint": _SOURCE_V2,
        },
        "compiled_at": "2026-08-24T10:11:12Z",
        "compiled_at_epoch": 1234.5,
        "indexes": {
            "bm25": {
                "path": os.fspath(tmp_path / "isolated-bm25"),
                "built_at": "2026-08-24T10:11:13Z",
                "built_at_epoch": 1235.5,
                "metadata": {
                    "build_duration_seconds": 9.25,
                    "content_authority": "must-survive",
                },
            }
        },
        "runtime": {
            "source_read": {
                "verified": True,
                "verification_scope": "content-bytes",
            },
            "lsp_provider": {"provider": "codenib_static_index"},
        },
        "artifact": None,
    }
    request = {
        "cell": "runtime-cold-source-bound",
        "arm": "legacy",
        "subject_root": os.fspath(subject_root),
    }

    normalized = profiler._normalized_public_manifest_payload(  # noqa: SLF001
        ordinary,
        request=request,
    )

    expected = deepcopy(ordinary)
    expected["repo"]["path"] = "<authenticated-benchmark-subject>"
    expected["compiled_at"] = "<measurement-metadata>"
    expected["compiled_at_epoch"] = 0
    expected["indexes"]["bm25"]["path"] = "<isolated-bm25-view>"
    expected["indexes"]["bm25"]["built_at"] = "<measurement-metadata>"
    expected["indexes"]["bm25"]["built_at_epoch"] = 0
    expected["indexes"]["bm25"]["metadata"][
        "build_duration_seconds"
    ] = "<measurement-metadata>"
    assert normalized == expected
    assert ordinary["compiled_at"] == "2026-08-24T10:11:12Z"

    authority_changed = deepcopy(ordinary)
    authority_changed["runtime"]["source_read"]["verified"] = False
    assert _json_digest(
        profiler._normalized_public_manifest_payload(  # noqa: SLF001
            ordinary,
            request=request,
        )
    ) != _json_digest(
        profiler._normalized_public_manifest_payload(  # noqa: SLF001
            authority_changed,
            request=request,
        )
    )

    portable = deepcopy(ordinary)
    portable["repo"]["path"] = "source"
    portable["indexes"]["bm25"]["path"] = "views/bm25"
    portable["artifact"] = {"verified": True, "schema": "portable"}
    portable_request = {
        **request,
        "cell": "runtime-cold-query-only",
        "arm": "candidate",
    }
    portable_normalized = profiler._normalized_public_manifest_payload(  # noqa: SLF001
        portable,
        request=portable_request,
    )
    assert portable_normalized["repo"]["path"] == "source"
    assert portable_normalized["indexes"]["bm25"]["path"] == "views/bm25"
    assert portable_normalized["artifact"] == portable["artifact"]
    assert (
        portable_normalized["runtime"]["lsp_provider"]
        == portable["runtime"]["lsp_provider"]
    )
    assert _json_digest(normalized) != _json_digest(portable_normalized)


def test_public_manifest_receipt_is_exactly_bound_to_the_active_context(
    tmp_path: Path,
) -> None:
    subject_root = tmp_path / "subject"

    def contract(
        *, portable: bool
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        artifact = (
            {
                "verified": True,
                "schema": "codenib.context-artifact.v1",
                "repository": "benchmark/fixture",
                "commit": "a" * 40,
                "views": ["bm25"],
            }
            if portable
            else None
        )
        context_manifest = {
            "schema_version": 3,
            "repo": {
                "path": os.fspath(subject_root),
                "commit": "a" * 40,
                "source_fingerprint": _SOURCE_V2,
            },
            "compiled_at": "2026-08-24T10:11:12Z",
            "compiled_at_epoch": 1234.5,
            "indexes": {
                "bm25": {
                    "path": (os.fspath(tmp_path / "isolated-bm25")),
                    "built_at": "2026-08-24T10:11:13Z",
                    "built_at_epoch": 1235.5,
                    "metadata": {"build_duration_seconds": 0.25},
                }
            },
        }
        lsp_provider = {"provider": "codenib_static_index", "status": "unavailable"}
        context_state = {
            "loaded_views": ["bm25"],
            "errors": {},
            "artifact": artifact,
            "source_verified": True,
            "source_error": None,
            "source_verification_scope": "content-bytes",
            "lsp_provider": lsp_provider,
            "commit_verified": False,
        }
        public = {
            **deepcopy(context_manifest),
            "runtime": {
                "loaded_views": ["bm25"],
                "view_errors": {},
                "tool_surface": "full",
                "explore_session": {
                    "calls": 0,
                    "ranges": 0,
                    "max_ranges": 256,
                    "evictions": 0,
                },
                "source_read": {
                    "verified": True,
                    "error": None,
                    "verification_scope": "content-bytes",
                    "commit_verified": False,
                    "checkout_state": "not-attested",
                },
                "lsp_provider": deepcopy(lsp_provider),
            },
        }
        if portable:
            public["artifact"] = deepcopy(artifact)
        request = {
            "cell": "runtime-cold-source-bound",
            "arm": "candidate" if portable else "legacy",
            "subject_root": os.fspath(subject_root),
        }
        return request, context_manifest, context_state, public

    for portable in (False, True):
        request, context_manifest, context_state, public = contract(portable=portable)
        assert (
            profiler._validated_public_manifest_payload(  # noqa: SLF001
                public,
                request=request,
                context_manifest=context_manifest,
                context_state=context_state,
                tool_surface="full",
            )
            == public
        )
        assert profiler._public_manifest_evidence(  # noqa: SLF001
            public,
            request=request,
            context_manifest=context_manifest,
            context_state=context_state,
            tool_surface="full",
        ) == {
            "raw_sha256": _json_digest(public),
            "parity_sha256": _json_digest(
                profiler._normalized_public_manifest_payload(  # noqa: SLF001
                    public,
                    request=request,
                )
            ),
            "parity_schema": "codenib.retained-storage-public-manifest-parity.v1",
        }

    request, context_manifest, context_state, public = contract(portable=False)
    invalid_ordinary: list[dict[str, Any]] = []
    missing_runtime = deepcopy(public)
    missing_runtime["runtime"].pop("loaded_views")
    invalid_ordinary.append(missing_runtime)
    extra_runtime = deepcopy(public)
    extra_runtime["runtime"]["unexpected"] = True
    invalid_ordinary.append(extra_runtime)
    manifest_drift = deepcopy(public)
    manifest_drift["repo"]["commit"] = "b" * 40
    invalid_ordinary.append(manifest_drift)
    extra_artifact = deepcopy(public)
    extra_artifact["artifact"] = {"verified": True}
    invalid_ordinary.append(extra_artifact)
    source_drift = deepcopy(public)
    source_drift["runtime"]["source_read"]["verified"] = False
    invalid_ordinary.append(source_drift)
    lsp_drift = deepcopy(public)
    lsp_drift["runtime"]["lsp_provider"]["status"] = "ready"
    invalid_ordinary.append(lsp_drift)
    view_drift = deepcopy(public)
    view_drift["runtime"]["loaded_views"] = []
    invalid_ordinary.append(view_drift)
    view_error_drift = deepcopy(public)
    view_error_drift["runtime"]["view_errors"] = {"bm25": "broken"}
    invalid_ordinary.append(view_error_drift)
    for invalid in invalid_ordinary:
        with pytest.raises((RuntimeError, ValueError)):
            profiler._validated_public_manifest_payload(  # noqa: SLF001
                invalid,
                request=request,
                context_manifest=context_manifest,
                context_state=context_state,
                tool_surface="full",
            )

    request, context_manifest, context_state, public = contract(portable=True)
    missing_artifact = deepcopy(public)
    missing_artifact.pop("artifact")
    artifact_drift = deepcopy(public)
    artifact_drift["artifact"]["commit"] = "b" * 40
    for invalid in (missing_artifact, artifact_drift):
        with pytest.raises((RuntimeError, ValueError)):
            profiler._validated_public_manifest_payload(  # noqa: SLF001
                invalid,
                request=request,
                context_manifest=context_manifest,
                context_state=context_state,
                tool_surface="full",
            )


def test_public_manifest_normalizer_masks_authority_differences_in_no_field(
    tmp_path: Path,
) -> None:
    """Changing source, artifact, or LSP evidence must change parity."""

    subject_root = tmp_path / "subject"
    ordinary = {
        "repo": {"path": os.fspath(subject_root)},
        "compiled_at": "first",
        "compiled_at_epoch": 1,
        "indexes": {
            "bm25": {
                "path": os.fspath(tmp_path / "view"),
                "built_at": "first",
                "built_at_epoch": 1,
                "metadata": {"build_duration_seconds": 1},
            }
        },
        "runtime": {
            "source_read": {"verified": True},
            "lsp_provider": {"provider": "persisted"},
        },
        "artifact": None,
    }
    request = {
        "cell": "runtime-cold-source-bound",
        "arm": "legacy",
        "subject_root": os.fspath(subject_root),
    }
    baseline = profiler._normalized_public_manifest_payload(  # noqa: SLF001
        ordinary,
        request=request,
    )
    for path, replacement in (
        (("runtime", "source_read", "verified"), False),
        (("runtime", "lsp_provider", "provider"), "native"),
        (("artifact",), {"verified": True}),
    ):
        changed = deepcopy(ordinary)
        target = changed
        for component in path[:-1]:
            target = target[component]
        target[path[-1]] = replacement
        assert (
            profiler._normalized_public_manifest_payload(  # noqa: SLF001
                changed,
                request=request,
            )
            != baseline
        )


def test_public_manifest_normalizer_rejects_malformed_measurement_metadata(
    tmp_path: Path,
) -> None:
    subject_root = tmp_path / "subject"
    request = {
        "cell": "runtime-cold-source-bound",
        "arm": "legacy",
        "subject_root": os.fspath(subject_root),
    }

    def payload() -> dict[str, Any]:
        return {
            "repo": {"path": os.fspath(subject_root)},
            "compiled_at": "2026-08-24T10:11:12Z",
            "compiled_at_epoch": 1234.5,
            "indexes": {
                "bm25": {
                    "path": os.fspath(tmp_path / "isolated-bm25"),
                    "built_at": "2026-08-24T10:11:13Z",
                    "built_at_epoch": 1235.5,
                    "metadata": {"build_duration_seconds": 0.25},
                }
            },
        }

    for container, field in (
        ((), "compiled_at"),
        ((), "compiled_at_epoch"),
        (("indexes", "bm25"), "built_at"),
        (("indexes", "bm25"), "built_at_epoch"),
    ):
        malformed = payload()
        target = malformed
        for component in container:
            target = target[component]
        target.pop(field)
        with pytest.raises(RuntimeError):
            profiler._normalized_public_manifest_payload(  # noqa: SLF001
                malformed,
                request=request,
            )

    for container, field in (
        ((), "compiled_at"),
        (("indexes", "bm25"), "built_at"),
    ):
        for invalid in (None, True, 1, 1.5):
            malformed = payload()
            target = malformed
            for component in container:
                target = target[component]
            target[field] = invalid
            with pytest.raises((RuntimeError, ValueError)):
                profiler._normalized_public_manifest_payload(  # noqa: SLF001
                    malformed,
                    request=request,
                )

    for container, field in (
        ((), "compiled_at_epoch"),
        (("indexes", "bm25"), "built_at_epoch"),
    ):
        for invalid in (None, True, "1", math.nan, math.inf, -math.inf, -1):
            malformed = payload()
            target = malformed
            for component in container:
                target = target[component]
            target[field] = invalid
            with pytest.raises((RuntimeError, ValueError)):
                profiler._normalized_public_manifest_payload(  # noqa: SLF001
                    malformed,
                    request=request,
                )

    for invalid in (None, True, "1", math.nan, math.inf, -math.inf, -1):
        malformed = payload()
        malformed["indexes"]["bm25"]["metadata"]["build_duration_seconds"] = invalid
        with pytest.raises((RuntimeError, ValueError)):
            profiler._normalized_public_manifest_payload(  # noqa: SLF001
                malformed,
                request=request,
            )

    first = payload()
    second = payload()
    second["compiled_at"] = "2027-01-02T03:04:05Z"
    second["compiled_at_epoch"] = 9999
    second["indexes"]["bm25"]["built_at"] = "2027-01-02T03:04:06Z"
    second["indexes"]["bm25"]["built_at_epoch"] = 10_000
    second["indexes"]["bm25"]["metadata"]["build_duration_seconds"] = 4.5
    assert profiler._normalized_public_manifest_payload(  # noqa: SLF001
        first,
        request=request,
    ) == profiler._normalized_public_manifest_payload(  # noqa: SLF001
        second,
        request=request,
    )


def test_public_server_source_probe_separates_payload_content_and_refusal_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.mcp import server as server_module

    class Manifest:
        repo_path = "benchmark/fixture"
        commit = "a" * 40
        source_fingerprint = _SOURCE_V2

    class SourceContext:
        source_verified = True
        source_error = None
        artifact = {"repository": "benchmark/fixture"}
        manifest = Manifest()

        def read_source_bytes(self, path: str, *, max_bytes: int) -> bytes:
            assert path == "package.py"
            assert max_bytes == 64 * 1024 * 1024
            return b"first line\nsecond line\n"

    context = SourceContext()
    monkeypatch.setattr(server_module, "_ctx", context)
    query_payload = [
        {
            "query": {"text": "package"},
            "results": [{"file": "package.py", "start_line": 1}],
        }
    ]
    request = {
        "cell": "runtime-cold-source-bound",
        "arm": "candidate",
        "subject": {
            "repository_key": "benchmark/fixture",
            "revision": "a" * 40,
        },
        "subject_root": "/unused-benchmark-subject",
    }

    verified = profiler._source_read_evidence(  # noqa: SLF001
        server_module,
        context,
        query_payload,
        request=request,
    )
    content = {
        "content": "first line\n",
        "content_projection": {
            "truncated": False,
            "original_chars": 11,
            "returned_chars": 11,
            "strategy": "complete",
        },
        "end_line": 1,
        "file": "package.py",
        "start_line": 1,
    }
    payload = {
        **content,
        "source": {
            "repository": "benchmark/fixture",
            "commit": "a" * 40,
            "source_fingerprint": _SOURCE_V2,
            "verified": True,
            "verification_scope": "content-bytes",
            "commit_verified": False,
            "checkout_state": "not-attested",
        },
    }
    assert verified == {
        "status": "verified",
        "payload_sha256": _json_digest(payload),
        "content_sha256": _json_digest(content),
        "error_sha256": None,
        "count": 1,
    }
    assert verified["payload_sha256"] != verified["content_sha256"]

    class DisabledContext(SourceContext):
        source_verified = False
        source_error = "retained route has no repository source"

        def read_source_bytes(self, path: str, *, max_bytes: int) -> bytes:
            raise AssertionError("disabled public source route reached storage")

    disabled_context = DisabledContext()
    monkeypatch.setattr(server_module, "_ctx", disabled_context)
    disabled_request = {
        **request,
        "cell": "runtime-cold-query-only",
    }
    message = "source reads are unavailable: retained route has no repository source"
    assert profiler._source_read_evidence(  # noqa: SLF001
        server_module,
        disabled_context,
        query_payload,
        request=disabled_request,
    ) == {
        "status": "source-disabled",
        "payload_sha256": None,
        "content_sha256": None,
        "error_sha256": _json_digest(
            {"error_type": "RuntimeError", "message": message}
        ),
        "count": 1,
    }


def test_public_source_payload_validator_rejects_any_shape_or_authority_drift(
    tmp_path: Path,
) -> None:
    subject_root = tmp_path / "subject"
    subject = {
        "repository_key": "benchmark/fixture",
        "revision": "a" * 40,
    }
    portable_request = {
        "cell": "runtime-cold-source-bound",
        "arm": "candidate",
        "subject": subject,
        "subject_root": os.fspath(subject_root),
    }

    def payload(*, repository: str = "benchmark/fixture") -> dict[str, Any]:
        return {
            "file": "package.py",
            "start_line": 1,
            "end_line": 1,
            "content": "first line\n",
            "content_projection": {
                "truncated": False,
                "original_chars": 11,
                "returned_chars": 11,
                "strategy": "complete",
            },
            "source": {
                "repository": repository,
                "commit": "a" * 40,
                "source_fingerprint": _SOURCE_V2,
                "verified": True,
                "verification_scope": "content-bytes",
                "commit_verified": False,
                "checkout_state": "not-attested",
            },
        }

    valid = payload()
    assert (
        profiler._validated_public_source_payload(  # noqa: SLF001
            valid,
            request=portable_request,
            file_path="package.py",
            start_line=1,
            source_fingerprint=_SOURCE_V2,
        )
        == valid
    )
    ordinary_request = {
        **portable_request,
        "arm": "legacy",
    }
    ordinary = payload(repository=os.fspath(subject_root))
    assert (
        profiler._validated_public_source_payload(  # noqa: SLF001
            ordinary,
            request=ordinary_request,
            file_path="package.py",
            start_line=1,
            source_fingerprint=_SOURCE_V2,
        )
        == ordinary
    )

    invalid_payloads: list[dict[str, Any]] = []
    for field in valid:
        missing = deepcopy(valid)
        missing.pop(field)
        invalid_payloads.append(missing)
    extra = deepcopy(valid)
    extra["unexpected"] = True
    invalid_payloads.append(extra)
    for field in valid["source"]:
        missing = deepcopy(valid)
        missing["source"].pop(field)
        invalid_payloads.append(missing)
    extra_source = deepcopy(valid)
    extra_source["source"]["unexpected"] = True
    invalid_payloads.append(extra_source)

    for field, replacement in (
        ("repository", "wrong/repository"),
        ("commit", "b" * 40),
        ("source_fingerprint", "sha256-v2:" + "f" * 64),
        ("verified", False),
        ("verified", 1),
        ("verification_scope", "commit"),
        ("commit_verified", True),
        ("commit_verified", 0),
        ("checkout_state", "attested"),
    ):
        drift = deepcopy(valid)
        drift["source"][field] = replacement
        invalid_payloads.append(drift)

    for field, replacement in (
        ("file", None),
        ("file", "other.py"),
        ("start_line", True),
        ("start_line", 0),
        ("end_line", 2),
        ("content", None),
        ("content_projection", None),
    ):
        malformed = deepcopy(valid)
        malformed[field] = replacement
        invalid_payloads.append(malformed)
    projection_extra = deepcopy(valid)
    projection_extra["content_projection"]["unexpected"] = True
    invalid_payloads.append(projection_extra)
    projection_count_drift = deepcopy(valid)
    projection_count_drift["content_projection"]["returned_chars"] = 10
    invalid_payloads.append(projection_count_drift)

    for invalid in invalid_payloads:
        with pytest.raises((RuntimeError, ValueError)):
            profiler._validated_public_source_payload(  # noqa: SLF001
                invalid,
                request=portable_request,
                file_path="package.py",
                start_line=1,
                source_fingerprint=_SOURCE_V2,
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


def test_v4_manifest_accepts_only_the_frozen_five_cell_order(tmp_path: Path) -> None:
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
    assert manifest["schema_version"] == 4
    assert manifest["benchmark"] == "retained_storage_explicit_route_gate_v4"
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


def test_canonical_v4_report_has_exact_shape_tracks_and_process_count(
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
        "cell_route_contracts",
        "parity_projections",
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
        name: {
            "cells": list(specification["cells"]),
            "projection": specification["projection"],
        }
        for name, specification in profiler.TRACKS.items()
    }
    assert report["protocol"]["expected"]["cell_route_contracts"] == (
        profiler.CELL_ROUTE_CONTRACTS
    )
    assert report["protocol"]["expected"]["parity_projections"] == list(
        profiler.PARITY_PROJECTIONS
    )
    assert report["protocol"]["expected"]["canonical_sample_count"] == 1440
    assert report["configuration"]["cell_route_contracts"] == (
        profiler.CELL_ROUTE_CONTRACTS
    )
    assert report["configuration"]["parity_projections"] == list(
        profiler.PARITY_PROJECTIONS
    )
    assert report["configuration"]["stopwatch_boundaries"] == {
        "compiler-cold": (
            "fresh-inner-codenib-cli-import-parser-index-handler-through-return"
        ),
        "compiler-current": (
            "fresh-inner-codenib-cli-import-parser-index-handler-through-return"
        ),
        "runtime-cold": (
            "fresh-inner-import-parser-handler-through-ready-callback-fixed-"
            "queries-public-manifest-source-read-probe-or-refusal-and-normal-"
            "cleanup-return"
        ),
        "runtime-cold-query-only": (
            "fresh-inner-import-parser-direct-or-retained-materialized-"
            "artifact-handler-through-ready-callback-fixed-queries-public-"
            "manifest-source-read-refusal-and-normal-cleanup-return"
        ),
        "runtime-cold-source-bound": (
            "fresh-inner-import-parser-ordinary-manifest-or-retained-"
            "materialized-artifact-handler-through-ready-callback-fixed-queries-"
            "public-manifest-source-read-probe-and-normal-cleanup-return"
        ),
    }
    assert report["configuration"]["cold_definitions"] == {
        "compiler-cold": "empty-codenib-cache",
        "runtime-cold": (
            "fresh-process-and-context-with-public-manifest-and-source-read-"
            "probe-or-refusal"
        ),
        "runtime-cold-query-only": (
            "fresh-process-and-context-with-public-manifest-and-source-read-refusal"
        ),
        "runtime-cold-source-bound": (
            "fresh-process-and-context-with-public-manifest-and-source-read-probe"
        ),
    }
    assert report["decision"] == {
        "policy_status": "unratified",
        "report_only": True,
        "promotion_eligible": False,
        "recommendation": "retain-explicit-routes",
        "reason": (
            "one or more track parity projections are red; one or more "
            "compatibility scopes remain incomplete"
        ),
    }
    assert report["benchmark_receipts"]["unchanged"] is True
    assert len(report["subjects"]) == 3
    assert len(report["media"]) == 2
    assert len(report["cells"]) == 30
    assert {
        cell: sum(item["cell"] == cell for item in report["cells"].values())
        for cell in profiler.CELLS
    } == {cell: 6 for cell in profiler.CELLS}
    assert list(report["tracks"]) == [
        "compiler",
        "query-only-runtime",
        "manifest-runtime-compatibility",
        "source-bound-runtime",
        "source-bound-manifest-compatibility",
    ]
    assert report["tracks"] == {
        "compiler": {
            "cells": ["compiler-cold", "compiler-current"],
            "projection": "compiler",
            "measurement_complete": True,
            "parity_passed": True,
            "safety_passed": True,
            "scope_complete": True,
            "decision": "passed",
            "passed": True,
            "policy_status": "unratified",
            "promotion_eligible": False,
        },
        "query-only-runtime": {
            "cells": ["runtime-cold-query-only"],
            "projection": "full-runtime",
            "measurement_complete": True,
            "parity_passed": True,
            "safety_passed": True,
            "scope_complete": True,
            "decision": "passed",
            "passed": True,
            "policy_status": "unratified",
            "promotion_eligible": False,
        },
        "manifest-runtime-compatibility": {
            "cells": ["runtime-cold"],
            "projection": "full-runtime",
            "measurement_complete": True,
            "parity_passed": False,
            "safety_passed": True,
            "scope_complete": False,
            "decision": "blocked",
            "passed": False,
            "policy_status": "unratified",
            "promotion_eligible": False,
        },
        "source-bound-runtime": {
            "cells": ["runtime-cold-source-bound"],
            "projection": "content-authority",
            "measurement_complete": True,
            "parity_passed": True,
            "safety_passed": True,
            "scope_complete": True,
            "decision": "passed",
            "passed": True,
            "policy_status": "unratified",
            "promotion_eligible": False,
        },
        "source-bound-manifest-compatibility": {
            "cells": ["runtime-cold-source-bound"],
            "projection": "full-runtime",
            "measurement_complete": True,
            "parity_passed": False,
            "safety_passed": True,
            "scope_complete": False,
            "decision": "blocked",
            "passed": False,
            "policy_status": "unratified",
            "promotion_eligible": False,
        },
    }
    assert report["process_isolation"]["passed"] is True
    assert report["process_isolation"]["expected_samples"] == 1440
    assert report["process_isolation"]["observed_samples"] == 1440
    assert len(report["process_isolation"]["inner_process_ids"]) == 1440
    assert len(set(report["process_isolation"]["inner_process_ids"])) == 1440
    assert report["process_isolation"]["duplicate_process_ids"] == []
    for cell in report["cells"].values():
        assert {"runs", "summary", "parity", "safety", "performance"} <= set(cell)
        assert (
            cell["primary_projection"]
            == profiler.CELL_PRIMARY_PROJECTIONS[cell["cell"]]
        )
        assert set(cell["parity"]) == set(profiler.PARITY_PROJECTIONS)
        for projection in profiler.PARITY_PROJECTIONS:
            assert set(cell["parity"][projection]) == {
                "every_pair_equal",
                "stable_within_arm",
                "identity_sha256",
                "passed",
            }
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
    assert report["process_isolation"]["observed_samples"] == 60
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
        lambda receipt, _request, _call: receipt["result"]["bm25_plan"].__setitem__(
            "source_fingerprint", _SHA_D
        ),
        lambda receipt, request, _call: (
            receipt["parity_identities"]["compiler"]["view"].__setitem__(
                "documents_sha256", _SHA_B
            )
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
                profiler._index_arguments(request("compiler-current", "legacy")),
            )
        ],
        ("compiler-current", "candidate"): [
            ("provision", paths),
            (
                "cli",
                profiler._index_arguments(request("compiler-current", "candidate")),
            ),
            (
                "cli",
                profiler._import_cache_arguments(
                    request("compiler-current", "candidate"), paths
                ),
            ),
        ],
        ("runtime-cold", "legacy"): [
            (
                "cli",
                profiler._index_arguments(request("runtime-cold", "legacy")),
            )
        ],
        ("runtime-cold", "candidate"): [
            ("provision", paths),
            (
                "cli",
                profiler._index_arguments(request("runtime-cold", "candidate")),
            ),
            (
                "cli",
                profiler._import_cache_arguments(
                    request("runtime-cold", "candidate"), paths
                ),
            ),
            (
                "cli",
                profiler._materialize_arguments(
                    request("runtime-cold", "candidate"), paths, generation=1
                ),
            ),
        ],
        ("runtime-cold-query-only", "legacy"): [
            (
                "cli",
                profiler._index_arguments(request("runtime-cold-query-only", "legacy")),
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
                    request("runtime-cold-query-only", "candidate")
                ),
            ),
            (
                "cli",
                profiler._import_cache_arguments(
                    request("runtime-cold-query-only", "candidate"), paths
                ),
            ),
            (
                "cli",
                profiler._materialize_arguments(
                    request("runtime-cold-query-only", "candidate"),
                    paths,
                    generation=1,
                ),
            ),
        ],
        ("runtime-cold-source-bound", "legacy"): [
            (
                "cli",
                profiler._index_arguments(
                    request("runtime-cold-source-bound", "legacy")
                ),
            )
        ],
        ("runtime-cold-source-bound", "candidate"): [
            ("provision", paths),
            (
                "cli",
                profiler._index_arguments(
                    request("runtime-cold-source-bound", "candidate")
                ),
            ),
            (
                "cli",
                profiler._import_cache_arguments(
                    request("runtime-cold-source-bound", "candidate"), paths
                ),
            ),
            (
                "cli",
                profiler._materialize_arguments(
                    request("runtime-cold-source-bound", "candidate"),
                    paths,
                    generation=1,
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
        "--artifact",
        paths["runtime_output"],
        "--repository",
        subject["repository_key"],
    ]
    assert "--repo" not in retained_arguments
    source_bound_arguments = profiler._runtime_arguments(
        request("runtime-cold-source-bound", "candidate"), paths, generation=1
    )
    assert source_bound_arguments == [
        *retained_arguments,
        "--repo",
        os.fspath(subject_root.resolve()),
    ]
    assert profiler._runtime_arguments(
        request("runtime-cold-source-bound", "legacy"), paths, generation=None
    ) == ["mcp", os.fspath(profiler._cache_manifest_path(subject_root))]


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the canonical profiler route requires Linux /proc accounting",
)
def test_real_local_runtime_route_matrix_uses_each_public_mcp_delivery(
    tmp_path: Path,
) -> None:
    manifest_path, subject_roots, media_roots = _fixture(tmp_path)
    manifest, _receipt = profiler.load_subject_manifest(manifest_path)
    subject = manifest["subjects"][0]
    subject_root = subject_roots[subject["id"]]
    media_id, media_root = next(iter(media_roots.items()))
    route_matrix = (
        ("runtime-cold", "legacy", "manifest-path", "verified"),
        (
            "runtime-cold-query-only",
            "legacy",
            "direct-artifact",
            "source-disabled",
        ),
        (
            "runtime-cold-query-only",
            "candidate",
            "retained-materialized-artifact",
            "source-disabled",
        ),
        (
            "runtime-cold-source-bound",
            "candidate",
            "retained-materialized-artifact",
            "verified",
        ),
    )
    receipts: dict[tuple[str, str], dict[str, Any]] = {}

    for index, (cell, arm, route, source_status) in enumerate(route_matrix, start=1):
        request = {
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
            "run_id": f"{index:032x}",
        }

        try:
            receipt = profiler._sample_worker(request)  # noqa: SLF001
        except Exception as exc:
            raise AssertionError(f"real route failed for {cell}/{arm}") from exc
        receipt = profiler._validate_sample_receipt(  # noqa: SLF001
            receipt,
            request=request,
        )

        assert receipt["result"]["runtime"]["delivery"]["route"] == route
        assert receipt["result"]["source_read"]["status"] == source_status
        assert receipt["result"]["public_manifest"] is not None
        assert receipt["safety"]["context_closed"] is True
        assert receipt["safety"]["source_closed"] is True
        assert receipt["safety"]["cleanup_complete"] is True
        receipts[(cell, arm)] = receipt

    direct = receipts[("runtime-cold-query-only", "legacy")]
    retained_query_only = receipts[("runtime-cold-query-only", "candidate")]
    assert direct["parity_identities"] == retained_query_only["parity_identities"]

    ordinary = receipts[("runtime-cold", "legacy")]
    retained_source = receipts[("runtime-cold-source-bound", "candidate")]
    assert (
        ordinary["parity_identities"]["content-authority"]
        == retained_source["parity_identities"]["content-authority"]
    )
    assert (
        ordinary["parity_identities"]["full-runtime"]
        != retained_source["parity_identities"]["full-runtime"]
    )
    assert not any(media_root.iterdir())


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
                "source_closed": True,
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


def test_source_bound_runtime_manifest_allows_only_the_two_authenticated_rebinds(
    tmp_path: Path,
) -> None:
    subject_root = tmp_path / "subject"
    subject_root.mkdir()
    runtime_output = tmp_path / "runtime-output"
    runtime_view = runtime_output / "views" / "bm25"
    runtime_view.mkdir(parents=True)
    persisted = {
        "version": "1.2",
        "repo": {
            "path": "source",
            "commit": "a" * 40,
            "source_fingerprint": _SOURCE_V2,
        },
        "indexes": {
            "bm25": {
                "path": "views/bm25",
                "status": "fresh",
                "metadata": {"builder_schema": 8},
            }
        },
    }
    request = {
        "cell": "runtime-cold-source-bound",
        "arm": "candidate",
        "subject_root": os.fspath(subject_root),
    }
    expected = deepcopy(persisted)
    expected["repo"]["path"] = os.fspath(subject_root)
    expected["indexes"]["bm25"]["path"] = os.fspath(runtime_view.resolve())

    assert (
        profiler._validate_runtime_context_manifest(  # noqa: SLF001
            expected,
            persisted=persisted,
            request=request,
            paths={"runtime_output": os.fspath(runtime_output)},
        )
        == expected
    )
    assert persisted["repo"]["path"] == "source"
    assert persisted["indexes"]["bm25"]["path"] == "views/bm25"

    invalid_values: list[dict[str, Any]] = []
    for mutate in (
        lambda value: value["repo"].__setitem__("commit", "b" * 40),
        lambda value: value["indexes"]["bm25"]["metadata"].__setitem__(
            "builder_schema", 9
        ),
        lambda value: value.__setitem__("unexpected", True),
        lambda value: value.pop("version"),
        lambda value: value["repo"].__setitem__("path", "source"),
        lambda value: value["indexes"]["bm25"].__setitem__(
            "path", os.fspath(tmp_path / "other-view")
        ),
    ):
        changed = deepcopy(expected)
        mutate(changed)
        invalid_values.append(changed)
    for invalid in invalid_values:
        with pytest.raises(RuntimeError, match="exact persisted binding"):
            profiler._validate_runtime_context_manifest(  # noqa: SLF001
                invalid,
                persisted=persisted,
                request=request,
                paths={"runtime_output": os.fspath(runtime_output)},
            )

    malformed_persisted = deepcopy(persisted)
    malformed_persisted["repo"]["path"] = os.fspath(subject_root)
    malformed_view = deepcopy(persisted)
    malformed_view["indexes"]["bm25"]["path"] = "views/other"
    for malformed in (malformed_persisted, malformed_view):
        with pytest.raises(RuntimeError, match="canonically portable"):
            profiler._validate_runtime_context_manifest(  # noqa: SLF001
                expected,
                persisted=malformed,
                request=request,
                paths={"runtime_output": os.fspath(runtime_output)},
            )


def test_other_runtime_manifests_require_exact_persisted_identity(
    tmp_path: Path,
) -> None:
    ordinary = {
        "version": "1.2",
        "repo": {"path": os.fspath(tmp_path / "subject"), "commit": "a" * 40},
        "indexes": {
            "bm25": {
                "path": os.fspath(tmp_path / "ordinary-view"),
                "status": "fresh",
            }
        },
    }
    portable = {
        "version": "1.2",
        "repo": {"path": "source", "commit": "a" * 40},
        "indexes": {"bm25": {"path": "views/bm25", "status": "fresh"}},
    }
    routes = (
        ({"cell": "runtime-cold", "arm": "legacy"}, ordinary),
        ({"cell": "runtime-cold-query-only", "arm": "legacy"}, portable),
        ({"cell": "runtime-cold-query-only", "arm": "candidate"}, portable),
    )
    paths = {"runtime_output": os.fspath(tmp_path / "unused-runtime")}
    for request, persisted in routes:
        assert (
            profiler._validate_runtime_context_manifest(  # noqa: SLF001
                persisted,
                persisted=persisted,
                request=request,
                paths=paths,
            )
            == persisted
        )
        for mutation in (
            lambda value: value["repo"].__setitem__("commit", "b" * 40),
            lambda value: value["indexes"]["bm25"].__setitem__("status", "stale"),
            lambda value: value.pop("version"),
            lambda value: value.__setitem__("unexpected", True),
        ):
            changed = deepcopy(persisted)
            mutation(changed)
            with pytest.raises(RuntimeError, match="exact persisted binding"):
                profiler._validate_runtime_context_manifest(  # noqa: SLF001
                    changed,
                    persisted=persisted,
                    request=request,
                    paths=paths,
                )


def test_runtime_context_authority_contract_is_cell_and_arm_specific() -> None:
    subject = {
        "repository_key": "benchmark/fixture",
        "revision": "a" * 40,
    }
    ordinary_request = {
        "cell": "runtime-cold",
        "arm": "legacy",
        "subject": subject,
    }
    candidate_request = {
        "cell": "runtime-cold",
        "arm": "candidate",
        "subject": subject,
    }
    direct_request = {
        "cell": "runtime-cold-query-only",
        "arm": "legacy",
        "subject": subject,
    }
    legacy = {
        "loaded_views": ["bm25"],
        "errors": {},
        "artifact": None,
        "source_verified": True,
        "source_error": None,
        "source_verification_scope": "content-bytes",
        "native_vector_authorized": False,
        "native_lsp_allowed": True,
        "lsp_provider": _runtime_identity(ordinary_request)["capabilities"][
            "lsp_provider"
        ],
        "commit_verified": False,
    }
    candidate = {
        **legacy,
        "artifact": _runtime_identity(candidate_request)["artifact"],
        "source_verified": False,
        "source_error": "retained route has no repository source",
        "source_verification_scope": None,
        "native_lsp_allowed": False,
        "lsp_provider": _runtime_identity(candidate_request)["capabilities"][
            "lsp_provider"
        ],
    }

    legacy_authority = profiler._validate_runtime_context_state(
        legacy,
        request=ordinary_request,
    )
    candidate_authority = profiler._validate_runtime_context_state(
        candidate,
        request=candidate_request,
    )
    direct_authority = profiler._validate_runtime_context_state(
        candidate,
        request=direct_request,
    )

    assert legacy_authority == _runtime_identity(ordinary_request)
    assert direct_authority == _runtime_identity(direct_request)
    assert candidate_authority == _runtime_identity(candidate_request)
    assert direct_authority["delivery"] == {
        "route": "direct-artifact",
        "context_provenance": "portable-context-artifact",
    }
    assert candidate_authority["delivery"] == {
        "route": "retained-materialized-artifact",
        "context_provenance": "portable-context-artifact",
    }
    assert {
        key: value for key, value in direct_authority.items() if key != "delivery"
    } == {key: value for key, value in candidate_authority.items() if key != "delivery"}
    assert candidate_authority["source_authority"] == {
        "kind": "source-disabled",
        "verified": False,
        "verification_scope": None,
    }
    assert set(candidate_authority["capabilities"]) == {
        "loaded_views",
        "view_errors",
        "read_source",
        "native_vector_authorized",
        "native_lsp_allowed",
        "lsp_provider",
        "commit_verified",
        "checkout_state",
    }
    assert candidate_authority["capabilities"]["view_errors"] == {}

    for invalid_view_errors in ([], {"bm25": "synthetic failure"}):
        invalid_runtime = _runtime_identity(candidate_request)
        invalid_runtime["capabilities"]["view_errors"] = invalid_view_errors
        with pytest.raises((RuntimeError, ValueError)):
            profiler._validate_runtime_identity(  # noqa: SLF001
                invalid_runtime,
                request=candidate_request,
                label="synthetic runtime identity",
            )

    with pytest.raises(RuntimeError, match="exact per-cell contract"):
        profiler._validate_runtime_context_state(
            {
                **legacy,
                "source_verified": False,
                "source_error": "synthetic unverified source",
                "source_verification_scope": None,
            },
            request=ordinary_request,
        )
    with pytest.raises(RuntimeError, match="exact per-cell contract"):
        profiler._validate_runtime_context_state(
            {
                **candidate,
                "source_verified": True,
                "source_error": None,
                "source_verification_scope": "content-bytes",
            },
            request=direct_request,
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
