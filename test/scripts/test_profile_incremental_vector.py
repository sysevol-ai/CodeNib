# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "profiling" / "profile_incremental_vector.py"
SPEC = importlib.util.spec_from_file_location(
    "_profile_incremental_vector_for_tests", SCRIPT_PATH
)
assert SPEC is not None and SPEC.loader is not None
profile = importlib.util.module_from_spec(SPEC)
sys.modules["_profile_incremental_vector_for_tests"] = profile
SPEC.loader.exec_module(profile)


class FakeDocument:
    __slots__ = ("metadata", "page_content")

    def __init__(self, content, **metadata):
        self.page_content = content
        self.metadata = metadata


class FakeIndex:
    __slots__ = ("vectors",)

    def __init__(self, vectors):
        self.vectors = vectors

    def reconstruct(self, row):
        return self.vectors[row]


class FakeResult:
    __slots__ = (
        "end_line",
        "file",
        "node_id",
        "node_name",
        "score",
        "start_line",
        "type",
    )

    def __init__(self, document, score):
        metadata = document.metadata
        self.file = metadata.get("file", "")
        self.start_line = metadata.get("start_line", 0)
        self.end_line = metadata.get("end_line", 0)
        self.type = metadata.get("chunk_type", "")
        self.node_name = metadata.get("name", "")
        self.node_id = metadata.get("node_id", "")
        self.score = score


class FakeStore:
    __slots__ = ("documents", "index")

    def __init__(self, documents, vectors):
        self.documents = documents
        self.index = FakeIndex(vectors)

    def _get_index_and_docs(self, level):
        assert level == "l2"
        return self.index, self.documents

    def search(self, query, top_k, level):
        assert level == "l2"
        return [
            FakeResult(document, 1.0 - row / 10)
            for row, document in enumerate(self.documents[:top_k])
        ]


def _documents():
    return [
        FakeDocument(
            "def a(): pass",
            file="a.py",
            start_line=0,
            end_line=1,
            chunk_type="function",
            name="a",
            node_id="a.py:a()",
        ),
        FakeDocument(
            "def b(): pass",
            file="b.py",
            start_line=3,
            end_line=4,
            chunk_type="function",
            name="b",
            node_id="b.py:b()",
        ),
    ]


def test_artifact_guard_accepts_identical_flat_indexes():
    documents = _documents()
    vectors = [
        np.array([1.0, 0.0], dtype=np.float32),
        np.array([0.0, 1.0], dtype=np.float32),
    ]
    fresh = FakeStore(documents, vectors)
    incremental = FakeStore(list(documents), [vector.copy() for vector in vectors])

    result = profile.compare_artifacts(
        incremental,
        fresh,
        levels=["l2"],
        top_k=2,
        max_queries=2,
    )

    assert result["equivalence_guardrail_pass"] is True
    assert result["levels"]["l2"]["replay"]["set_exact_fraction"] == 1.0


def test_artifact_guard_rejects_vector_drift():
    documents = _documents()
    fresh = FakeStore(
        documents,
        [
            np.array([1.0, 0.0], dtype=np.float32),
            np.array([0.0, 1.0], dtype=np.float32),
        ],
    )
    incremental = FakeStore(
        documents,
        [
            np.array([0.8, 0.2], dtype=np.float32),
            np.array([0.0, 1.0], dtype=np.float32),
        ],
    )

    result = profile.compare_artifacts(
        incremental,
        fresh,
        levels=["l2"],
        top_k=2,
        max_queries=2,
    )

    assert result["equivalence_guardrail_pass"] is False
    assert result["levels"]["l2"]["vectors_close"] is False


def test_artifact_guard_rejects_top_k_order_drift():
    documents = _documents()
    vectors = [
        np.array([1.0, 0.0], dtype=np.float32),
        np.array([0.0, 1.0], dtype=np.float32),
    ]
    fresh = FakeStore(documents, vectors)
    incremental = FakeStore(
        list(reversed(documents)),
        list(reversed(vectors)),
    )

    result = profile.compare_artifacts(
        incremental,
        fresh,
        levels=["l2"],
        top_k=2,
        max_queries=2,
    )

    replay = result["levels"]["l2"]["replay"]
    assert replay["set_exact_fraction"] == 1.0
    assert replay["ordered_exact_fraction"] == 0.0
    assert result["equivalence_guardrail_pass"] is False


def test_arm_order_is_stable_and_counterbalanced():
    assert profile.maintenance_arm_order("repo", 1) == profile.maintenance_arm_order(
        "repo", 1
    )
    orders = {
        profile.maintenance_arm_order(f"repo-{index}", index) for index in range(100)
    }
    assert orders == {
        ("fully-rebuild", "incremental"),
        ("incremental", "fully-rebuild"),
    }


def test_resume_state_ignores_invalid_and_old_protocol_rows(tmp_path):
    config_id = "config-a"
    output = tmp_path / "results.jsonl"
    output.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "protocol_version": profile.PROTOCOL_VERSION,
                        "experiment_config_id": config_id,
                        "instance_id": "repo-a",
                        "step": 1,
                        "instance_transition_count": 2,
                        "fully-rebuild": {"status": "ok"},
                        "incremental": {"status": "ok"},
                        "fidelity": {"equivalence_guardrail_pass": True},
                    }
                ),
                json.dumps(
                    {
                        "protocol_version": profile.PROTOCOL_VERSION - 1,
                        "instance_id": "repo-a",
                        "step": 2,
                        "instance_transition_count": 2,
                    }
                ),
                "not-json",
            ]
        )
    )

    assert profile._completed_steps(output, config_id) == {"repo-a": {1}}
    assert profile._completed_instances(output, config_id) == set()


def test_resume_state_rejects_failed_or_inexact_rows(tmp_path):
    config_id = "config-a"
    output = tmp_path / "results.jsonl"
    rows = []
    for step, incremental_status, exact in (
        (1, "error", True),
        (2, "ok", False),
    ):
        rows.append(
            {
                "protocol_version": profile.PROTOCOL_VERSION,
                "experiment_config_id": config_id,
                "instance_id": "repo-a",
                "step": step,
                "instance_transition_count": 2,
                "fully-rebuild": {"status": "ok"},
                "incremental": {"status": incremental_status},
                "fidelity": {"equivalence_guardrail_pass": exact},
            }
        )
    output.write_text("\n".join(json.dumps(row) for row in rows))

    assert profile._completed_steps(output, config_id) == {}
    assert profile._completed_instances(output, config_id) == set()


def test_resume_state_rejects_mixed_experiment_configuration(tmp_path):
    output = tmp_path / "results.jsonl"
    output.write_text(
        json.dumps(
            {
                "protocol_version": profile.PROTOCOL_VERSION,
                "experiment_config_id": "config-a",
                "instance_id": "repo-a",
                "step": 1,
                "instance_transition_count": 1,
                "fully-rebuild": {"status": "ok"},
                "incremental": {"status": "ok"},
                "fidelity": {"equivalence_guardrail_pass": True},
            }
        )
    )

    assert profile._completed_steps(output, "config-b") == {}
    assert profile._completed_instances(output, "config-b") == set()


def test_experiment_config_id_is_canonical_and_sensitive():
    kwargs = {
        "model": "model",
        "provider": "huggingface",
        "dimension": 4,
        "embedding_kwargs": {"revision": "abc", "encode_kwargs": {"batch_size": 8}},
        "levels": ["l2"],
        "metric": "l2",
        "delta_threshold": 0.1,
        "top_k": 10,
        "replay_queries": 32,
        "runtime_log_level": "WARNING",
    }

    first = profile.experiment_config_id(**kwargs)
    reordered = profile.experiment_config_id(
        **{
            **kwargs,
            "embedding_kwargs": {
                "encode_kwargs": {"batch_size": 8},
                "revision": "abc",
            },
        }
    )
    changed = profile.experiment_config_id(
        **{**kwargs, "embedding_kwargs": {"revision": "different"}}
    )

    assert first == reordered
    assert first != changed
