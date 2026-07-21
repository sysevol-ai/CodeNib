#!/usr/bin/env python3
"""Recompute and verify the manuscript-facing numerical claim ledger."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from draw_faiss_index_ablation import load_results, select_guardrailed_method
from draw_graphrag_rerank import (
    GRAPH_PAIRS,
    _latency,
    _paired_effect,
    _pairwise_interaction_audit,
    load_matrix,
)
from draw_lsp_replay import _load_reports, prepare_replay_data
from draw_materialized_trace import _validate_summary
from draw_pareto_rerank import load_pareto_data
from draw_profile import load_profiles as load_build_profiles
from draw_query_profile import load_profiles as load_query_profiles
from draw_retrieval_ablation_suite import select_graph_weight
from plot_style import MODEL_DIMENSIONS, MODEL_IDS, MODEL_LABELS, MODEL_ORDER

# This is the reviewed manuscript-facing contract, not another estimator. Each
# value is compared with a recomputation from the frozen inputs below. Keeping
# trailing zeros as strings makes the paper's reported precision explicit.
DISPLAY_CONTRACT: dict[str, Any] = {
    "setup.base.snapshots": "100",
    "setup.base.repositories": "25",
    "setup.base.models": "5",
    "setup.graph.dev_snapshots": "58",
    "setup.graph.dev_repositories": "15",
    "setup.graph.test_snapshots": "42",
    "setup.graph.test_repositories": "10",
    "setup.faiss.accuracy_guardrail": "0.95",
    "setup.agent.queries_per_model": "500",
    "setup.agent.models": "5",
    "setup.agent.trajectories": "7500",
    "setup.lifecycle.snapshots": "25",
    "setup.lifecycle.requests_per_trace": "42",
    "setup.incremental.transitions": "40",
    "setup.incremental.repositories": "8",
    "setup.incremental.graph_source_transitions": "33",
    "setup.incremental.vector_source_transitions": "31",
    "rq1.dense.mean_query_ms_range": ["26", "295"],
    "rq1.dense.file_recall_range": ["0.705", "0.820"],
    "rq1.dense.symbol_recall_range": ["0.422", "0.638"],
    "rq1.jina_rerank4b_k50.file_recall": "0.858",
    "rq1.jina_rerank4b_k50.query_s": "4.29",
    "rq1.jina_dense.file_recall": "0.812",
    "rq1.jina_dense.query_ms": "92",
    "rq1.best_symbol.recall": "0.742",
    "rq1.best_symbol.query_s": "14.1",
    "rq1.graph.selected_weight": "0.5",
    "rq1.graph.no_reranker_effect_range_pp": ["-4.8", "+7.1"],
    "rq1.graph.qwen06_ci95_pp": ["-2.6", "+19.5"],
    "rq1.graph.all_model_intervals_include_zero": "true",
    "rq1.graph.all_familywise_intervals_include_zero": "true",
    "rq1.graph.nonreranked_added_latency_ms_range": ["15", "39"],
    "rq1.graph.reranked_total_latency_s_range": ["2.7", "6.8"],
    "rq2.profile.paired_profiles": "500",
    "rq2.profile.construction_measurements": "1000",
    "rq2.profile.l0_median_s_range": ["3.8", "56.7"],
    "rq2.profile.l2_median_s_range": ["19.3", "285.0"],
    "rq2.profile.per_snapshot_l2_l0_median_ratio_range": ["5.0", "6.4"],
    "rq2.profile.query_median_ms_range": ["20.9", "233.8"],
    "rq2.profile.qwen06_query_median_ms": "45.1",
    "rq2.profile.parameter_count_b_range": ["0.137", "7.069"],
    "rq2.profile.output_dimension_range": ["768", "3584"],
    "rq2.profile.l0_slope_s_per_kloc_range": ["0.029", "0.644"],
    "rq2.profile.l2_slope_s_per_kloc_range": ["0.186", "4.282"],
    "rq2.profile.all_loc_slopes_positive": "true",
    "rq2.profile.slopes_increase_with_parameter_count": "true",
    "rq2.profile.query_medians_increase_with_parameter_count": "true",
    "rq2.profile.parameter_dimension_rank_aligned": "true",
    "rq2.faiss.flat_mean_search_ms": "0.910",
    "rq2.faiss.flat_mean_task_recall": "0.620",
    "rq2.faiss.hnsw16_neighbor_recall": "0.977",
    "rq2.faiss.hnsw16_mean_search_ms": "0.0268",
    "rq2.faiss.hnsw16_speedup": "33.9",
    "rq2.faiss.ivf25_neighbor_recall": "0.965",
    "rq2.faiss.ivf25_mean_search_ms": "0.223",
    "rq2.faiss.ivf25_speedup": "4.1",
    "rq2.faiss.selected_task_outcomes_equal_flat": "true",
    "rq2.faiss.hnsw_absolute_saving_ms": "0.883",
    "rq2.faiss.flat_build_ms": "6.4",
    "rq2.faiss.hnsw_build_s": "2.00",
    "rq2.faiss.ivf_build_s": "0.889",
    "rq2.faiss.size_mib": ["21.74", "23.18", "22.33"],
    "rq3.requests.total": "1000",
    "rq3.requests.matches": "632",
    "rq3.requests.match_rate": "63.2%",
    "rq3.requests.definition_matches": "437",
    "rq3.requests.definition_match_rate": "87.4%",
    "rq3.requests.reference_matches": "195",
    "rq3.requests.reference_match_rate": "39.0%",
    "rq3.requests.nonempty_matches": "630",
    "rq3.measurements.matched_rows": "6320",
    "rq3.measurements.static_p50_ms": "0.62",
    "rq3.measurements.live_p50_ms": "2.26",
    "rq3.measurements.paired_saving_p50_ms": "1.69",
    "rq3.measurements.live_static_ratio_p50": "4.72",
    "rq3.definition_match_rate_range": ["80%", "99%"],
    "rq3.reference_match_rate_range": ["22%", "73%"],
    "rq3.all_request_guardrail_pass": "false",
    "rq4.graph.symbol_strict_passes": "15",
    "rq4.graph.file_strict_passes": "14",
    "rq4.graph.symbol_speedup_p50": "8.67",
    "rq4.graph.symbol_speedup_iqr": ["5.95", "10.99"],
    "rq4.graph.file_speedup_p50": "1.95",
    "rq4.graph.joint_passes": "14",
    "rq4.graph.symbol_over_file_p50": "4.25",
    "rq4.vector.strict_passes": "28",
    "rq4.vector.speedup_p50": "25.44",
    "rq4.vector.speedup_iqr": ["15.24", "35.15"],
    "rq4.vector.artifact_exact": "31",
    "rq4.vector.ordered_replay_exact": "28",
    "rq4.vector.set_replay_exact": "28",
    "rq4.stability.comparisons": "4",
    "rq4.stability.edge_f1_p50_percent": "99.75",
    "rq4.stability.behavior_p50_percent": "99.35",
    "rq4.stability.changed_exact": "3",
    "rq4.table.graph.go": "7|7|8.89|6.99|10.01|100.00|100.00",
    "rq4.table.graph.python": "8|8|6.95|6.22|13.00|100.00|100.00",
    "rq4.table.graph.rust": "9|0|17.00|8.97|40.42|99.12|97.40",
    "rq4.table.graph.ts": "9|0|1.97|1.32|7.59|97.61|99.53",
    "rq4.table.vector.go": "7|7|27.61|22.18|31.77|7|7",
    "rq4.table.vector.python": "6|6|38.18|37.42|38.75|6|6",
    "rq4.table.vector.rust": "9|6|19.43|14.26|24.79|9|6",
    "rq4.table.vector.ts": "9|9|13.63|8.92|25.53|9|9",
    "lifecycle.materialization_median_s": "116.7",
    "lifecycle.materialization_ci95_s": ["65.6", "153.9"],
    "lifecycle.hydration_median_s": "7.40",
    "lifecycle.hydration_ci95_s": ["6.99", "8.07"],
    "lifecycle.service_median_s": "0.727",
    "lifecycle.service_ci95_s": ["0.593", "1.079"],
    "lifecycle.view_build_median_s": ["0.81", "37.97", "59.48"],
    "lifecycle.artifact_median_mib": "160.3",
    "lifecycle.max_vector_build_s": "3441",
    "lifecycle.max_graph_build_s": "499",
    "lifecycle.q20.sessions": "20",
    "lifecycle.q20.process_isolated_s": "13.20",
    "lifecycle.q20.process_isolated_ci95_s": ["10.43", "15.96"],
    "lifecycle.q20.shared_resident_s": "6.24",
    "lifecycle.q20.shared_resident_ci95_s": ["3.66", "8.11"],
    "lifecycle.q100.sessions": "100",
    "lifecycle.q100.projected_s": ["8.53", "1.27"],
    "rq5.baseline_mean_tokens_k": ["154.1", "159.7", "116.8", "72.5", "25.9"],
    "rq5.baseline_mean_recall": ["0.758", "0.701", "0.776", "0.505", "0.470"],
    "rq5.selected_token_percent_range": ["12.9", "49.9"],
    "rq5.haiku.selected_arm": "eager",
    "rq5.haiku.selected_token_percent": "49.9",
    "rq5.haiku.selected_token_ci95": ["44.2", "56.7"],
    "rq5.haiku.selected_recall_delta": "-0.009",
    "rq5.haiku.selected_recall_delta_ci95": ["-0.043", "+0.022"],
    "rq5.haiku.compact_token_percent": "61.6",
    "rq5.qwen9.compact_token_percent": "45.1",
    "rq5.qwen9.compact_recall_delta": "-0.006",
    "rq5.qwen27.compact_token_percent": "44.8",
    "rq5.qwen27.compact_recall_delta": "-0.003",
    "rq5.gemma.compact_token_percent": "12.9",
    "rq5.gemma.compact_recall_delta": "+0.040",
    "rq5.gemini.compact_token_percent": "35.8",
    "rq5.gemini.compact_recall_delta": "+0.067",
    "rq5.qwen27.eager_lower_recall_bound": "-0.055",
    "rq5.recall_delta_guardrail": "-0.05",
    "rq5.compact_eager_token_percent": ["123.3", "87.6", "78.7", "27.9", "76.3"],
    "rq5.compact_eager_token_ci95": [
        ["113.4", "134.1"],
        ["82.3", "93.0"],
        ["72.9", "85.4"],
        ["23.4", "32.5"],
        ["63.9", "92.3"],
    ],
    "rq5.compact_eager_recall_delta": [
        "+0.018",
        "-0.023",
        "+0.008",
        "-0.070",
        "-0.044",
    ],
    "rq5.compact_eager_recall_delta_ci95": [
        ["-0.013", "+0.051"],
        ["-0.052", "+0.005"],
        ["-0.014", "+0.032"],
        ["-0.097", "-0.045"],
        ["-0.074", "-0.018"],
    ],
    "rq5.forced_final_counts_by_arm": [
        ["0", "0", "0"],
        ["122", "83", "66"],
        ["54", "38", "18"],
        ["60", "9", "3"],
        ["16", "0", "0"],
    ],
    "rq5.forced_final_percent_by_arm": [
        ["0.0", "0.0", "0.0"],
        ["24.4", "16.6", "13.2"],
        ["10.8", "7.6", "3.6"],
        ["12.0", "1.8", "0.6"],
        ["3.2", "0.0", "0.0"],
    ],
    "rq5.pooled_subgroup_token_percent_range": ["38.3", "59.3"],
}

AGENT_SUMMARY_SCHEMA_VERSION = 11
AGENT_MODEL_ORDER = (
    "Claude Haiku 4.5",
    "Qwen3.5-9B",
    "Qwen3.5-27B",
    "Gemma 4-12B",
    "Gemini 2.5 Flash",
)


def _validate_agent_summary(summary: Mapping[str, Any]) -> None:
    if summary.get("schema_version") != AGENT_SUMMARY_SCHEMA_VERSION:
        raise ValueError(
            f"agent summary must use schema version {AGENT_SUMMARY_SCHEMA_VERSION}"
        )
    missing = sorted(set(AGENT_MODEL_ORDER) - set(summary.get("models", {})))
    if missing:
        raise ValueError(f"agent summary is missing models: {', '.join(missing)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--query-dir", type=Path, required=True)
    parser.add_argument("--model-scale-metadata", type=Path, required=True)
    parser.add_argument("--model-cache-provenance", type=Path, required=True)
    parser.add_argument("--eval-dir", type=Path, required=True)
    parser.add_argument("--dataset-stats", type=Path, required=True)
    parser.add_argument("--graph-result-dir", type=Path, required=True)
    parser.add_argument("--graph-selection-results", type=Path, required=True)
    parser.add_argument("--faiss-results", type=Path, required=True)
    parser.add_argument("--lsp-reports", type=Path, required=True)
    parser.add_argument("--lifecycle-summary", type=Path, required=True)
    parser.add_argument("--incremental-graph-results", type=Path, required=True)
    parser.add_argument("--incremental-vector-results", type=Path, required=True)
    parser.add_argument("--incremental-summary", type=Path, required=True)
    parser.add_argument("--agent-summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--graph-bootstrap-samples", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=20260713)
    parser.add_argument("--accuracy-guardrail", type=float, default=0.95)
    return parser.parse_args()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _format_value(value: Any, format_spec: str) -> Any:
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_format_value(item, format_spec) for item in value]
    if format_spec == "bool":
        return "true" if bool(value) else "false"
    if format_spec == "str":
        return str(value)
    if format_spec == "d":
        number = float(value)
        if not number.is_integer():
            raise ValueError(f"expected an integer, got {value!r}")
        return str(int(number))
    return format(float(value), format_spec)


class ClaimLedger:
    def __init__(self) -> None:
        self.claims: list[dict[str, Any]] = []
        self.failures: list[str] = []
        self.used: set[str] = set()

    def add(
        self,
        claim_id: str,
        raw_value: Any,
        format_spec: str,
        *,
        section: str,
        source: str,
        estimator: str,
        unit: str,
    ) -> None:
        if claim_id in self.used:
            raise ValueError(f"duplicate claim id: {claim_id}")
        if claim_id not in DISPLAY_CONTRACT:
            raise KeyError(f"claim is absent from DISPLAY_CONTRACT: {claim_id}")
        self.used.add(claim_id)
        observed = _format_value(raw_value, format_spec)
        expected = DISPLAY_CONTRACT[claim_id]
        passed = observed == expected
        if not passed:
            self.failures.append(
                f"{claim_id}: expected {expected!r}, recomputed {observed!r}"
            )
        self.claims.append(
            {
                "id": claim_id,
                "section": section,
                "source": source,
                "estimator": estimator,
                "unit": unit,
                "raw_value": _jsonable(raw_value),
                "format": format_spec,
                "display_value": observed,
                "expected_display_value": expected,
                "passed": passed,
            }
        )

    def finish(self, output: Path, protocol: Mapping[str, Any]) -> None:
        unused = sorted(set(DISPLAY_CONTRACT) - self.used)
        if unused:
            self.failures.append(f"unused display-contract claims: {unused}")
        if self.failures:
            raise ValueError(
                "paper claim verification failed:\n" + "\n".join(self.failures)
            )
        section_counts = Counter(claim["section"] for claim in self.claims)
        payload = {
            "schema_version": 1,
            "all_passed": True,
            "claim_count": len(self.claims),
            "section_counts": dict(sorted(section_counts.items())),
            "protocol": dict(protocol),
            "claims": self.claims,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"PASS paper claims: {len(self.claims)} claims")
        print(f"Saved: {output}")


def _lookup_row(frame, **criteria):
    selected = frame
    for key, value in criteria.items():
        selected = selected[selected[key] == value]
    if len(selected) != 1:
        raise ValueError(f"expected one row for {criteria}, found {len(selected)}")
    return selected.iloc[0]


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _require_keys(mapping: Mapping[str, Any], keys: Iterable[str], label: str) -> None:
    missing = sorted(set(keys) - mapping.keys())
    if missing:
        raise ValueError(f"{label} is missing required keys: {', '.join(missing)}")


def _require_hex_digest(value: Any, length: int, label: str) -> None:
    if not isinstance(value, str) or len(value) != length:
        raise ValueError(f"{label} must be a {length}-character hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be hexadecimal") from exc


def load_model_scale_metadata(
    metadata_path: Path, provenance_path: Path
) -> list[dict[str, Any]]:
    """Load scale metadata and bind it to the retained cache audit."""

    metadata = _read_json(metadata_path)
    provenance = _read_json(provenance_path)
    if metadata.get("schema_version") != 1:
        raise ValueError("unsupported model-scale metadata schema")
    if provenance.get("schema_version") != 1:
        raise ValueError("unsupported model-cache provenance schema")
    _require_keys(
        metadata,
        ("source", "parameter_count_method", "models"),
        "model-scale metadata",
    )
    models = metadata["models"]
    if not isinstance(models, list):
        raise ValueError("model-scale metadata models must be a list")
    model_ids = [row.get("model_id") for row in models]
    if model_ids != list(MODEL_IDS):
        raise ValueError(
            "model-scale metadata must follow the canonical model order: "
            f"{list(MODEL_IDS)!r}"
        )

    cached_models = provenance.get("models")
    if not isinstance(cached_models, Mapping):
        raise ValueError("model-cache provenance models must be an object")
    for row in models:
        model_id = row["model_id"]
        _require_keys(
            row,
            (
                "display_label",
                "revision",
                "output_dimension",
                "dimension_config_key",
                "stored_parameter_count",
                "tensor_count",
                "config",
                "weights",
            ),
            f"model-scale metadata {model_id}",
        )
        label = row["display_label"]
        if label != MODEL_LABELS[model_id]:
            raise ValueError(f"unexpected display label for {model_id}: {label!r}")
        if row["output_dimension"] != MODEL_DIMENSIONS[label]:
            raise ValueError(
                f"output dimension disagrees with figure contract for {model_id}"
            )
        if int(row["stored_parameter_count"]) <= 0 or int(row["tensor_count"]) <= 0:
            raise ValueError(f"non-positive model scale for {model_id}")

        config = row["config"]
        _require_keys(
            config,
            ("path", "bytes", "cache_blob_id", "sha256"),
            f"model-scale config {model_id}",
        )
        _require_hex_digest(config["cache_blob_id"], 40, f"{model_id} config blob")
        _require_hex_digest(config["sha256"], 64, f"{model_id} config SHA-256")
        for weight in row["weights"]:
            _require_keys(
                weight,
                ("path", "bytes", "sha256"),
                f"model-scale weight {model_id}",
            )
            _require_hex_digest(
                weight["sha256"], 64, f"{model_id} {weight['path']} SHA-256"
            )

        cached = cached_models.get(model_id)
        if not isinstance(cached, Mapping):
            raise ValueError(f"cache provenance is missing {model_id}")
        snapshots = [
            snapshot
            for snapshot in cached.get("snapshots", [])
            if snapshot.get("revision") == row["revision"]
        ]
        if len(snapshots) != 1:
            raise ValueError(
                f"expected one cache snapshot for {model_id}@{row['revision']}"
            )
        cached_files = snapshots[0].get("files", {})
        expected_config = {
            "blob": config["cache_blob_id"],
            "bytes": config["bytes"],
        }
        if cached_files.get(config["path"]) != expected_config:
            raise ValueError(
                f"config identity disagrees with cache audit for {model_id}"
            )
        expected_weights = {
            weight["path"]: {
                "blob": weight["sha256"],
                "bytes": weight["bytes"],
            }
            for weight in row["weights"]
        }
        cached_weights = {
            path: value
            for path, value in cached_files.items()
            if path.endswith(".safetensors")
        }
        if cached_weights != expected_weights:
            raise ValueError(
                f"weight identity disagrees with cache audit for {model_id}"
            )
    return models


def _linear_slopes(frame: Any, value_column: str) -> dict[str, float]:
    slopes: dict[str, float] = {}
    for model in MODEL_ORDER:
        rows = frame[frame["model"] == model]
        x = rows["total_loc_k"].to_numpy(dtype=float)
        y = rows[value_column].to_numpy(dtype=float)
        mask = np.isfinite(x) & np.isfinite(y)
        if mask.sum() < 2 or np.unique(x[mask]).size < 2:
            raise ValueError(f"insufficient finite points for {model} {value_column}")
        slopes[model] = float(np.polyfit(x[mask], y[mask], 1)[0])
    return slopes


def _strictly_increasing(values: Sequence[float]) -> bool:
    return all(
        left < right for left, right in zip(values[:-1], values[1:], strict=True)
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _swebench_repository(instance_id: str) -> str:
    """Recover the repository slug from a canonical SWE-bench instance ID."""

    repository, separator, issue = instance_id.rpartition("-")
    if separator != "-" or not issue.isdigit() or repository.count("__") != 1:
        raise ValueError(f"invalid SWE-bench instance ID: {instance_id!r}")
    return repository.replace("__", "/", 1)


def _median(values: Iterable[float]) -> float:
    return float(np.median(list(values)))


def _lsp_nonempty_matched_requests(reports: Sequence[Mapping[str, Any]]) -> int:
    count = 0
    for report in reports:
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in report.get("comparisons") or []:
            request_id = str((row.get("request") or {}).get("request_id") or "")
            grouped[request_id].append(row)
        for rows in grouped.values():
            if rows and all(row.get("same_result") is True for row in rows):
                if all(
                    (row.get("static_call") or {}).get("result_count", 0) > 0
                    and (row.get("reference_call") or {}).get("result_count", 0) > 0
                    for row in rows
                ):
                    count += 1
    return count


INCREMENTAL_LANGUAGES = ("go", "python", "rust", "ts")


def _transition_identity(row: Mapping[str, Any]) -> tuple[str, int]:
    instance_id = str(row.get("instance_id") or "")
    step = row.get("step")
    if not instance_id or not isinstance(step, int) or step < 1:
        raise ValueError("incremental row is missing instance_id or step")
    return instance_id, step


def _source_rows(
    rows: Sequence[Mapping[str, Any]], delta_key: str
) -> list[Mapping[str, Any]]:
    return [row for row in rows if int(row.get(delta_key) or 0) > 0]


def _validate_incremental_evidence(
    graph_rows: Sequence[Mapping[str, Any]],
    vector_rows: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    if summary.get("schema_version") != 2:
        raise ValueError("incremental summary must use schema version 2")

    identities: dict[str, set[tuple[str, int]]] = {}
    for view, rows, protocol in (
        ("graph", graph_rows, 21),
        ("vector", vector_rows, 4),
    ):
        view_summary = summary.get(view) or {}
        completeness = view_summary.get("completeness") or {}
        if completeness.get("complete") is not True:
            raise ValueError(f"{view} incremental result set is incomplete")
        if int(completeness.get("expected_rows") or 0) != 40:
            raise ValueError(f"{view} incremental manifest must contain 40 rows")
        if int(completeness.get("unique_transition_rows") or 0) != len(rows):
            raise ValueError(f"{view} raw row count disagrees with summary")
        view_identities = {_transition_identity(row) for row in rows}
        if len(view_identities) != len(rows):
            raise ValueError(f"{view} incremental results contain duplicate rows")
        identities[view] = view_identities
        raw_protocols = {row.get("protocol_version") for row in rows}
        if raw_protocols != {protocol}:
            raise ValueError(f"{view} incremental protocol is not frozen at {protocol}")
        if raw_protocols != set(view_summary.get("protocol_versions") or ()):
            raise ValueError(f"{view} protocol disagrees with summary")
    if identities["graph"] != identities["vector"]:
        raise ValueError("graph and vector inputs do not cover the same transitions")

    graph_source = _source_rows(graph_rows, "delta_files")
    vector_source = _source_rows(vector_rows, "delta_files_test_excluded")
    for view, rows in (("graph", graph_source), ("vector", vector_source)):
        expected = int(summary[view]["overall"]["source_changing_rows"])
        if len(rows) != expected:
            raise ValueError(f"{view} source-changing count disagrees with summary")
        if {str(row.get("language") or "").lower() for row in rows} != set(
            INCREMENTAL_LANGUAGES
        ):
            raise ValueError(f"{view} source rows do not cover all languages")

    symbol_passes = sum(
        row.get("equivalence_guarded_speedup_vs_fully_rebuild") is not None
        for row in graph_source
    )
    file_passes = sum(
        row.get("file_level_equivalence_guarded_speedup_vs_fully_rebuild") is not None
        for row in graph_source
    )
    vector_passes = sum(row.get("analysis_admitted") is True for row in vector_source)
    if symbol_passes != int(summary["graph"]["overall"]["symbol"]["admitted"]):
        raise ValueError("graph symbol admission count disagrees with summary")
    if file_passes != int(summary["graph"]["overall"]["file"]["admitted"]):
        raise ValueError("graph file admission count disagrees with summary")
    if vector_passes != int(summary["vector"]["overall"]["incremental"]["admitted"]):
        raise ValueError("vector admission count disagrees with summary")
    return graph_source, vector_source


def _graph_language_table_row(rows: Sequence[Mapping[str, Any]], language: str) -> str:
    selected = [row for row in rows if str(row["language"]).lower() == language]
    admitted = [
        float(row["equivalence_guarded_speedup_vs_fully_rebuild"])
        for row in selected
        if row.get("equivalence_guarded_speedup_vs_fully_rebuild") is not None
    ]
    observed = [float(row["speedup_vs_fully_rebuild"]) for row in selected]
    speedups = admitted or observed
    p25, p50, p75 = np.quantile(speedups, (0.25, 0.5, 0.75))
    edge_f1 = _median(
        row["symbol-level-patch"]["fidelity"]["raw_graph"]["metrics"]["edges"]["f1"]
        for row in selected
    )
    behavior = _median(
        row["symbol-level-patch"]["fidelity"]["behavior"]["equivalent_fraction"]
        for row in selected
    )
    return "|".join(
        (
            str(len(selected)),
            str(len(admitted)),
            f"{p50:.2f}",
            f"{p25:.2f}",
            f"{p75:.2f}",
            f"{100 * edge_f1:.2f}",
            f"{100 * behavior:.2f}",
        )
    )


def _vector_language_table_row(rows: Sequence[Mapping[str, Any]], language: str) -> str:
    selected = [row for row in rows if str(row["language"]).lower() == language]
    admitted = [row for row in selected if row.get("analysis_admitted") is True]
    speedups = [
        float(row["equivalence_guarded_speedup_vs_fully_rebuild"]) for row in admitted
    ]
    p25, p50, p75 = np.quantile(speedups, (0.25, 0.5, 0.75))
    artifact_exact = sum(
        row["fidelity"]["levels"]["l2"]["document_identities_exact"] is True
        and row["fidelity"]["levels"]["l2"]["vectors_close"] is True
        for row in selected
    )
    replay_exact = sum(
        row["fidelity"]["levels"]["l2"]["replay"]["ordered_exact_fraction"] == 1
        for row in selected
    )
    return "|".join(
        (
            str(len(selected)),
            str(len(admitted)),
            f"{p50:.2f}",
            f"{p25:.2f}",
            f"{p75:.2f}",
            str(artifact_exact),
            str(replay_exact),
        )
    )


def build_ledger(args: argparse.Namespace) -> ClaimLedger:
    ledger = ClaimLedger()
    add = ledger.add

    dense, rerank = load_pareto_data(args.profile_dir, args.query_dir, args.eval_dir)
    build = load_build_profiles(args.profile_dir, args.dataset_stats)
    query = load_query_profiles(args.query_dir)
    model_scale = load_model_scale_metadata(
        args.model_scale_metadata, args.model_cache_provenance
    )
    graph_matrix = load_matrix(args.graph_result_dir, "test")
    graph_selection = _read_json(args.graph_selection_results)
    faiss = load_results(args.faiss_results)
    reports = _load_reports(args.lsp_reports)
    replay = prepare_replay_data(reports)
    lifecycle = _read_json(args.lifecycle_summary)
    _validate_summary(lifecycle)
    incremental_graph = _read_jsonl(args.incremental_graph_results)
    incremental_vector = _read_jsonl(args.incremental_vector_results)
    incremental_summary = _read_json(args.incremental_summary)
    graph_source_rows, vector_source_rows = _validate_incremental_evidence(
        incremental_graph,
        incremental_vector,
        incremental_summary,
    )
    agent = _read_json(args.agent_summary)
    _validate_agent_summary(agent)

    add(
        "setup.base.snapshots",
        build["instance_id"].nunique(),
        "d",
        section="setup",
        source="profile records",
        estimator="unique snapshot count",
        unit="snapshots",
    )
    add(
        "setup.base.repositories",
        len({_swebench_repository(value) for value in build["instance_id"].unique()}),
        "d",
        section="setup",
        source="profile records",
        estimator="unique repository slugs in snapshot IDs",
        unit="repositories",
    )
    add(
        "setup.base.models",
        build["model"].nunique(),
        "d",
        section="setup",
        source="profile records",
        estimator="unique model count",
        unit="models",
    )
    graph_partitions = graph_selection["partitions"]
    dev_instances = [
        instance_id
        for instance_id, partition in graph_partitions.items()
        if partition == "dev"
    ]
    test_instances = [
        instance_id
        for instance_id, partition in graph_partitions.items()
        if partition == "test"
    ]
    add(
        "setup.graph.dev_snapshots",
        len(dev_instances),
        "d",
        section="setup",
        source="graph selection result",
        estimator="partition count",
        unit="snapshots",
    )
    add(
        "setup.graph.dev_repositories",
        len({_swebench_repository(value) for value in dev_instances}),
        "d",
        section="setup",
        source="graph selection result",
        estimator="unique repository slugs in development partition",
        unit="repositories",
    )
    add(
        "setup.graph.test_snapshots",
        len(graph_matrix[0]["results"]),
        "d",
        section="setup",
        source="GraphRAG matrix",
        estimator="held-out partition count",
        unit="snapshots",
    )
    add(
        "setup.graph.test_repositories",
        len({_swebench_repository(value) for value in test_instances}),
        "d",
        section="setup",
        source="graph selection result",
        estimator="unique repository slugs in held-out partition",
        unit="repositories",
    )
    add(
        "setup.faiss.accuracy_guardrail",
        args.accuracy_guardrail,
        ".2f",
        section="setup",
        source="claim-ledger protocol",
        estimator="configured minimum task-outcome retention",
        unit="fraction",
    )
    add(
        "setup.agent.queries_per_model",
        agent["paired_query_count"],
        "d",
        section="setup",
        source="agent summary",
        estimator="paired unique query count",
        unit="queries/model",
    )
    add(
        "setup.agent.models",
        len(agent["models"]),
        "d",
        section="setup",
        source="agent summary",
        estimator="model count",
        unit="models",
    )
    add(
        "setup.agent.trajectories",
        agent["paired_query_count"] * len(agent["models"]) * 3,
        "d",
        section="setup",
        source="agent summary",
        estimator="queries x models x policies",
        unit="trajectories",
    )
    add(
        "setup.lifecycle.snapshots",
        lifecycle["protocol"]["snapshot_count"],
        "d",
        section="setup",
        source="lifecycle summary",
        estimator="snapshot count",
        unit="snapshots",
    )
    add(
        "setup.lifecycle.requests_per_trace",
        lifecycle["protocol"]["request_count"]
        / lifecycle["protocol"]["snapshot_count"],
        "d",
        section="setup",
        source="lifecycle summary",
        estimator="requests / snapshots",
        unit="requests/trace",
    )

    add(
        "setup.incremental.transitions",
        len(incremental_graph),
        "d",
        section="setup",
        source="incremental graph/vector JSONL",
        estimator="unique frozen commit transitions",
        unit="transitions",
    )
    add(
        "setup.incremental.repositories",
        len({row["instance_id"] for row in incremental_graph}),
        "d",
        section="setup",
        source="incremental graph JSONL",
        estimator="unique repository snapshots",
        unit="repositories",
    )
    add(
        "setup.incremental.graph_source_transitions",
        len(graph_source_rows),
        "d",
        section="setup",
        source="incremental graph JSONL",
        estimator="transitions with graph-scope source changes",
        unit="transitions",
    )
    add(
        "setup.incremental.vector_source_transitions",
        len(vector_source_rows),
        "d",
        section="setup",
        source="incremental vector JSONL",
        estimator="transitions with non-test vector-scope source changes",
        unit="transitions",
    )

    dense_query_ms = 1000.0 * dense["query_time"].to_numpy()
    add(
        "rq1.dense.mean_query_ms_range",
        [dense_query_ms.min(), dense_query_ms.max()],
        ".0f",
        section="RQ1",
        source="retrieval profiles",
        estimator="range of per-model mean query latency",
        unit="ms",
    )
    add(
        "rq1.dense.file_recall_range",
        [dense["recall_files"].min(), dense["recall_files"].max()],
        ".3f",
        section="RQ1",
        source="retrieval evaluation",
        estimator="range of per-model mean Recall@10",
        unit="fraction",
    )
    add(
        "rq1.dense.symbol_recall_range",
        [dense["recall_symbols"].min(), dense["recall_symbols"].max()],
        ".3f",
        section="RQ1",
        source="retrieval evaluation",
        estimator="range of per-model mean Recall@10",
        unit="fraction",
    )
    jina_rerank = _lookup_row(rerank, pipeline="Jina + Reranker-4B", k=50)
    jina_dense = _lookup_row(dense, label="Jina-Code-1.5B")
    add(
        "rq1.jina_rerank4b_k50.file_recall",
        jina_rerank["recall_files"],
        ".3f",
        section="RQ1",
        source="retrieval evaluation",
        estimator="mean file Recall@10",
        unit="fraction",
    )
    add(
        "rq1.jina_rerank4b_k50.query_s",
        jina_rerank["query_time"],
        ".2f",
        section="RQ1",
        source="retrieval profiles",
        estimator="mean end-to-end query latency",
        unit="s",
    )
    add(
        "rq1.jina_dense.file_recall",
        jina_dense["recall_files"],
        ".3f",
        section="RQ1",
        source="retrieval evaluation",
        estimator="mean file Recall@10",
        unit="fraction",
    )
    add(
        "rq1.jina_dense.query_ms",
        1000.0 * jina_dense["query_time"],
        ".0f",
        section="RQ1",
        source="retrieval profiles",
        estimator="mean end-to-end query latency",
        unit="ms",
    )
    best_symbol = rerank.loc[rerank["recall_symbols"].idxmax()]
    add(
        "rq1.best_symbol.recall",
        best_symbol["recall_symbols"],
        ".3f",
        section="RQ1",
        source="retrieval evaluation",
        estimator="maximum measured mean symbol Recall@10",
        unit="fraction",
    )
    add(
        "rq1.best_symbol.query_s",
        best_symbol["query_time"],
        ".1f",
        section="RQ1",
        source="retrieval profiles",
        estimator="mean latency at maximum symbol recall",
        unit="s",
    )
    add(
        "rq1.graph.selected_weight",
        select_graph_weight(graph_selection),
        ".1f",
        section="RQ1",
        source="development graph sweep",
        estimator="lexicographic File Success@10/5/1 selection",
        unit="RRF weight",
    )

    rng = np.random.default_rng(args.seed)
    graph_effects: dict[str, dict[str, tuple[float, float, float]]] = defaultdict(dict)
    for baseline, graph, label, _ in GRAPH_PAIRS:
        for data in graph_matrix:
            model = data["config"]["embedding_model"]
            graph_effects[label][model] = _paired_effect(
                data, baseline, graph, args.graph_bootstrap_samples, rng
            )
    no_reranker = list(graph_effects["No reranker"].values())
    point_effects = [row[0] for row in no_reranker]
    add(
        "rq1.graph.no_reranker_effect_range_pp",
        [min(point_effects), max(point_effects)],
        "+.1f",
        section="RQ1",
        source="held-out GraphRAG matrix",
        estimator="range of paired File Success@10 effects",
        unit="percentage points",
    )
    qwen06_effect = graph_effects["No reranker"]["Qwen/Qwen3-Embedding-0.6B"]
    add(
        "rq1.graph.qwen06_ci95_pp",
        list(qwen06_effect[1:]),
        "+.1f",
        section="RQ1",
        source="held-out GraphRAG matrix",
        estimator="repository-clustered paired percentile interval",
        unit="percentage points",
    )
    all_effects = [
        effect for group in graph_effects.values() for effect in group.values()
    ]
    add(
        "rq1.graph.all_model_intervals_include_zero",
        all(low <= 0 <= high for _, low, high in all_effects),
        "bool",
        section="RQ1",
        source="held-out GraphRAG matrix",
        estimator="all ten model-level intervals contain zero",
        unit="boolean",
    )
    interaction_rng = np.random.default_rng(args.seed)
    interaction_results = []
    for baseline, graph, _, _ in GRAPH_PAIRS:
        interaction_results.append(
            _pairwise_interaction_audit(
                graph_matrix,
                baseline,
                graph,
                args.graph_bootstrap_samples,
                interaction_rng,
            )
        )
    add(
        "rq1.graph.all_familywise_intervals_include_zero",
        not any(result["any_interval_excludes_zero"] for result in interaction_results),
        "bool",
        section="RQ1",
        source="held-out GraphRAG matrix",
        estimator="all simultaneous cross-embedding intervals contain zero",
        unit="boolean",
    )
    nonreranked_additions = [
        _latency(data, "dense_graph_fusion_w0p5", "median")
        - _latency(data, "dense", "median")
        for data in graph_matrix
    ]
    reranked_totals = [
        _latency(data, arm, "median") / 1000.0
        for data in graph_matrix
        for arm in ("dense_cross_encoder", "dense_graph_fusion_w0p5_cross_encoder")
    ]
    add(
        "rq1.graph.nonreranked_added_latency_ms_range",
        [min(nonreranked_additions), max(nonreranked_additions)],
        ".0f",
        section="RQ1",
        source="held-out GraphRAG matrix",
        estimator="range of per-model median graph-minus-dense latency",
        unit="ms",
    )
    add(
        "rq1.graph.reranked_total_latency_s_range",
        [min(reranked_totals), max(reranked_totals)],
        ".1f",
        section="RQ1",
        source="held-out GraphRAG matrix",
        estimator="range of reranked-arm median total latency",
        unit="s",
    )

    add(
        "rq2.profile.paired_profiles",
        len(build),
        "d",
        section="RQ2",
        source="construction profiles",
        estimator="snapshot-model pairs",
        unit="profiles",
    )
    add(
        "rq2.profile.construction_measurements",
        2 * len(build),
        "d",
        section="RQ2",
        source="construction profiles",
        estimator="L0 plus L2 measurements",
        unit="measurements",
    )
    medians_l0 = build.groupby("model")["time_l0"].median()
    medians_l2 = build.groupby("model")["time_l2"].median()
    per_snapshot_ratios = (
        build.assign(ratio=build["time_l2"] / build["time_l0"])
        .groupby("model")["ratio"]
        .median()
    )
    query_medians = query.groupby("model")["query_ms"].median()
    l0_slopes = _linear_slopes(build, "time_l0")
    l2_slopes = _linear_slopes(build, "time_l2")
    scale_labels = [row["display_label"] for row in model_scale]
    parameter_counts = [float(row["stored_parameter_count"]) for row in model_scale]
    output_dimensions = [float(row["output_dimension"]) for row in model_scale]
    ordered_l0_slopes = [l0_slopes[label] for label in scale_labels]
    ordered_l2_slopes = [l2_slopes[label] for label in scale_labels]
    ordered_query_medians = [float(query_medians[label]) for label in scale_labels]
    add(
        "rq2.profile.l0_median_s_range",
        [medians_l0.min(), medians_l0.max()],
        ".1f",
        section="RQ2",
        source="construction profiles",
        estimator="range of per-model medians",
        unit="s",
    )
    add(
        "rq2.profile.l2_median_s_range",
        [medians_l2.min(), medians_l2.max()],
        ".1f",
        section="RQ2",
        source="construction profiles",
        estimator="range of per-model medians",
        unit="s",
    )
    add(
        "rq2.profile.per_snapshot_l2_l0_median_ratio_range",
        [per_snapshot_ratios.min(), per_snapshot_ratios.max()],
        ".1f",
        section="RQ2",
        source="construction profiles",
        estimator="range of per-model medians of paired L2/L0 ratios",
        unit="ratio",
    )
    add(
        "rq2.profile.query_median_ms_range",
        [query_medians.min(), query_medians.max()],
        ".1f",
        section="RQ2",
        source="query profiles",
        estimator="range of per-model medians",
        unit="ms",
    )
    add(
        "rq2.profile.qwen06_query_median_ms",
        query_medians[MODEL_LABELS["Qwen/Qwen3-Embedding-0.6B"]],
        ".1f",
        section="RQ2",
        source="query profiles",
        estimator="Qwen3-Embedding-0.6B median complete dense-query latency",
        unit="ms",
    )
    add(
        "rq2.profile.parameter_count_b_range",
        [min(parameter_counts) / 1e9, max(parameter_counts) / 1e9],
        ".3f",
        section="RQ2",
        source="revision-bound model scale audit",
        estimator="range of stored safetensors parameter counts",
        unit="billion parameters",
    )
    add(
        "rq2.profile.output_dimension_range",
        [min(output_dimensions), max(output_dimensions)],
        "d",
        section="RQ2",
        source="revision-bound model scale audit",
        estimator="range of configured embedding output dimensions",
        unit="dimensions",
    )
    add(
        "rq2.profile.l0_slope_s_per_kloc_range",
        [min(ordered_l0_slopes), max(ordered_l0_slopes)],
        ".3f",
        section="RQ2",
        source="construction profiles",
        estimator="range of per-model OLS slopes for L0 time on kLOC",
        unit="s/kLOC",
    )
    add(
        "rq2.profile.l2_slope_s_per_kloc_range",
        [min(ordered_l2_slopes), max(ordered_l2_slopes)],
        ".3f",
        section="RQ2",
        source="construction profiles",
        estimator="range of per-model OLS slopes for L2 time on kLOC",
        unit="s/kLOC",
    )
    add(
        "rq2.profile.all_loc_slopes_positive",
        all(value > 0 for value in ordered_l0_slopes + ordered_l2_slopes),
        "bool",
        section="RQ2",
        source="construction profiles",
        estimator="all ten per-model OLS slopes are positive",
        unit="boolean",
    )
    add(
        "rq2.profile.slopes_increase_with_parameter_count",
        _strictly_increasing(parameter_counts)
        and _strictly_increasing(ordered_l0_slopes)
        and _strictly_increasing(ordered_l2_slopes),
        "bool",
        section="RQ2",
        source="construction profiles plus revision-bound model scale audit",
        estimator="strict rank monotonicity of both slope sequences",
        unit="boolean",
    )
    add(
        "rq2.profile.query_medians_increase_with_parameter_count",
        _strictly_increasing(parameter_counts)
        and _strictly_increasing(ordered_query_medians),
        "bool",
        section="RQ2",
        source="query profiles plus revision-bound model scale audit",
        estimator="strict rank monotonicity of warm query medians",
        unit="boolean",
    )
    add(
        "rq2.profile.parameter_dimension_rank_aligned",
        _strictly_increasing(parameter_counts)
        and _strictly_increasing(output_dimensions),
        "bool",
        section="RQ2",
        source="revision-bound model scale audit",
        estimator="parameter count and output dimension have identical rank order",
        unit="boolean",
    )

    summary = faiss["summary"]
    flat = summary["methods"]["flat"]
    hnsw_name, hnsw = select_guardrailed_method(
        summary, "HNSW-Flat", args.accuracy_guardrail
    )
    ivf_name, ivf = select_guardrailed_method(
        summary, "IVF-Flat", args.accuracy_guardrail
    )
    if (hnsw_name, ivf_name) != ("hnsw-ef16", "ivf-025pct"):
        raise ValueError(
            f"unexpected guardrailed FAISS methods: {hnsw_name}, {ivf_name}"
        )
    add(
        "rq2.faiss.flat_mean_search_ms",
        flat["mean_search_ms"],
        ".3f",
        section="RQ2",
        source="FAISS ablation",
        estimator="mean across snapshots",
        unit="ms",
    )
    add(
        "rq2.faiss.flat_mean_task_recall",
        flat["mean_task_recall"],
        ".3f",
        section="RQ2",
        source="FAISS ablation",
        estimator="mean task Recall@10",
        unit="fraction",
    )
    add(
        "rq2.faiss.hnsw16_neighbor_recall",
        hnsw["mean_ann_recall_at_k"],
        ".3f",
        section="RQ2",
        source="FAISS ablation",
        estimator="mean neighbor Recall@10",
        unit="fraction",
    )
    add(
        "rq2.faiss.hnsw16_mean_search_ms",
        hnsw["mean_search_ms"],
        ".4f",
        section="RQ2",
        source="FAISS ablation",
        estimator="mean across snapshots",
        unit="ms",
    )
    add(
        "rq2.faiss.hnsw16_speedup",
        flat["mean_search_ms"] / hnsw["mean_search_ms"],
        ".1f",
        section="RQ2",
        source="FAISS ablation",
        estimator="ratio of mean search latency",
        unit="ratio",
    )
    add(
        "rq2.faiss.ivf25_neighbor_recall",
        ivf["mean_ann_recall_at_k"],
        ".3f",
        section="RQ2",
        source="FAISS ablation",
        estimator="mean neighbor Recall@10",
        unit="fraction",
    )
    add(
        "rq2.faiss.ivf25_mean_search_ms",
        ivf["mean_search_ms"],
        ".3f",
        section="RQ2",
        source="FAISS ablation",
        estimator="mean across snapshots",
        unit="ms",
    )
    add(
        "rq2.faiss.ivf25_speedup",
        flat["mean_search_ms"] / ivf["mean_search_ms"],
        ".1f",
        section="RQ2",
        source="FAISS ablation",
        estimator="ratio of mean search latency",
        unit="ratio",
    )
    task_equal = all(
        instance["methods"][method]["task"] == instance["methods"]["flat"]["task"]
        for instance in faiss["instances"]
        for method in (hnsw_name, ivf_name)
    )
    add(
        "rq2.faiss.selected_task_outcomes_equal_flat",
        task_equal,
        "bool",
        section="RQ2",
        source="FAISS ablation",
        estimator="all selected-method per-snapshot task outcomes equal Flat",
        unit="boolean",
    )
    add(
        "rq2.faiss.hnsw_absolute_saving_ms",
        flat["mean_search_ms"] - hnsw["mean_search_ms"],
        ".3f",
        section="RQ2",
        source="FAISS ablation",
        estimator="difference of mean search latency",
        unit="ms",
    )
    add(
        "rq2.faiss.flat_build_ms",
        flat["mean_build_ms"],
        ".1f",
        section="RQ2",
        source="FAISS ablation",
        estimator="mean index-only build latency",
        unit="ms",
    )
    add(
        "rq2.faiss.hnsw_build_s",
        hnsw["mean_build_ms"] / 1000.0,
        ".2f",
        section="RQ2",
        source="FAISS ablation",
        estimator="mean index-only build latency",
        unit="s",
    )
    add(
        "rq2.faiss.ivf_build_s",
        ivf["mean_build_ms"] / 1000.0,
        ".3f",
        section="RQ2",
        source="FAISS ablation",
        estimator="mean index-only build latency",
        unit="s",
    )
    add(
        "rq2.faiss.size_mib",
        [flat["mean_index_mb"], hnsw["mean_index_mb"], ivf["mean_index_mb"]],
        ".2f",
        section="RQ2",
        source="FAISS ablation",
        estimator="mean serialized size, Flat/HNSW/IVF",
        unit="MiB",
    )

    coverage_total = sum(total for _, total in replay.coverage.values())
    coverage_matches = sum(matched for matched, _ in replay.coverage.values())
    definition = [
        value
        for (language, capability), value in replay.coverage.items()
        if capability == "definition"
    ]
    references = [
        value
        for (language, capability), value in replay.coverage.items()
        if capability == "references"
    ]
    add(
        "rq3.requests.total",
        coverage_total,
        "d",
        section="RQ3",
        source="LSP replay reports",
        estimator="unique graph-anchored request count",
        unit="requests",
    )
    add(
        "rq3.requests.matches",
        coverage_matches,
        "d",
        section="RQ3",
        source="LSP replay reports",
        estimator="unique requests with stable matching fingerprints",
        unit="requests",
    )
    add(
        "rq3.requests.match_rate",
        coverage_matches / coverage_total,
        ".1%",
        section="RQ3",
        source="LSP replay reports",
        estimator="matched unique requests / all unique requests",
        unit="percent",
    )
    add(
        "rq3.requests.definition_matches",
        sum(value[0] for value in definition),
        "d",
        section="RQ3",
        source="LSP replay reports",
        estimator="matched definition requests",
        unit="requests",
    )
    add(
        "rq3.requests.definition_match_rate",
        sum(matched for matched, _ in definition)
        / sum(total for _, total in definition),
        ".1%",
        section="RQ3",
        source="LSP replay reports",
        estimator="matched definition requests / sampled definition requests",
        unit="percent",
    )
    add(
        "rq3.requests.reference_matches",
        sum(value[0] for value in references),
        "d",
        section="RQ3",
        source="LSP replay reports",
        estimator="matched reference requests",
        unit="requests",
    )
    add(
        "rq3.requests.reference_match_rate",
        sum(matched for matched, _ in references)
        / sum(total for _, total in references),
        ".1%",
        section="RQ3",
        source="LSP replay reports",
        estimator="matched reference requests / sampled reference requests",
        unit="percent",
    )
    add(
        "rq3.requests.nonempty_matches",
        _lsp_nonempty_matched_requests(reports),
        "d",
        section="RQ3",
        source="LSP replay reports",
        estimator="matched unique requests nonempty in both providers",
        unit="requests",
    )
    add(
        "rq3.measurements.matched_rows",
        replay.matched_row_count,
        "d",
        section="RQ3",
        source="LSP replay reports",
        estimator="matched requests x measured repetitions",
        unit="rows",
    )
    add(
        "rq3.measurements.static_p50_ms",
        _median(replay.static_all),
        ".2f",
        section="RQ3",
        source="LSP replay reports",
        estimator="median of per-request repetition medians",
        unit="ms",
    )
    add(
        "rq3.measurements.live_p50_ms",
        _median(replay.live_all),
        ".2f",
        section="RQ3",
        source="LSP replay reports",
        estimator="median of per-request repetition medians",
        unit="ms",
    )
    add(
        "rq3.measurements.paired_saving_p50_ms",
        _median(replay.saving_all),
        ".2f",
        section="RQ3",
        source="LSP replay reports",
        estimator="median per-request live-minus-static median",
        unit="ms",
    )
    add(
        "rq3.measurements.live_static_ratio_p50",
        _median(replay.speedup_all),
        ".2f",
        section="RQ3",
        source="LSP replay reports",
        estimator="median per-request live/static ratio",
        unit="ratio",
    )
    add(
        "rq3.definition_match_rate_range",
        [min(m / n for m, n in definition), max(m / n for m, n in definition)],
        ".0%",
        section="RQ3",
        source="LSP replay reports",
        estimator="range across languages",
        unit="percent",
    )
    add(
        "rq3.reference_match_rate_range",
        [min(m / n for m, n in references), max(m / n for m, n in references)],
        ".0%",
        section="RQ3",
        source="LSP replay reports",
        estimator="range across languages",
        unit="percent",
    )
    add(
        "rq3.all_request_guardrail_pass",
        coverage_matches == coverage_total,
        "bool",
        section="RQ3",
        source="LSP replay reports",
        estimator="all-request fingerprint match",
        unit="boolean",
    )

    graph_symbol_speedups = [
        float(row["equivalence_guarded_speedup_vs_fully_rebuild"])
        for row in graph_source_rows
        if row.get("equivalence_guarded_speedup_vs_fully_rebuild") is not None
    ]
    graph_file_speedups = [
        float(row["file_level_equivalence_guarded_speedup_vs_fully_rebuild"])
        for row in graph_source_rows
        if row.get("file_level_equivalence_guarded_speedup_vs_fully_rebuild")
        is not None
    ]
    graph_joint_ratios = [
        float(row["equivalence_guarded_speedup_vs_file_level_patch"])
        for row in graph_source_rows
        if row.get("equivalence_guarded_speedup_vs_file_level_patch") is not None
    ]
    vector_speedups = [
        float(row["equivalence_guarded_speedup_vs_fully_rebuild"])
        for row in vector_source_rows
        if row.get("analysis_admitted") is True
    ]
    add(
        "rq4.graph.symbol_strict_passes",
        len(graph_symbol_speedups),
        "d",
        section="RQ4",
        source="incremental graph JSONL",
        estimator="source-changing transitions passing the symbol guard",
        unit="transitions",
    )
    add(
        "rq4.graph.file_strict_passes",
        len(graph_file_speedups),
        "d",
        section="RQ4",
        source="incremental graph JSONL",
        estimator="source-changing transitions passing the file guard",
        unit="transitions",
    )
    graph_symbol_quartiles = np.quantile(graph_symbol_speedups, (0.25, 0.5, 0.75))
    add(
        "rq4.graph.symbol_speedup_p50",
        graph_symbol_quartiles[1],
        ".2f",
        section="RQ4",
        source="incremental graph JSONL",
        estimator="median admitted amortized rebuild/update ratio",
        unit="ratio",
    )
    add(
        "rq4.graph.symbol_speedup_iqr",
        [graph_symbol_quartiles[0], graph_symbol_quartiles[2]],
        ".2f",
        section="RQ4",
        source="incremental graph JSONL",
        estimator="IQR of admitted amortized rebuild/update ratios",
        unit="ratio",
    )
    add(
        "rq4.graph.file_speedup_p50",
        _median(graph_file_speedups),
        ".2f",
        section="RQ4",
        source="incremental graph JSONL",
        estimator="median admitted amortized rebuild/update ratio",
        unit="ratio",
    )
    add(
        "rq4.graph.joint_passes",
        len(graph_joint_ratios),
        "d",
        section="RQ4",
        source="incremental graph JSONL",
        estimator="transitions jointly admitted for symbol and file arms",
        unit="transitions",
    )
    add(
        "rq4.graph.symbol_over_file_p50",
        _median(graph_joint_ratios),
        ".2f",
        section="RQ4",
        source="incremental graph JSONL",
        estimator="median admitted file/symbol amortized-time ratio",
        unit="ratio",
    )
    add(
        "rq4.vector.strict_passes",
        len(vector_speedups),
        "d",
        section="RQ4",
        source="incremental vector JSONL",
        estimator="source-changing transitions passing artifact and replay guards",
        unit="transitions",
    )
    vector_quartiles = np.quantile(vector_speedups, (0.25, 0.5, 0.75))
    add(
        "rq4.vector.speedup_p50",
        vector_quartiles[1],
        ".2f",
        section="RQ4",
        source="incremental vector JSONL",
        estimator="median admitted rebuild/update ratio",
        unit="ratio",
    )
    add(
        "rq4.vector.speedup_iqr",
        [vector_quartiles[0], vector_quartiles[2]],
        ".2f",
        section="RQ4",
        source="incremental vector JSONL",
        estimator="IQR of admitted rebuild/update ratios",
        unit="ratio",
    )
    vector_artifact_exact = sum(
        row["fidelity"]["levels"]["l2"]["document_identities_exact"] is True
        and row["fidelity"]["levels"]["l2"]["vectors_close"] is True
        for row in vector_source_rows
    )
    vector_ordered_exact = sum(
        row["fidelity"]["levels"]["l2"]["replay"]["ordered_exact_fraction"] == 1
        for row in vector_source_rows
    )
    vector_set_exact = sum(
        row["fidelity"]["levels"]["l2"]["replay"]["set_exact_fraction"] == 1
        for row in vector_source_rows
    )
    for claim_id, value, estimator in (
        (
            "rq4.vector.artifact_exact",
            vector_artifact_exact,
            "exact document identities and close vectors",
        ),
        (
            "rq4.vector.ordered_replay_exact",
            vector_ordered_exact,
            "exact ordered top-k replay",
        ),
        (
            "rq4.vector.set_replay_exact",
            vector_set_exact,
            "exact set top-k replay",
        ),
    ):
        add(
            claim_id,
            value,
            "d",
            section="RQ4",
            source="incremental vector JSONL",
            estimator=estimator,
            unit="transitions",
        )

    stability = incremental_summary["graph"]["provider_stability"]
    add(
        "rq4.stability.comparisons",
        stability["comparisons"],
        "d",
        section="RQ4",
        source="incremental summary",
        estimator="independent same-commit rebuild comparisons",
        unit="comparisons",
    )
    add(
        "rq4.stability.edge_f1_p50_percent",
        100 * stability["edge_f1"]["p50"],
        ".2f",
        section="RQ4",
        source="incremental summary",
        estimator="median fresh/fresh edge F1",
        unit="percent",
    )
    add(
        "rq4.stability.behavior_p50_percent",
        100 * stability["behavior_agreement"]["p50"],
        ".2f",
        section="RQ4",
        source="incremental summary",
        estimator="median fresh/fresh serving agreement",
        unit="percent",
    )
    add(
        "rq4.stability.changed_exact",
        stability["changed_exact"],
        "d",
        section="RQ4",
        source="incremental summary",
        estimator="fresh/fresh comparisons exact on changed scope",
        unit="comparisons",
    )

    for language in INCREMENTAL_LANGUAGES:
        add(
            f"rq4.table.graph.{language}",
            _graph_language_table_row(graph_source_rows, language),
            "str",
            section="RQ4",
            source="incremental graph JSONL",
            estimator="formatted appendix language row",
            unit="n|guard|p50|p25|p75|edge-f1-percent|behavior-percent",
        )
        add(
            f"rq4.table.vector.{language}",
            _vector_language_table_row(vector_source_rows, language),
            "str",
            section="RQ4",
            source="incremental vector JSONL",
            estimator="formatted appendix language row",
            unit="n|guard|p50|p25|p75|artifact-exact|replay-exact",
        )

    overall = lifecycle["overall"]
    add(
        "lifecycle.materialization_median_s",
        overall["materialization_seconds"]["median"],
        ".1f",
        section="lifecycle",
        source="lifecycle summary",
        estimator="median across snapshot summaries",
        unit="s",
    )
    add(
        "lifecycle.materialization_ci95_s",
        [
            overall["materialization_seconds"]["ci95_low"],
            overall["materialization_seconds"]["ci95_high"],
        ],
        ".1f",
        section="lifecycle",
        source="lifecycle summary",
        estimator="snapshot-level percentile bootstrap interval",
        unit="s",
    )
    add(
        "lifecycle.hydration_median_s",
        overall["load_seconds"]["median"],
        ".2f",
        section="lifecycle",
        source="lifecycle summary",
        estimator="median across snapshot summaries",
        unit="s",
    )
    add(
        "lifecycle.hydration_ci95_s",
        [overall["load_seconds"]["ci95_low"], overall["load_seconds"]["ci95_high"]],
        ".2f",
        section="lifecycle",
        source="lifecycle summary",
        estimator="snapshot-level percentile bootstrap interval",
        unit="s",
    )
    add(
        "lifecycle.service_median_s",
        overall["service_seconds"]["median"],
        ".3f",
        section="lifecycle",
        source="lifecycle summary",
        estimator="median across snapshot summaries",
        unit="s",
    )
    add(
        "lifecycle.service_ci95_s",
        [
            overall["service_seconds"]["ci95_low"],
            overall["service_seconds"]["ci95_high"],
        ],
        ".3f",
        section="lifecycle",
        source="lifecycle summary",
        estimator="snapshot-level percentile bootstrap interval",
        unit="s",
    )
    per_index = overall["per_index_seconds"]
    add(
        "lifecycle.view_build_median_s",
        [per_index[key]["median"] for key in ("bm25", "symbol_graph", "vector")],
        ".2f",
        section="lifecycle",
        source="lifecycle summary",
        estimator="median across snapshots",
        unit="s",
    )
    add(
        "lifecycle.artifact_median_mib",
        overall["artifact_bytes"]["median"] / 2**20,
        ".1f",
        section="lifecycle",
        source="lifecycle summary",
        estimator="median serialized artifact size",
        unit="MiB",
    )
    rows = lifecycle["snapshot_rows"]
    add(
        "lifecycle.max_vector_build_s",
        max(row["per_index_seconds"]["vector"] for row in rows),
        ".0f",
        section="lifecycle",
        source="lifecycle summary",
        estimator="maximum snapshot vector build",
        unit="s",
    )
    add(
        "lifecycle.max_graph_build_s",
        max(row["per_index_seconds"]["symbol_graph"] for row in rows),
        ".0f",
        section="lifecycle",
        source="lifecycle summary",
        estimator="maximum snapshot graph build",
        unit="s",
    )
    curves = {row["consumer_count"]: row for row in lifecycle["amortization_curve"]}
    q20 = curves[20]["deployment_models"]
    q100 = curves[100]["deployment_models"]
    isolated20 = q20["artifact_shared_process_isolated"][
        "effective_seconds_per_consumer"
    ]
    resident20 = q20["shared_resident_runtime"]["effective_seconds_per_consumer"]
    add(
        "lifecycle.q20.sessions",
        curves[20]["consumer_count"],
        "d",
        section="lifecycle",
        source="lifecycle projection",
        estimator="configured source-query session count",
        unit="sessions",
    )
    add(
        "lifecycle.q20.process_isolated_s",
        isolated20["median"],
        ".2f",
        section="lifecycle",
        source="lifecycle projection",
        estimator="median projected effective time/session",
        unit="s/session",
    )
    add(
        "lifecycle.q20.process_isolated_ci95_s",
        [isolated20["ci95_low"], isolated20["ci95_high"]],
        ".2f",
        section="lifecycle",
        source="lifecycle projection",
        estimator="snapshot-level percentile bootstrap interval",
        unit="s/session",
    )
    add(
        "lifecycle.q20.shared_resident_s",
        resident20["median"],
        ".2f",
        section="lifecycle",
        source="lifecycle projection",
        estimator="median projected effective time/session",
        unit="s/session",
    )
    add(
        "lifecycle.q20.shared_resident_ci95_s",
        [resident20["ci95_low"], resident20["ci95_high"]],
        ".2f",
        section="lifecycle",
        source="lifecycle projection",
        estimator="snapshot-level percentile bootstrap interval",
        unit="s/session",
    )
    add(
        "lifecycle.q100.projected_s",
        [
            q100["artifact_shared_process_isolated"]["effective_seconds_per_consumer"][
                "median"
            ],
            q100["shared_resident_runtime"]["effective_seconds_per_consumer"]["median"],
        ],
        ".2f",
        section="lifecycle",
        source="lifecycle projection",
        estimator="process-isolated/shared-resident medians",
        unit="s/session",
    )
    add(
        "lifecycle.q100.sessions",
        curves[100]["consumer_count"],
        "d",
        section="lifecycle",
        source="lifecycle projection",
        estimator="configured source-query session count",
        unit="sessions",
    )

    model_order = AGENT_MODEL_ORDER
    model_all = [agent["models"][model]["All"] for model in model_order]
    add(
        "rq5.baseline_mean_tokens_k",
        [row["grep_mean_tokens"] / 1000.0 for row in model_all],
        ".1f",
        section="RQ5",
        source="agent summary",
        estimator="mean trajectory tokens per query",
        unit="thousand tokens/query",
    )
    add(
        "rq5.baseline_mean_recall",
        [row["grep_block_recall_at_5"] for row in model_all],
        ".3f",
        section="RQ5",
        source="agent summary",
        estimator="mean committed-answer block Recall@5",
        unit="fraction",
    )
    selections = agent["operating_point_selection"]["models"]
    selected_tokens = [
        selections[model]["selected_token_percent_of_grep"] for model in model_order
    ]
    add(
        "rq5.selected_token_percent_range",
        [min(selected_tokens), max(selected_tokens)],
        ".1f",
        section="RQ5",
        source="agent summary",
        estimator="range of per-model selected-arm token percentages",
        unit="percent of baseline",
    )
    haiku = selections["Claude Haiku 4.5"]
    add(
        "rq5.haiku.selected_arm",
        haiku["selected_arm"],
        "str",
        section="RQ5",
        source="agent summary",
        estimator="minimum-token arm clearing the common quality margin",
        unit="policy",
    )
    add(
        "rq5.haiku.selected_token_percent",
        haiku["selected_token_percent_of_grep"],
        ".1f",
        section="RQ5",
        source="agent summary",
        estimator="mean paired token percentage",
        unit="percent of baseline",
    )
    add(
        "rq5.haiku.selected_token_ci95",
        agent["models"]["Claude Haiku 4.5"]["All"]["arms"]["eager"][
            "token_percent_ci95"
        ],
        ".1f",
        section="RQ5",
        source="agent summary",
        estimator="snapshot-clustered percentile interval",
        unit="percent of baseline",
    )
    add(
        "rq5.haiku.selected_recall_delta",
        haiku["selected_block_recall_at_5_delta"],
        "+.3f",
        section="RQ5",
        source="agent summary",
        estimator="paired mean Recall@5 change",
        unit="fraction",
    )
    add(
        "rq5.haiku.selected_recall_delta_ci95",
        haiku["selected_block_recall_at_5_delta_ci95"],
        "+.3f",
        section="RQ5",
        source="agent summary",
        estimator="snapshot-clustered percentile interval",
        unit="fraction",
    )
    add(
        "rq5.haiku.compact_token_percent",
        agent["models"]["Claude Haiku 4.5"]["All"]["arms"]["compact"][
            "token_percent_of_grep"
        ],
        ".1f",
        section="RQ5",
        source="agent summary",
        estimator="mean paired token percentage",
        unit="percent of baseline",
    )
    for model, prefix in (
        ("Qwen3.5-9B", "qwen9"),
        ("Qwen3.5-27B", "qwen27"),
        ("Gemma 4-12B", "gemma"),
        ("Gemini 2.5 Flash", "gemini"),
    ):
        compact = agent["models"][model]["All"]["arms"]["compact"]
        add(
            f"rq5.{prefix}.compact_token_percent",
            compact["token_percent_of_grep"],
            ".1f",
            section="RQ5",
            source="agent summary",
            estimator="mean paired token percentage",
            unit="percent of baseline",
        )
        add(
            f"rq5.{prefix}.compact_recall_delta",
            compact["block_recall_at_5_delta"],
            "+.3f",
            section="RQ5",
            source="agent summary",
            estimator="paired mean Recall@5 change",
            unit="fraction",
        )
    add(
        "rq5.qwen27.eager_lower_recall_bound",
        agent["models"]["Qwen3.5-27B"]["All"]["arms"]["eager"][
            "block_recall_at_5_delta_ci95"
        ][0],
        "+.3f",
        section="RQ5",
        source="agent summary",
        estimator="lower snapshot-clustered interval bound",
        unit="fraction",
    )
    add(
        "rq5.recall_delta_guardrail",
        agent["recall_guardrail"],
        ".2f",
        section="RQ5",
        source="agent summary",
        estimator="configured lower interval bound for paired Recall@5 change",
        unit="fraction",
    )
    contrasts = [
        agent["policy_contrasts"][model]["compact_vs_eager"] for model in model_order
    ]
    add(
        "rq5.compact_eager_token_percent",
        [row["token_percent_of_eager"] for row in contrasts],
        ".1f",
        section="RQ5",
        source="agent summary",
        estimator="mean paired compact/eager token ratio",
        unit="percent",
    )
    add(
        "rq5.compact_eager_token_ci95",
        [row["token_percent_ci95"] for row in contrasts],
        ".1f",
        section="RQ5",
        source="agent summary",
        estimator="snapshot-clustered paired compact/eager token intervals",
        unit="percent",
    )
    add(
        "rq5.compact_eager_recall_delta",
        [row["block_recall_at_5_delta"] for row in contrasts],
        "+.3f",
        section="RQ5",
        source="agent summary",
        estimator="paired compact-minus-eager Recall@5 change",
        unit="fraction",
    )
    add(
        "rq5.compact_eager_recall_delta_ci95",
        [row["block_recall_at_5_delta_ci95"] for row in contrasts],
        "+.3f",
        section="RQ5",
        source="agent summary",
        estimator="snapshot-clustered paired compact-minus-eager Recall@5 intervals",
        unit="fraction",
    )
    audit_arms = ("grep", "eager", "compact")
    forced_counts = [
        [
            agent["execution_audit"][model]["by_arm"][arm]["forced_final_answers"]
            for arm in audit_arms
        ]
        for model in model_order
    ]
    add(
        "rq5.forced_final_counts_by_arm",
        forced_counts,
        "d",
        section="RQ5",
        source="agent execution audit",
        estimator="forced-final trajectories by model and grep/eager/compact arm",
        unit="trajectories",
    )
    add(
        "rq5.forced_final_percent_by_arm",
        [
            [100.0 * count / agent["paired_query_count"] for count in row]
            for row in forced_counts
        ],
        ".1f",
        section="RQ5",
        source="agent execution audit",
        estimator="forced-final trajectories / paired queries by model and arm",
        unit="percent",
    )
    strata = [
        *[
            row
            for label, row in agent["pooled_strata"]["languages"].items()
            if label != "All"
        ],
        *agent["pooled_strata"]["query_categories"].values(),
    ]
    subgroup_tokens = [
        row["arms"][arm]["token_percent_of_grep"]
        for row in strata
        for arm in ("eager", "compact")
    ]
    add(
        "rq5.pooled_subgroup_token_percent_range",
        [min(subgroup_tokens), max(subgroup_tokens)],
        ".1f",
        section="RQ5",
        source="agent summary",
        estimator="range across pooled language/query-type arms",
        unit="percent of baseline",
    )
    return ledger


def main() -> None:
    args = parse_args()
    ledger = build_ledger(args)
    ledger.finish(
        args.output,
        {
            "graph_bootstrap_samples": args.graph_bootstrap_samples,
            "seed": args.seed,
            "accuracy_guardrail": args.accuracy_guardrail,
            "display_contract": "reviewed manuscript values embedded in build_paper_claims.py",
        },
    )


if __name__ == "__main__":
    main()
