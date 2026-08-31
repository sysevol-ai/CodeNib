# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import pytest

from codenib.wiki.agent_wiki import AgentWiki
from codenib.wiki.cache_audit import audit_wiki_cache
from codenib.wiki.sqlite_store import SQLiteWikiStore


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


def test_cache_audit_rejects_unknown_repository_selectors(tmp_path):
    class Registry:
        def list_infos(self):
            return [SimpleNamespace(id="owner__repo")]

        def get(self, repo_id):
            raise AssertionError("unknown selection reached bundle lookup")

    with pytest.raises(ValueError, match="unknown repository selector.*typo"):
        audit_wiki_cache(
            Registry(),
            model="fake-model",
            cache_dir=tmp_path / "wiki-cache",
            repo_ids=["typo"],
        )


def test_cache_audit_subset_does_not_count_other_repo_as_orphan(tmp_path):
    cache_dir = tmp_path / "wiki-cache"
    bundles = {}
    for repo_id in ("owner__selected", "owner__other"):
        bundle = SimpleNamespace(
            entry=SimpleNamespace(
                repo=repo_id.replace("__", "/"),
                repo_dir=str(tmp_path / repo_id),
                instance_id=repo_id,
                commit_short="abc123",
                language="python",
            ),
            manifest=SimpleNamespace(languages=["python"], indexes={}),
            vector_store=None,
            bm25=None,
        )
        bundles[repo_id] = bundle
        wiki = AgentWiki(bundle, model="fake-model", cache_dir=str(cache_dir))
        wiki._write_cache(
            "outline",
            {
                "pages": [
                    {
                        "id": "overview",
                        "title": "Overview",
                        "summary": "Repository overview",
                        "files": ["README.md"],
                        "children": [],
                    }
                ]
            },
        )

    class Registry:
        def list_infos(self):
            return [SimpleNamespace(id=repo_id) for repo_id in bundles]

        def get(self, repo_id):
            return bundles.get(repo_id)

    report = audit_wiki_cache(
        Registry(),
        model="fake-model",
        cache_dir=cache_dir,
        repo_ids=["owner__selected"],
    )

    assert report["repositories"] == 1
    assert report["storage"]["files"] == 1
    assert report["storage"]["orphan_files"] == 0


def test_cache_audit_reads_store_without_publishing_and_reports_orphans(tmp_path):
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

    outline = {
        "pages": [
            {
                "id": "overview",
                "title": "Overview",
                "summary": "Repository overview",
                "files": ["README.md"],
                "children": [],
            }
        ]
    }
    seed_wiki = AgentWiki(bundle, model="fake-model", cache_dir=str(tmp_path))
    overview_meta = seed_wiki._overview_page_meta(outline["pages"][0], [])
    page_suffix = seed_wiki._page_cache_suffix(overview_meta)

    def entry(
        entry_id,
        *,
        repository_id="owner__repo",
        data=None,
    ):
        return SimpleNamespace(
            entry_id=entry_id,
            repository_id=repository_id,
            envelope={"model": "fake-model", "data": data or {}},
        )

    stored_entries = {
        seed_wiki._store_entry_id("outline"): entry(
            seed_wiki._store_entry_id("outline"),
            data=outline,
        ),
        seed_wiki._store_entry_id(page_suffix): entry(
            seed_wiki._store_entry_id(page_suffix),
            data={
                "id": "overview",
                "title": "Overview",
                "generation": {"mode": "generated", "metrics": {}},
                "quality": {"valid": True},
            },
        ),
        "orphan": entry("orphan"),
        "other-repository": entry(
            "other-repository",
            repository_id="owner__other",
        ),
    }

    class MemoryWikiStore:
        def __init__(self):
            self.read_calls = []
            self.scan_filters = []

        def read(self, entry_id):
            self.read_calls.append(entry_id)
            return stored_entries.get(entry_id)

        def publish(self, **kwargs):
            raise AssertionError("read-only audit must not publish")

        def scan(self, *, repository_ids=None):
            self.scan_filters.append(repository_ids)
            if repository_ids is None:
                return tuple(stored_entries.values())
            selected = set(repository_ids)
            return tuple(
                item
                for item in stored_entries.values()
                if item.repository_id in selected
            )

        def generation_guard(self, entry_id):
            raise AssertionError("read-only audit must not take generation guards")

    class ReadOnlyAuditWiki(AgentWiki):
        def _read_cache(self, suffix):
            raise AssertionError("audit must use the read-only cache path")

    store = MemoryWikiStore()
    report = audit_wiki_cache(
        Registry(),
        model="fake-model",
        cache_dir=tmp_path / "missing-cache",
        repo_ids=["owner__repo"],
        wiki_factory=ReadOnlyAuditWiki,
        store=store,
    )

    assert report["coverage"]["all"]["percent"] == 100.0
    assert report["storage"]["backend"] == "MemoryWikiStore"
    assert report["storage"]["entries"] == 3
    assert report["storage"]["orphan_entries"] == 1
    assert report["storage"]["files"] == 0
    assert report["storage"]["orphan_files"] == 0
    assert store.scan_filters == [{"owner__repo"}]
    assert store.read_calls == [
        seed_wiki._store_entry_id("outline"),
        seed_wiki._store_entry_id(page_suffix),
    ]
    assert not (tmp_path / "missing-cache").exists()


def test_cache_audit_reports_database_and_legacy_storage_in_mixed_state(tmp_path):
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
            return bundle if repo_id == "owner__repo" else None

    cache_dir = tmp_path / "wiki-cache"
    legacy = AgentWiki(bundle, model="fake-model", cache_dir=str(cache_dir))
    outline = {
        "pages": [
            {
                "id": "overview",
                "title": "Overview",
                "summary": "Repository overview",
                "files": ["README.md"],
                "children": [],
            }
        ]
    }
    legacy._write_cache("outline", outline)
    overview_meta = legacy._overview_page_meta(outline["pages"][0], [])
    legacy._write_cache(
        legacy._page_cache_suffix(overview_meta),
        {
            "id": "overview",
            "title": "Overview",
            "generation": {"mode": "generated", "metrics": {}},
            "quality": {"valid": True},
        },
    )
    (cache_dir / "agentwiki_orphan.json").write_text(
        '{"data":{"id":"removed"}}',
        encoding="utf-8",
    )

    store = SQLiteWikiStore(cache_dir / "wiki.sqlite3")
    store.publish(
        entry_id="orphan",
        repository_id="owner__repo",
        envelope={"data": {"id": "removed"}},
    )

    report = audit_wiki_cache(
        Registry(),
        model="fake-model",
        cache_dir=cache_dir,
        store=store,
    )

    storage = report["storage"]
    assert report["coverage"]["all"]["percent"] == 100.0
    assert storage["entries"] == 1
    assert storage["orphan_entries"] == 1
    assert storage["files"] == 3
    assert storage["orphan_files"] == 1
    assert storage["database_payload_bytes"] > 0
    assert storage["legacy_bytes"] > 0
    assert storage["bytes"] == storage["legacy_bytes"]
    assert storage["orphan_bytes"] == storage["orphan_legacy_bytes"]
    assert storage["orphan_database_payload_bytes"] > 0
