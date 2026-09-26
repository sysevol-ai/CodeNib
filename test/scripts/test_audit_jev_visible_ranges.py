# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import pytest

from scripts.audit_jev_visible_ranges import corrected_node
from scripts.benchmark_jev_base import metrics


@pytest.mark.parametrize(
    ("kind", "prefix"),
    [("function", "a.py:f()\n"), ("method", "a.py:f()\nclass A:\n")],
)
def test_metadata_does_not_credit_a_hidden_target_line(kind, prefix):
    raw = {
        "node_id": "a.py:f()",
        "type": kind,
        "file": "a.py",
        "start_line": 10,
        "end_line": 12,
        "content": prefix + "    visible = 1\n    par",
    }
    node = corrected_node(raw)
    assert (node.start_line, node.end_line) == (10, 11)
    case = {
        "target_files": ["a.py"],
        "target_blocks": [{"file": "a.py", "start": 13, "end": 13}],
    }
    assert metrics(case, [node])["span_recall@5"] == 0
    assert raw["end_line"] == 12
