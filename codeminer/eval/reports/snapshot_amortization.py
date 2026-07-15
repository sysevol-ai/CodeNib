# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Account for exact-snapshot reuse of one materialized repository view."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from codeminer.compiler.snapshot_store import SourceSnapshot


def view_construction_seconds(profile: Mapping[str, Any], level: str) -> float:
    """Return non-overlapping embedding-plus-index construction time."""

    if level not in {"l0", "l2"}:
        raise ValueError(f"unsupported view level {level!r}")
    sections = profile.get("sections") or []
    by_label: dict[str, float] = {}
    for section in sections:
        label = str(section.get("label") or "")
        value = section.get("total")
        if not label or not isinstance(value, (int, float)):
            continue
        if label in by_label:
            raise ValueError(f"duplicate profile section {label!r}")
        by_label[label] = float(value)

    combined = by_label.get(f"vector_store_add_documents_{level}")
    if combined is not None:
        return _nonnegative(combined, f"vector_store_add_documents_{level}")

    embedding_label = f"embedding_encode_{level}"
    if embedding_label not in by_label:
        raise ValueError(f"profile is missing {embedding_label!r}")
    embedding = _nonnegative(by_label[embedding_label], embedding_label)
    index_label = f"faiss_index_add_{level}"
    index = _nonnegative(by_label.get(index_label, 0.0), index_label)
    return embedding + index


def analyze_snapshot_amortization(
    dataset_rows: Iterable[Mapping[str, Any]],
    build_profiles: Sequence[Mapping[str, Any]],
    *,
    level: str = "l2",
    overhead_thresholds_seconds: Sequence[float] = (1.0, 0.1),
) -> dict[str, Any]:
    """Compare build-once reuse with the same view rebuilt per consumer."""

    rows = [dict(row) for row in dataset_rows]
    if not rows:
        raise ValueError("dataset has no rows")
    thresholds = tuple(float(value) for value in overhead_thresholds_seconds)
    if any(value <= 0 for value in thresholds):
        raise ValueError("overhead thresholds must be positive")

    profiles_by_snapshot: dict[str, Mapping[str, Any]] = {}
    identities = set()
    for profile in build_profiles:
        snapshot = _snapshot(profile)
        if snapshot.snapshot_id in profiles_by_snapshot:
            raise ValueError(
                f"duplicate build profile for {snapshot.repo}@{snapshot.commit}"
            )
        profiles_by_snapshot[snapshot.snapshot_id] = profile
        identities.add(_profile_identity(profile, level))
    if not profiles_by_snapshot:
        raise ValueError("no build profiles")
    if len(identities) != 1:
        raise ValueError("build profiles do not share one view identity")

    consumers = Counter(_snapshot(row).snapshot_id for row in rows)
    dataset_snapshots = {_snapshot(row).snapshot_id: _snapshot(row) for row in rows}
    missing = sorted(set(consumers) - set(profiles_by_snapshot))
    if missing:
        labels = [
            f"{dataset_snapshots[key].repo}@{dataset_snapshots[key].commit}"
            for key in missing
        ]
        raise ValueError(f"missing build profiles for {', '.join(labels)}")

    result_rows = []
    for snapshot_id in sorted(consumers):
        snapshot = dataset_snapshots[snapshot_id]
        profile = profiles_by_snapshot[snapshot_id]
        count = consumers[snapshot_id]
        build_seconds = view_construction_seconds(profile, level)
        result_rows.append(
            {
                "snapshot_id": snapshot_id,
                "repository": snapshot.repo,
                "commit": snapshot.commit,
                "language": profile.get("language_group") or profile.get("language"),
                "consumer_count": count,
                "build_once_seconds": build_seconds,
                "rebuild_per_consumer_seconds": build_seconds * count,
                "avoided_build_seconds": build_seconds * (count - 1),
                "amortized_build_seconds_per_consumer": build_seconds / count,
                "consumers_for_overhead_threshold": {
                    _threshold_key(threshold): math.ceil(build_seconds / threshold)
                    for threshold in thresholds
                },
            }
        )

    build_once = sum(row["build_once_seconds"] for row in result_rows)
    rebuild_per_consumer = sum(
        row["rebuild_per_consumer_seconds"] for row in result_rows
    )
    avoided = rebuild_per_consumer - build_once
    amortized = [row["amortized_build_seconds_per_consumer"] for row in result_rows]
    profile_identity = next(iter(identities))
    return {
        "schema_version": 1,
        "view": {
            "level": level,
            "embedding_model": profile_identity[0],
            "embedding_provider": profile_identity[1],
            "embedding_dimension": profile_identity[2],
            "profile_tag": profile_identity[3],
        },
        "method": {
            "shared_total": "one non-overlapping view construction per snapshot",
            "comparator_total": "the identical view rebuilt once per consumer",
            "construction_timer": (
                f"vector_store_add_documents_{level}, or "
                f"embedding_encode_{level} + faiss_index_add_{level}"
            ),
            "excluded_timers": ["total_duration", "build_vector_store"],
            "excludes": [
                "source checkout",
                "source chunking",
                "graph construction",
                "query execution",
            ],
        },
        "rows": result_rows,
        "summary": {
            "consumer_records": len(rows),
            "unique_snapshots": len(result_rows),
            "avoided_rebuilds": len(rows) - len(result_rows),
            "records_per_snapshot_mean": len(rows) / len(result_rows),
            "records_per_snapshot_median": statistics.median(consumers.values()),
            "build_once_seconds": build_once,
            "rebuild_per_consumer_seconds": rebuild_per_consumer,
            "avoided_build_seconds": avoided,
            "avoided_build_fraction": (
                avoided / rebuild_per_consumer if rebuild_per_consumer else 0.0
            ),
            "amortized_build_seconds_per_consumer": build_once / len(rows),
            "snapshot_amortized_seconds_median": statistics.median(amortized),
        },
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the headline accounting result without hiding its boundary."""

    summary = report["summary"]
    view = report["view"]
    return "\n".join(
        [
            "# Snapshot view amortization",
            "",
            (
                f"View: {view['embedding_model']} {str(view['level']).upper()} "
                f"({view['embedding_dimension']} dimensions)."
            ),
            "",
            "| consumers | snapshots | avoided rebuilds | build once | "
            "rebuild per consumer | amortized / consumer |",
            "|---:|---:|---:|---:|---:|---:|",
            (
                f"| {summary['consumer_records']} | {summary['unique_snapshots']} "
                f"| {summary['avoided_rebuilds']} "
                f"| {summary['build_once_seconds']:.1f} s "
                f"| {summary['rebuild_per_consumer_seconds']:.1f} s "
                f"| {summary['amortized_build_seconds_per_consumer']:.3f} s |"
            ),
            "",
            (
                "The comparator rebuilds the identical view for every consumer. "
                "Times exclude checkout, chunking, graph construction, and queries."
            ),
            "",
        ]
    )


def _snapshot(payload: Mapping[str, Any]) -> SourceSnapshot:
    repo = str(payload.get("repo") or payload.get("repository") or "").strip()
    commit = str(payload.get("base_commit") or payload.get("commit") or "").strip()
    if not repo or not commit:
        raise ValueError("row is missing repository snapshot identity")
    return SourceSnapshot(repo, commit)


def _profile_identity(
    profile: Mapping[str, Any], level: str
) -> tuple[str, str, int, str]:
    model = str(profile.get("embedding_model") or "").strip()
    provider = str(profile.get("embedding_provider") or "").strip()
    dimension = profile.get("embedding_dimension")
    tag = str(profile.get("profile_tag") or "").strip()
    if not model or not provider or not isinstance(dimension, int) or not tag:
        raise ValueError("build profile has incomplete view identity")
    view_construction_seconds(profile, level)
    return model, provider, dimension, tag


def _nonnegative(value: float, label: str) -> float:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"invalid duration for {label!r}: {value!r}")
    return value


def _threshold_key(threshold: float) -> str:
    return f"le_{threshold:g}_seconds"
