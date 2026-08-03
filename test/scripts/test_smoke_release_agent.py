# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from scripts.smoke_release_agent import _assert_chat_response


def _response(skill_id: str) -> dict:
    return {
        "answer": "The function returns the sum of its two inputs.",
        "total_turns": 3,
        "tool_calls": [
            {
                "skill_id": skill_id,
                "result_count": 1,
                "error": None,
            }
        ],
        "citations": [
            {
                "file": "calculator.py",
                "start_line": 1,
            }
        ],
    }


def test_release_smoke_accepts_the_agent_search_contract():
    _assert_chat_response(_response("repository_search"))


def test_release_smoke_rejects_the_retired_bm25_tool():
    with pytest.raises(RuntimeError, match="grounded repository retrieval"):
        _assert_chat_response(_response("bm25_search"))
