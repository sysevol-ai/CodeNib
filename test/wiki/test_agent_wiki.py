# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Fast unit tests for the agent wiki retrieval guardrails."""

from types import SimpleNamespace

from codenib.wiki.agent_wiki import (
    AgentWiki,
    _clean_markdown,
    _fact_plan_markdown,
    _page_quality_report,
    _prune_uncited_blocks,
    _readme_intro,
    _remove_orphan_headings,
)
from codenib.wiki.evidence import EvidenceItem, candidate_key


class _FakeVectorStore:
    def __init__(self, nodes):
        self.nodes = nodes
        self.calls = []

    def search_with_content(self, query, top_k):
        self.calls.append((query, top_k))
        return self.nodes[:top_k]


def test_markdown_cleanup_removes_outer_fence_and_uncited_prose():
    fenced = (
        "```markdown\n"
        "Unsupported introductory prose without an evidence marker anywhere.\n\n"
        "## Flow\n\n"
        "The source-backed routing implementation is shown here. [E1]\n"
        "```"
    )

    cleaned = _clean_markdown(fenced)
    pruned = _prune_uncited_blocks(cleaned)

    assert not cleaned.startswith("```")
    assert "Unsupported introductory" not in pruned
    assert "## Flow" in pruned
    assert "[E1]" in pruned


def test_markdown_cleanup_removes_empty_sections_but_keeps_parent_sections():
    markdown = (
        "## Runtime\n\n"
        "### Dispatch\n\n"
        "Requests enter through the dispatcher. [E1]\n\n"
        "## Empty section\n\n"
        "## Storage\n\n"
        "The repository stores an index manifest. [E2]"
    )

    cleaned = _remove_orphan_headings(markdown)

    assert "## Runtime" in cleaned
    assert "### Dispatch" in cleaned
    assert "## Empty section" not in cleaned
    assert "## Storage" in cleaned


def test_page_quality_requires_supported_plan_coverage():
    plan = {
        "sections": [
            {"title": "Routing", "claims": [{"evidence": ["E1"]}]},
            {"title": "Storage", "claims": [{"evidence": ["E2"]}]},
            {"title": "Runtime", "claims": [{"evidence": ["E3"]}]},
        ]
    }
    sparse = (
        "The repository exposes a request path. [E1]\n\n"
        "## Routing\n\n"
        "The router dispatches source requests to the runtime. [E1]"
    )
    complete = (
        "The repository exposes indexed context to callers. [E1]\n\n"
        "## Routing\n\n"
        "The router dispatches source requests to the runtime. [E1]\n\n"
        "## Storage\n\n"
        "The index persists repository source records. [E2]\n\n"
        "## Runtime\n\n"
        "The runtime serves those records to clients. [E3]"
    )

    assert _page_quality_report(sparse, plan)["valid"] is False
    report = _page_quality_report(complete, plan)
    assert report["valid"] is True
    assert report["claim_coverage"] == 1.0


def test_fact_plan_renderer_drops_unsupported_claims():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=1,
            end_line=10,
            symbol="README.md",
            kind="file",
            content=(
                "CodeNib builds a source-linked Wiki from a local repository "
                "and exposes indexed source context."
            ),
        ),
        EvidenceItem(
            id="E2",
            file="src/index.py",
            start_line=1,
            end_line=8,
            symbol="Indexer",
            kind="class",
            content="class Indexer: pass",
        ),
    ]
    markdown = _fact_plan_markdown(
        {
            "sections": [
                {
                    "title": "Indexing",
                    "claims": [
                        {
                            "statement": "The `Indexer` builds repository records",
                            "evidence": ["E2"],
                        }
                    ],
                },
                {
                    "title": "Invented",
                    "claims": [
                        {
                            "statement": "The powerful `MagicIndex` is universal",
                            "evidence": ["E1"],
                        }
                    ],
                },
            ]
        },
        evidence,
        [],
    )

    assert "## Indexing" in markdown
    assert "## Invented" not in markdown
    assert "`MagicIndex`" not in markdown


def test_readme_intro_skips_logo_markup():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=1,
            end_line=20,
            symbol="README.md",
            kind="file",
            content=(
                '<div align="center">\n<img src="logo.svg">\n</div>\n\n'
                "CodeNib compiles repository views and serves source-linked "
                "context through a local Wiki and tools.\n"
            ),
        )
    ]

    assert _readme_intro(evidence) == (
        "CodeNib compiles repository views and serves source-linked context "
        "through a local Wiki and tools.",
        "E1",
    )


class _FakeBM25:
    def __init__(self, nodes):
        self.nodes = nodes
        self.calls = []

    def search(self, query, top_k, **kwargs):
        self.calls.append((query, top_k, kwargs))
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


def test_agent_wiki_fuses_dense_and_bm25_routes(tmp_path):
    shared = {
        "file": "src/core.py",
        "node_name": "dispatch",
        "start_line": 0,
        "end_line": 4,
        "content": "def dispatch(): pass",
    }
    dense = _FakeVectorStore(
        [
            shared,
            {
                "file": "src/model.py",
                "node_name": "Model",
                "start_line": 0,
                "end_line": 4,
                "content": "class Model: pass",
            },
        ]
    )
    bm25 = _FakeBM25(
        [
            shared,
            {
                "file": "src/cli.py",
                "node_name": "main",
                "start_line": 0,
                "end_line": 4,
                "content": "def main(): pass",
            },
        ]
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir=str(tmp_path), language="python"),
        vector_store=dense,
        bm25=bm25,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")

    result = wiki._retrieve(
        {"title": "Dispatch", "summary": "request dispatch", "keywords": []},
        top_k=3,
    )

    assert [node["node_name"] for node in result] == ["dispatch", "Model", "main"]
    assert wiki._retrieval_routes[candidate_key(shared, wiki._node_attr)] == (
        "dense",
        "bm25",
    )
    assert bm25.calls[0][2]["return_code_content"] is True


def test_generated_page_uses_fact_plan_and_reports_grounding(tmp_path):
    class LLM:
        cache_identity = "fake"

        def __init__(self):
            self.calls = []

        def complete(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            if len(self.calls) == 1:
                return (
                    '{"thesis":"request routing","sections":[{"title":"Flow",'
                    '"claims":[{"statement":"Router dispatches requests",'
                    '"evidence":["E1"]}]}]}'
                )
            return (
                "The `Router` owns request dispatch and coordinates the "
                "source-backed request path. [E1]\n\n"
                "## Flow\n\n"
                "The implementation in `src/core.py` provides the concrete "
                "dispatch entry point used by this subsystem. [E1]"
            )

    node = {
        "file": "src/core.py",
        "node_name": "Router",
        "type": "class",
        "start_line": 0,
        "end_line": 6,
        "content": "class Router:\n    def dispatch(self):\n        return handle()",
    }
    llm = LLM()
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=_FakeVectorStore([node]),
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
        code_graph=lambda: None,
    )
    wiki = AgentWiki(bundle, model="fake-model", llm=llm)

    page = wiki._generate_page(
        {
            "id": "routing",
            "title": "Request Routing",
            "summary": "How requests move through the repository",
            "keywords": ["dispatch"],
            "files": ["src/core.py"],
        }
    )

    assert len(llm.calls) == 2
    assert page["grounding"]["valid"] is True
    assert page["quality"]["valid"] is True
    assert page["generation"]["mode"] == "generated"
    assert page["citations"][0]["start_line"] == 1
    assert page["evidence"]["items"][0]["routes"] == ("dense",)


def test_agent_wiki_cache_key_tracks_view_rebuild_identity(tmp_path):
    view = SimpleNamespace(
        status="fresh",
        commit="abc123",
        built_at_epoch=1.0,
        config={"builder_schema": 1},
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={"bm25": view}),
    )
    wiki = AgentWiki(bundle, model="fake-model")
    before = wiki._key("outline")
    view.built_at_epoch = 2.0
    view.config = {"builder_schema": 2}

    assert wiki._key("outline") != before
