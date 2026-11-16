# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Exercise QA with the native agent and prepared repository indexes."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from codenib.agent.runtime.explorer import RepositoryContextExplorer
from codenib.eval.agent_runner.qa import (
    _bound_source_tool,
    repository_relative_paths,
    run_qa_agent,
)


class _ToolCall(SimpleNamespace):
    def model_dump(self, **kwargs):
        return {"id": self.id, "type": self.type, "function": vars(self.function)}


def test_native_agent_reads_real_index_and_source(integration_manifest):
    from codenib.llm import LiteLLMChat

    calls = []
    llm = MagicMock(spec=LiteLLMChat)
    llm.model = "scripted"

    def response(messages, **kwargs):
        calls.append(copy.deepcopy(messages))
        if len(calls) == 1:
            assert "Answer the question about this repository" in messages[0]["content"]
            assert "End with exactly these three lines" not in messages[0]["content"]
            names = {t["function"]["name"] for t in kwargs["tools"]}
            assert {"bm25_search", "find_callers", "find_callees", "read"} <= names
            assert "bash" not in names
            name, arguments = "bm25_search", {"query": "calculate_tax"}
        elif len(calls) == 2:
            assert "src/service.py" in messages[-1]["content"]
            name, arguments = "read", {"file_path": "src/service.py"}
        else:
            assert "return helper(amount)" in messages[-1]["content"]
            message = SimpleNamespace(
                role="assistant",
                content="Tax is computed using helper(amount).",
                tool_calls=None,
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
        call = _ToolCall(
            id=str(len(calls)),
            type="function",
            function=SimpleNamespace(
                name=name,
                arguments=json.dumps(arguments),
            ),
        )
        message = SimpleNamespace(role="assistant", content=None, tool_calls=[call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    llm._call_raw.side_effect = response
    with RepositoryContextExplorer.from_manifest(
        integration_manifest, policy="graph"
    ) as explorer:
        result = run_qa_agent(explorer, "How is tax computed?", llm=llm)
    assert len(calls) == result["steps"] == 3
    assert "helper(amount)" in result["answer"]
    assert result["integration"]["agent_class"] == "codenib.agent.runner.AgentRunner"
    assert result["integration"]["views"] == ["bm25", "symbol_graph"]
    assert any(m["role"] == "tool" for m in result["history"])
    root = str(explorer.context.manifest.repo_path)
    assert root not in json.dumps(calls)
    assert root not in json.dumps(result)
    assert result["integration"]["repo"] == "."


def test_missing_file_error_reaches_model_with_relative_path(integration_manifest):
    from codenib.llm import LiteLLMChat

    calls = []
    llm = MagicMock(spec=LiteLLMChat)
    llm.model = "scripted"

    def response(messages, **kwargs):
        calls.append(copy.deepcopy(messages))
        if len(calls) == 1:
            call = _ToolCall(
                id="missing",
                type="function",
                function=SimpleNamespace(
                    name="read", arguments=json.dumps({"file_path": "missing.py"})
                ),
            )
            message = SimpleNamespace(role="assistant", content=None, tool_calls=[call])
        else:
            assert "missing.py" in messages[-1]["content"]
            assert "file not found" in messages[-1]["content"]
            message = SimpleNamespace(
                role="assistant", content="The file is unavailable.", tool_calls=None
            )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    llm._call_raw.side_effect = response
    with RepositoryContextExplorer.from_manifest(
        integration_manifest, policy="graph"
    ) as explorer:
        root = str(explorer.context.manifest.repo_path)
        result = run_qa_agent(explorer, "Read missing.py", llm=llm)
    assert len(calls) == 2
    for payload in (calls, result):
        assert root not in json.dumps(payload)
    assert "repo_path: ." in calls[0][0]["content"]


def test_relative_paths_match_complete_roots_and_json_escaped_unicode(tmp_path):
    root = tmp_path / "SWE-QA-Bench" / "用户" / "repo"
    root.mkdir(parents=True)
    record = {
        "root": str(root),
        "file": str(root / "src/a.py"),
        "tool": json.dumps({"error": str(root / "missing.py")}),
        "sibling": str(root) + "sitory/a.py",
    }
    cleaned = repository_relative_paths(record, root)
    assert cleaned["root"] == "." and cleaned["file"] == "src/a.py"
    assert json.loads(cleaned["tool"])["error"] == "missing.py"
    assert cleaned["sibling"] == record["sibling"]


def test_qa_tools_cannot_read_another_checkout_or_git_metadata(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    seen = []
    execute = _bound_source_tool(
        lambda **kwargs: seen.append(kwargs), root, "file_path"
    )
    for path in (str(tmp_path / "outside.py"), ".git/config"):
        with pytest.raises(ValueError, match="source view"):
            execute(file_path=path)
    execute(file_path="src/file.py")
    assert Path(seen[0]["file_path"]) == root / "src/file.py"
