# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import pytest

from codenib.wiki.prewarm import prewarm_wiki_cache


class _Wiki:
    def __init__(self):
        self.calls = []

    def page_tree(self):
        return [
            {
                "id": "overview",
                "title": "Overview",
                "cache_state": "cold",
                "children": [
                    {
                        "id": "detail",
                        "title": "Detail",
                        "cache_state": "cold",
                        "children": [],
                    }
                ],
            },
            {
                "id": "runtime",
                "title": "Runtime",
                "cache_state": "ready",
                "children": [],
            },
        ]

    def cached_page_tree(self):
        return self.page_tree()

    def page(self, page_id):
        self.calls.append(page_id)
        return {
            "id": page_id,
            "generation": {
                "mode": "generated",
                "metrics": {"total_ms": 12.0, "model_calls": 1},
            },
            "quality": {"valid": True},
        }


def _registry(wiki):
    bundle = SimpleNamespace(wiki=wiki)

    class Registry:
        def list_infos(self):
            return [SimpleNamespace(id="owner__repo")]

        def get(self, repo_id):
            return bundle

    return Registry()


def test_prewarm_generates_only_cold_pages_in_the_selected_scope():
    wiki = _Wiki()

    report = prewarm_wiki_cache(
        _registry(wiki),
        wiki_factory=lambda bundle: bundle.wiki,
        scope="root",
        workers=2,
    )

    assert wiki.calls == ["overview"]
    assert report["selected_for_generation"] == 1
    assert report["counts"] == {"skipped_ready": 1, "warmed": 1}
    warmed = next(page for page in report["pages"] if page["status"] == "warmed")
    assert warmed["quality_valid"] is True
    assert warmed["metrics"]["model_calls"] == 1


def test_prewarm_dry_run_never_requests_a_page():
    wiki = _Wiki()

    report = prewarm_wiki_cache(
        _registry(wiki),
        wiki_factory=lambda bundle: bundle.wiki,
        scope="all",
        max_pages=1,
        dry_run=True,
    )

    assert wiki.calls == []
    assert report["counts"] == {
        "planned": 1,
        "skipped_limit": 1,
        "skipped_ready": 1,
    }


def test_prewarm_dry_run_plans_a_missing_outline_without_generating_it():
    class ColdOutlineWiki(_Wiki):
        def cached_page_tree(self):
            return None

        def page_tree(self):
            raise AssertionError("dry-run must not generate an outline")

    wiki = ColdOutlineWiki()

    report = prewarm_wiki_cache(
        _registry(wiki),
        wiki_factory=lambda bundle: bundle.wiki,
        dry_run=True,
    )

    assert wiki.calls == []
    assert report["counts"] == {"planned": 1}
    assert report["pages"] == [
        {
            "repo": "owner__repo",
            "id": "outline",
            "title": "Wiki outline",
            "kind": "outline",
            "before": "missing",
            "status": "planned",
        }
    ]


@pytest.mark.parametrize(
    ("max_pages", "expected_tree_calls", "expected_page_calls", "counts"),
    [
        (0, 0, [], {"skipped_limit": 1}),
        (1, 1, [], {"skipped_limit": 1, "warmed": 1}),
    ],
)
def test_prewarm_counts_cold_outline_against_page_limit(
    max_pages,
    expected_tree_calls,
    expected_page_calls,
    counts,
):
    class ColdOutlineWiki(_Wiki):
        def __init__(self):
            super().__init__()
            self.tree_calls = 0

        def cached_page_tree(self):
            return None

        def page_tree(self):
            self.tree_calls += 1
            return super().page_tree()

    wiki = ColdOutlineWiki()

    report = prewarm_wiki_cache(
        _registry(wiki),
        wiki_factory=lambda bundle: bundle.wiki,
        max_pages=max_pages,
    )

    assert wiki.tree_calls == expected_tree_calls
    assert wiki.calls == expected_page_calls
    assert report["selected_for_generation"] == max_pages
    assert report["counts"] == counts
    assert next(page for page in report["pages"] if page["id"] == "outline")[
        "status"
    ] == ("warmed" if max_pages else "skipped_limit")


def test_prewarm_rejects_unknown_repository_selectors():
    wiki = _Wiki()

    with pytest.raises(ValueError, match="unknown repository selector.*typo"):
        prewarm_wiki_cache(
            _registry(wiki),
            wiki_factory=lambda bundle: bundle.wiki,
            repo_ids=["typo"],
        )

    assert wiki.calls == []


def test_prewarm_reports_wiki_factory_failure_and_continues_other_repositories():
    healthy_wiki = _Wiki()
    bundles = {
        "owner__broken": SimpleNamespace(wiki=None),
        "owner__healthy": SimpleNamespace(wiki=healthy_wiki),
    }

    class Registry:
        def list_infos(self):
            return [SimpleNamespace(id=repo_id) for repo_id in bundles]

        def get(self, repo_id):
            return bundles.get(repo_id)

    def factory(bundle):
        if bundle.wiki is None:
            raise RuntimeError("invalid Wiki route")
        return bundle.wiki

    report = prewarm_wiki_cache(
        Registry(),
        wiki_factory=factory,
        workers=2,
    )

    assert report["counts"] == {"error": 1, "warmed": 1}
    assert report["pages"][0] == {
        "repo": "owner__broken",
        "id": "",
        "title": "",
        "before": "unknown",
        "status": "error",
        "error": "outline unavailable: invalid Wiki route",
    }
    assert report["pages"][1]["repo"] == "owner__healthy"
    assert report["pages"][1]["status"] == "warmed"
    assert healthy_wiki.calls == ["overview"]


def test_prewarm_can_explicitly_retry_degraded_pages_now():
    class DegradedWiki(_Wiki):
        def page_tree(self):
            return [
                {
                    "id": "overview",
                    "title": "Overview",
                    "cache_state": "degraded",
                    "children": [],
                }
            ]

        def page(self, page_id, *, retry_degraded_now=False):
            self.calls.append((page_id, retry_degraded_now))
            return {
                "id": page_id,
                "generation": {"mode": "generated", "metrics": {}},
                "quality": {"valid": True},
            }

    wiki = DegradedWiki()

    report = prewarm_wiki_cache(
        _registry(wiki),
        wiki_factory=lambda bundle: bundle.wiki,
        retry_degraded_now=True,
    )

    assert wiki.calls == [("overview", True)]
    assert report["retry_degraded_now"] is True
    assert report["counts"] == {"warmed": 1}


def test_prewarm_can_retry_a_reader_ready_page_needing_operator_review():
    class ReviewWiki(_Wiki):
        def page_tree(self):
            return [
                {
                    "id": "overview",
                    "title": "Overview",
                    "cache_state": "ready",
                    "children": [],
                }
            ]

        def page_needs_operator_retry(self, page_id):
            return page_id == "overview"

        def page(self, page_id, *, retry_degraded_now=False):
            self.calls.append((page_id, retry_degraded_now))
            return {
                "id": page_id,
                "generation": {"mode": "generated", "metrics": {}},
                "quality": {"valid": True},
            }

    wiki = ReviewWiki()

    report = prewarm_wiki_cache(
        _registry(wiki),
        wiki_factory=lambda bundle: bundle.wiki,
        retry_degraded_now=True,
    )

    assert wiki.calls == [("overview", True)]
    assert report["counts"] == {"warmed": 1}


def test_prewarm_releases_each_repo_after_its_pages_finish():
    wiki = _Wiki()
    registry = _registry(wiki)
    bundle = registry.get("owner__repo")
    releases = []
    bundle.release_views = lambda: releases.append(list(wiki.calls)) or True

    report = prewarm_wiki_cache(
        registry,
        wiki_factory=lambda selected: selected.wiki,
        scope="all",
        release_views=True,
    )

    assert wiki.calls == ["overview", "detail"]
    assert releases == [["overview", "detail"]]
    assert report["release_views"] is True
