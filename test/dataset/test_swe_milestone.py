# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from codenib.dataset.swe_milestone import load_swe_milestone_cases


def test_selected_specification_does_not_expose_solution(tmp_path):
    (tmp_path / "srs").mkdir()
    (tmp_path / "srs/milestone_001_SRS.md").write_text("Implement libraries")
    (tmp_path / "metadata.json").write_text(
        json.dumps(
            {
                "repo_name": "navidrome/navidrome",
                "milestones": [
                    {
                        "id": "milestone_001",
                        "parent_milestones": [],
                        "commit_sha_end": "REFERENCE_NOT_FOR_PROMPT",
                    },
                    {"id": "milestone_002", "parent_milestones": []},
                ],
            }
        )
    )
    cases = load_swe_milestone_cases(tmp_path, instance_ids=["milestone_001"])
    assert len(cases) == 1
    assert cases[0].problem_statement == "Implement libraries"
    assert "REFERENCE_NOT_FOR_PROMPT" not in json.dumps(cases[0].prompt_record())
    with pytest.raises(ValueError, match="unknown milestones"):
        load_swe_milestone_cases(tmp_path, instance_ids=["missing"])


@pytest.mark.parametrize("identity", ["../escape", "/absolute", "a/b", ""])
def test_reject_invalid_identity(tmp_path, identity):
    (tmp_path / "metadata.json").write_text(
        json.dumps(
            {
                "repo_name": "owner/repo",
                "milestones": [{"id": identity}],
            }
        )
    )
    with pytest.raises(ValueError, match="identity"):
        load_swe_milestone_cases(tmp_path)
