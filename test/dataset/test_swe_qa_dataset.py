# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import os

import pytest

from codenib.dataset.swe_qa import load_swe_qa_cases


def test_loader_joins_commits_and_preserves_original_question_ids(tmp_path):
    (tmp_path / "repo_commit.txt").write_text("https://github.com/owner/repo abcdef1\n")
    (tmp_path / "Benchmark").mkdir()
    (tmp_path / "Benchmark/repo.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"question": "First?", "answer": "Reference one"},
                {"question": "Second?", "answer": "Reference two"},
            ]
        )
    )
    cases = load_swe_qa_cases(tmp_path, question_ids=["repo:2"], limit=1)
    assert len(cases) == 1
    case = cases[0]
    assert (case.instance_id, case.repo, case.base_commit) == (
        "repo:2",
        "owner/repo",
        "abcdef1",
    )
    assert case.question == "Second?" and case.reference_answer == "Reference two"
    with pytest.raises(ValueError, match="unknown question"):
        load_swe_qa_cases(tmp_path, question_ids=["repo:3"])
    with pytest.raises(ValueError, match="unknown repositories"):
        load_swe_qa_cases(tmp_path, repos=["other"])


@pytest.mark.integration
def test_actual_release_loads_all_tasks():
    checkout = os.getenv("SWE_QA_CHECKOUT")
    if not checkout:
        pytest.skip("set SWE_QA_CHECKOUT to the pinned upstream checkout")
    cases = load_swe_qa_cases(checkout)
    assert len(cases) == 720 and len({case.repo for case in cases}) == 15
    assert len({case.instance_id for case in cases}) == len(cases)
