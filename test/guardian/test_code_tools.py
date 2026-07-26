# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for Guardian's model-facing source search."""

from types import SimpleNamespace

from codeminer.guardian.loop.tools_code import search_code


def test_multi_term_query_matches_individual_source_terms(tmp_path):
    (tmp_path / "model.py").write_text(
        "def evaluate(model_path):\n" "    return _load_model(model_path)\n",
        encoding="utf-8",
    )

    result = search_code(
        str(tmp_path),
        "evaluate model_path default_model_path _load_model",
        retriever=None,
        top_k=10,
    )

    assert "model.py:1" in result
    assert "model.py:2" in result
    assert result.index("model.py:1") < result.index("model.py:2")


def test_retriever_failure_is_visible_without_disabling_source_search(tmp_path):
    (tmp_path / "model.py").write_text(
        "def prepare_predict_data():\n    return []\n",
        encoding="utf-8",
    )

    class BrokenRetriever:
        def query(self, query, top_k=None):
            raise RuntimeError("corrupt index")

    result = search_code(
        str(tmp_path),
        "prepare_predict_data",
        retriever=BrokenRetriever(),
    )

    assert "Symbol index unavailable (RuntimeError: corrupt index)" in result
    assert "model.py:1" in result


def test_source_matches_rank_above_copied_logs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "src" / "contract.py").write_text(
        "return train_test_split(values)\n", encoding="utf-8"
    )
    (tmp_path / "data" / "guardian.log").write_text(
        "return train_test_split(values)\n", encoding="utf-8"
    )

    result = search_code(
        str(tmp_path),
        "return train_test_split",
        top_k=1,
    )

    assert result.startswith("src/contract.py:1:")


def test_symbol_only_result_remains_direct_and_readable(tmp_path):
    result = search_code(
        str(tmp_path),
        "contract",
        retriever=SimpleNamespace(query=lambda query, top_k=None: ["symbol"]),
    )

    assert result == "symbol"
