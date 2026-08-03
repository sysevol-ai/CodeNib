# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from codenib.types import QueriedNode
from scripts.check_ask_retrieval import AskRetrievalCase, evaluate_case


def _node(path: str) -> QueriedNode:
    return QueriedNode(
        node_id=f"{path}:symbol()",
        node_name="symbol",
        file=f"/checkout/{path}",
    )


def test_evaluate_case_accepts_one_path_from_each_required_group():
    case = AskRetrievalCase(
        name="lifecycle",
        query="query",
        required_path_groups=(
            ("src/compiler.py",),
            ("src/web.py", "src/mcp.py"),
        ),
    )

    result = evaluate_case(
        case,
        [_node("src/compiler.py"), _node("src/mcp.py")],
    )

    assert result.passed is True
    assert result.missing_path_groups == ()
    assert result.paths == ("src/compiler.py", "src/mcp.py")


def test_evaluate_case_reports_missing_and_forbidden_paths():
    case = AskRetrievalCase(
        name="runtime",
        query="query",
        required_path_groups=(("src/runtime.py",),),
        forbidden_paths=("scripts/smoke.py",),
    )

    result = evaluate_case(case, [_node("scripts/smoke.py")])

    assert result.passed is False
    assert result.missing_path_groups == (("src/runtime.py",),)
    assert result.forbidden_hits == ("scripts/smoke.py",)
