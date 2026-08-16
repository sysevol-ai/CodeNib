# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import pickle

from codenib.graph.code_graph import (
    CodeGraph,
    current_graph_schema_version,
    persisted_graph_schema_version,
)


def test_persisted_graph_schema_version_reads_current_graph_without_loading(tmp_path):
    path = tmp_path / "graph.pkl"
    CodeGraph().save_graph(path)

    assert persisted_graph_schema_version(path) == current_graph_schema_version()


def test_persisted_graph_schema_version_reports_old_and_invalid_pickles(tmp_path):
    old_path = tmp_path / "old.pkl"
    with old_path.open("wb") as handle:
        pickle.dump({"schema_version": 4, "graph": "not loaded"}, handle)
    invalid_path = tmp_path / "invalid.pkl"
    invalid_path.write_bytes(b"not a pickle")

    assert persisted_graph_schema_version(old_path) == 4
    assert persisted_graph_schema_version(invalid_path) is None
