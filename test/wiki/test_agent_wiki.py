# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Fast unit tests for the agent wiki retrieval guardrails."""

from types import SimpleNamespace

from codeminer.wiki.agent_wiki import AgentWiki


class _FakeVectorStore:
    def __init__(self, nodes):
        self.nodes = nodes
        self.calls = []

    def search_with_content(self, query, top_k):
        self.calls.append((query, top_k))
        return self.nodes[:top_k]


def test_agent_wiki_retrieval_reranks_by_page_files_and_keywords(tmp_path):
    nodes = [
        {
            "file": "src/unrelated.py",
            "node_name": "unrelated",
            "start_line": 0,
            "end_line": 5,
            "content": "generic helper",
        },
        {
            "file": "src/core/http.py",
            "node_name": "dispatch_request",
            "start_line": 10,
            "end_line": 30,
            "content": "request pipeline dispatch handling",
        },
        {
            "file": "src/core/http.py",
            "node_name": "dispatch_request",
            "start_line": 10,
            "end_line": 30,
            "content": "duplicate copy should collapse",
        },
        {
            "file": "src/core/interceptors.py",
            "node_name": "InterceptorChain",
            "start_line": 40,
            "end_line": 70,
            "content": "request response modification",
        },
    ]
    store = _FakeVectorStore(nodes)
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=store,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"]),
    )
    wiki = AgentWiki(bundle, model="fake-model")

    result = wiki._retrieve(
        {
            "id": "request-pipeline",
            "title": "Request Pipeline",
            "summary": "How requests are dispatched",
            "files": ["src/core/http.py"],
            "keywords": ["request pipeline", "dispatch"],
        },
        top_k=2,
    )

    assert store.calls
    query, top_k = store.calls[0]
    assert "src/core/http.py" in query
    assert top_k == 8
    assert [node["node_name"] for node in result] == [
        "dispatch_request",
        "InterceptorChain",
    ]
