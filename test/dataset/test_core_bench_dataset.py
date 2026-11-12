# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import json

import pytest

from codenib.dataset.core_bench import load_core_corpus, load_core_qrels


def write_inputs(path):
    (path / "corpus.jsonl").write_text(
        '{"_id":"a","text":"tax helper"}\n{"_id":"b","text":"invoice"}\n'
    )
    (path / "queries.jsonl").write_text(
        '{"_id":"q1","text":"tax","filtered_corpus_id":["a"]}\n'
        '{"_id":"q2","text":"invoice","filtered_corpus_id":["b"]}\n'
    )


def test_core_loads_inputs_without_annotations(tmp_path):
    write_inputs(tmp_path)
    data = load_core_corpus(tmp_path, query_ids=["q2"], limit=1)
    assert data.document_ids == ("a", "b")
    assert data.queries[0].query_id == "q2"
    assert data.queries[0].candidates == ("b",)
    assert not (tmp_path / "qrels.jsonl").exists()


def test_core_rejects_candidates_from_another_corpus(tmp_path):
    write_inputs(tmp_path)
    (tmp_path / "queries.jsonl").write_text(
        json.dumps(
            {
                "_id": "q",
                "text": "tax",
                "filtered_corpus_id": ["future"],
            }
        )
    )
    with pytest.raises(ValueError, match="allowlist"):
        load_core_corpus(tmp_path)


def test_core_selection_rejects_unknown_queries(tmp_path):
    write_inputs(tmp_path)
    with pytest.raises(ValueError, match="unknown queries"):
        load_core_corpus(tmp_path, query_ids=["absent"])


def test_core_annotations_stay_in_separate_loader(tmp_path):
    (tmp_path / "qrels.jsonl").write_text(
        '{"query_id":"q","corpus_id":"a","score":1}\n'
        '{"query_id":"q","corpus_id":"b","score":0}\n'
    )
    assert load_core_qrels(tmp_path) == {"q": {"a"}}
