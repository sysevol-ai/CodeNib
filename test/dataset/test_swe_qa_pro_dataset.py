# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import os

import pytest

from codenib.dataset.swe_qa_pro import load_swe_qa_pro_cases


def record(question="How?", repo="owner/repo"):
    return {
        "repo": repo,
        "commit_id": "a" * 40,
        "cluster": {"id": "1", "name": "Fixture"},
        "qa_type": {"class_name": "how", "sub_class_name": "behavior"},
        "question": question,
        "answer": "PRIVATE GOLD",
    }


def test_loader_keeps_nested_metadata_and_excludes_gold_from_task_export(tmp_path):
    path = tmp_path / "test.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [record(), record("Why?")]))
    (case,) = load_swe_qa_pro_cases(
        path, repos=["repo"], question_ids=["repo:2"], limit=1
    )
    assert case.repo == "owner/repo" and case.commit_id == "a" * 40
    assert case.qa_type == record()["qa_type"] and case.cluster == record()["cluster"]
    assert case.question == "Why?" and case.reference_answer == "PRIVATE GOLD"
    assert "PRIVATE GOLD" not in json.dumps(case.task_record())
    with pytest.raises(ValueError, match="unknown repositories"):
        load_swe_qa_pro_cases(path, repos=["absent"])
    with pytest.raises(ValueError, match="unknown question"):
        load_swe_qa_pro_cases(path, question_ids=["repo:3"])


def test_loader_rejects_missing_source_identity(tmp_path):
    path = tmp_path / "test.jsonl"
    path.write_text(json.dumps({**record(), "commit_id": "main"}))
    with pytest.raises(ValueError, match="commit_id"):
        load_swe_qa_pro_cases(path)


@pytest.mark.integration
def test_actual_pro_release_schema_and_counts():
    path = os.getenv("SWE_QA_PRO_DATA")
    if not path:
        pytest.skip("set SWE_QA_PRO_DATA to the pinned release's data/test.jsonl")
    cases = load_swe_qa_pro_cases(path)
    assert len(cases) == 260 and len({c.repo for c in cases}) == 26
    assert len({c.instance_id for c in cases}) == 260
    assert all(
        isinstance(c.cluster, dict) and isinstance(c.qa_type, dict) for c in cases
    )
