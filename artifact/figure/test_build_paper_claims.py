#!/usr/bin/env python3
"""Regression tests for Figure 7 scale contracts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from build_paper_claims import (
    _linear_slopes,
    _strictly_increasing,
    _validate_agent_summary,
    _validate_incremental_evidence,
    load_model_scale_metadata,
)
from draw_agent_runtime import summarize_execution_audit
from plot_style import MODEL_DIMENSIONS, MODEL_IDS, MODEL_LABELS, MODEL_ORDER


class AgentSummaryContractTest(unittest.TestCase):
    def test_current_five_model_schema_is_required(self) -> None:
        models = {
            name: {}
            for name in (
                "Claude Haiku 4.5",
                "Qwen3.5-9B",
                "Qwen3.5-27B",
                "Gemma 4-12B",
                "Gemini 2.5 Flash",
            )
        }

        _validate_agent_summary({"schema_version": 11, "models": models})

        with self.assertRaisesRegex(ValueError, "schema version 11"):
            _validate_agent_summary({"schema_version": 10, "models": models})

        models.pop("Gemini 2.5 Flash")
        with self.assertRaisesRegex(ValueError, "Gemini 2.5 Flash"):
            _validate_agent_summary({"schema_version": 11, "models": models})


def _scale_inputs() -> tuple[dict, dict]:
    models = []
    cached_models = {}
    for position, model_id in enumerate(MODEL_IDS, start=1):
        revision = f"{position:x}" * 40
        weight_sha = f"{(position + 5) % 16:x}" * 64
        config_blob = f"{(position + 10) % 16:x}" * 40
        config_sha = f"{(position + 11) % 16:x}" * 64
        label = MODEL_LABELS[model_id]
        models.append(
            {
                "model_id": model_id,
                "display_label": label,
                "revision": revision,
                "output_dimension": MODEL_DIMENSIONS[label],
                "dimension_config_key": "hidden_size",
                "stored_parameter_count": position * 100,
                "tensor_count": position,
                "config": {
                    "path": "config.json",
                    "bytes": position,
                    "cache_blob_id": config_blob,
                    "sha256": config_sha,
                },
                "weights": [
                    {
                        "path": "model.safetensors",
                        "bytes": position * 10,
                        "sha256": weight_sha,
                    }
                ],
            }
        )
        cached_models[model_id] = {
            "snapshots": [
                {
                    "revision": revision,
                    "files": {
                        "config.json": {"blob": config_blob, "bytes": position},
                        "model.safetensors": {
                            "blob": weight_sha,
                            "bytes": position * 10,
                        },
                    },
                }
            ]
        }
    metadata = {
        "schema_version": 1,
        "source": "test",
        "parameter_count_method": "test",
        "models": models,
    }
    provenance = {"schema_version": 1, "models": cached_models}
    return metadata, provenance


class ModelScaleMetadataTest(unittest.TestCase):
    def _write(self, root: Path, name: str, payload: dict) -> Path:
        path = root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_metadata_is_bound_to_cache_provenance(self) -> None:
        metadata, provenance = _scale_inputs()
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            rows = load_model_scale_metadata(
                self._write(root, "scale.json", metadata),
                self._write(root, "provenance.json", provenance),
            )

        self.assertEqual([row["model_id"] for row in rows], list(MODEL_IDS))

    def test_weight_identity_drift_is_rejected(self) -> None:
        metadata, provenance = _scale_inputs()
        metadata["models"][0]["weights"][0]["sha256"] = "f" * 64
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            with self.assertRaisesRegex(ValueError, "weight identity"):
                load_model_scale_metadata(
                    self._write(root, "scale.json", metadata),
                    self._write(root, "provenance.json", provenance),
                )


class ScalingEstimatorTest(unittest.TestCase):
    def test_per_model_slopes_preserve_scale_order(self) -> None:
        records = []
        for position, model in enumerate(MODEL_ORDER, start=1):
            for loc in (1.0, 2.0, 4.0):
                records.append(
                    {
                        "model": model,
                        "total_loc_k": loc,
                        "time_l0": position * loc + 0.5,
                    }
                )

        slopes = _linear_slopes(pd.DataFrame(records), "time_l0")

        ordered = [slopes[model] for model in MODEL_ORDER]
        self.assertTrue(_strictly_increasing(ordered))
        self.assertAlmostEqual(ordered[0], 1.0)
        self.assertAlmostEqual(ordered[-1], 5.0)


class IncrementalEvidenceTest(unittest.TestCase):
    @staticmethod
    def _inputs() -> tuple[list[dict], list[dict], dict]:
        languages = ("go", "python", "rust", "ts")
        graph = []
        vector = []
        for repository in range(8):
            language = languages[repository // 2]
            for step in range(1, 6):
                identity = {
                    "instance_id": f"repo-{repository}",
                    "step": step,
                    "language": language,
                }
                graph.append(
                    {
                        **identity,
                        "protocol_version": 21,
                        "delta_files": 1,
                        "equivalence_guarded_speedup_vs_fully_rebuild": 2.0,
                        "file_level_equivalence_guarded_speedup_vs_fully_rebuild": 1.5,
                    }
                )
                vector.append(
                    {
                        **identity,
                        "protocol_version": 4,
                        "delta_files_test_excluded": 1,
                        "analysis_admitted": True,
                    }
                )
        summary = {
            "schema_version": 2,
            "graph": {
                "protocol_versions": [21],
                "completeness": {
                    "complete": True,
                    "expected_rows": 40,
                    "unique_transition_rows": 40,
                },
                "overall": {
                    "source_changing_rows": 40,
                    "symbol": {"admitted": 40},
                    "file": {"admitted": 40},
                },
            },
            "vector": {
                "protocol_versions": [4],
                "completeness": {
                    "complete": True,
                    "expected_rows": 40,
                    "unique_transition_rows": 40,
                },
                "overall": {
                    "source_changing_rows": 40,
                    "incremental": {"admitted": 40},
                },
            },
        }
        return graph, vector, summary

    def test_frozen_graph_and_vector_inputs_align(self) -> None:
        graph, vector, summary = self._inputs()

        graph_source, vector_source = _validate_incremental_evidence(
            graph, vector, summary
        )

        self.assertEqual(len(graph_source), 40)
        self.assertEqual(len(vector_source), 40)

    def test_protocol_drift_is_rejected(self) -> None:
        graph, vector, summary = self._inputs()
        vector[0]["protocol_version"] = 5

        with self.assertRaisesRegex(ValueError, "protocol"):
            _validate_incremental_evidence(graph, vector, summary)


class AgentExecutionAuditTest(unittest.TestCase):
    def test_execution_audit_retains_per_arm_counts(self) -> None:
        def cell(*, forced: bool) -> dict:
            return {
                "tool_calls": [],
                "trace_summary": {"forced_final_answer": forced},
            }

        paired = {
            "q1": {
                "grep": cell(forced=True),
                "eager": cell(forced=False),
                "compact": cell(forced=True),
            },
            "q2": {
                "grep": cell(forced=False),
                "eager": cell(forced=True),
                "compact": cell(forced=False),
            },
        }

        audit = summarize_execution_audit(paired)

        self.assertEqual(audit["forced_final_answers"], 3)
        self.assertEqual(audit["by_arm"]["grep"]["forced_final_answers"], 1)
        self.assertEqual(audit["by_arm"]["eager"]["forced_final_answers"], 1)
        self.assertEqual(audit["by_arm"]["compact"]["forced_final_answers"], 1)


if __name__ == "__main__":
    unittest.main()
