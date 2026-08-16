# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

from codenib.wiki.agent_wiki import AgentWiki
from codenib.wiki.cache_audit import audit_wiki_cache


def test_cache_audit_is_read_only_and_reports_current_page_quality(tmp_path):
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path / "repo"),
            instance_id="owner__repo",
            commit_short="abc123",
            language="python",
        ),
        manifest=SimpleNamespace(languages=["python"], indexes={}),
        vector_store=None,
        bm25=None,
    )
    info = SimpleNamespace(id="owner__repo")

    class Registry:
        def list_infos(self):
            return [info]

        def get(self, repo_id):
            return bundle if repo_id == info.id else None

    cache_dir = tmp_path / "wiki-cache"
    wiki = AgentWiki(bundle, model="fake-model", cache_dir=str(cache_dir))
    outline = {
        "pages": [
            {
                "id": "overview",
                "title": "Overview",
                "summary": "Repository overview",
                "files": ["README.md"],
                "children": [
                    {
                        "id": "runtime",
                        "title": "Runtime",
                        "summary": "Runtime flow",
                        "files": ["src/runtime.py"],
                        "children": [],
                    }
                ],
            }
        ]
    }
    wiki._outline = outline
    wiki._write_cache("outline", outline)
    overview_meta = wiki._overview_page_meta(outline["pages"][0], [])
    wiki._write_cache(
        wiki._page_cache_suffix(overview_meta),
        {
            "id": "overview",
            "title": "Overview",
            "generation": {
                "mode": "generated",
                "fallback": None,
                "metrics": {
                    "total_ms": 1200.0,
                    "retrieval_ms": 20.0,
                    "planning_ms": 1100.0,
                    "model_call_ms": 1000.0,
                    "model_calls": 1,
                    "repair_attempts": 0,
                },
            },
            "quality": {"valid": True},
        },
    )

    report = audit_wiki_cache(
        Registry(),
        model="fake-model",
        cache_dir=cache_dir,
    )

    assert report["coverage"]["all"] == {
        "cached": 1,
        "total": 2,
        "missing": 1,
        "percent": 50.0,
    }
    assert report["coverage"]["overview"]["percent"] == 100.0
    assert report["generation_modes"] == {"generated": 1}
    assert report["generation_metrics"]["total_ms"] == {
        "count": 1,
        "p50": 1200.0,
        "p95": 1200.0,
        "max": 1200.0,
    }
    assert report["missing_overviews"] == []
    assert report["fallback_pages"] == []
    assert report["quality_invalid_pages"] == []


def test_cache_audit_does_not_create_a_missing_cache_directory(tmp_path):
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path / "repo"),
            instance_id="owner__repo",
            commit_short="abc123",
            language="python",
        ),
        manifest=SimpleNamespace(languages=["python"], indexes={}),
        vector_store=None,
        bm25=None,
    )

    class Registry:
        def list_infos(self):
            return [SimpleNamespace(id="owner__repo")]

        def get(self, repo_id):
            return bundle

    cache_dir = tmp_path / "missing-cache"

    report = audit_wiki_cache(
        Registry(),
        model="fake-model",
        cache_dir=cache_dir,
    )

    assert report["missing_outlines"] == ["owner__repo"]
    assert not cache_dir.exists()
