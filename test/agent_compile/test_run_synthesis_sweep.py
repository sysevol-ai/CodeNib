# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the synthesis sweep's dataset-specific loading boundary."""

from __future__ import annotations

import datasets

import codeminer.dataset.codeminer_synthesis as synthesis_dataset
from scripts.agent_compile.run_synthesis_sweep import _load_normalized


def test_load_normalized_forwards_frozen_dataset_revision(monkeypatch):
    calls = {}

    def load_dataset(dataset, config_name, *, split, revision):
        calls.update(
            dataset=dataset,
            config_name=config_name,
            split=split,
            revision=revision,
        )
        return [{"query_id": "q1"}]

    monkeypatch.setattr(datasets, "load_dataset", load_dataset)
    monkeypatch.setattr(
        synthesis_dataset,
        "normalize_synthesis_record",
        lambda row, config_name: {**row, "source_config": config_name},
    )

    rows = _load_normalized(
        "Python",
        dataset="org/frozen-benchmark",
        split="test",
        revision="abc123",
    )

    assert calls == {
        "dataset": "org/frozen-benchmark",
        "config_name": "Python",
        "split": "test",
        "revision": "abc123",
    }
    assert rows == [{"query_id": "q1", "source_config": "Python"}]
