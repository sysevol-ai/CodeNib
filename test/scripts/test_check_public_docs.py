# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path

import yaml

from scripts import check_public_docs

REPO_ROOT = Path(__file__).parents[2]


def _config() -> dict:
    return {
        "exclude_docs": "\n".join(sorted(check_public_docs.REQUIRED_EXCLUSIONS)),
        "nav": [{"Home": "index.md"}],
    }


def test_mkdocs_navigation_tabs_contract():
    config = check_public_docs._load_config(REPO_ROOT / "mkdocs.yml")
    features = set(config["theme"]["features"])
    assert {
        "navigation.indexes",
        "navigation.tabs",
        "navigation.tabs.sticky",
    } <= features

    nav = config["nav"]
    titles = [item if isinstance(item, str) else next(iter(item)) for item in nav]
    assert titles == [
        "Home",
        "News",
        "Get Started",
        "Guides",
        "Concepts",
        "API Reference",
        "Development",
    ]

    sections = {
        next(iter(item)): next(iter(item.values()))
        for item in nav
        if isinstance(item, dict)
    }
    assert sections["News"] == "news.md"
    assert sections["Get Started"][0] == "get-started/index.md"
    assert sections["Guides"][0] == "guides/index.md"
    assert sections["Concepts"][0] == "concepts/index.md"
    assert sections["Development"][0] == "development/index.md"

    reference_deployments = next(
        item["Reference Deployments"]
        for item in sections["Guides"]
        if isinstance(item, dict) and "Reference Deployments" in item
    )
    assert reference_deployments == [
        "guides/reference-deployments/index.md",
        {"NVIDIA DGX Spark": "guides/reference-deployments/dgx-spark.md"},
    ]

    retrieval_planning = next(
        item["Retrieval Planning"]
        for item in sections["Concepts"]
        if isinstance(item, dict) and "Retrieval Planning" in item
    )
    assert retrieval_planning == [{"RAG Ops And Planner": "rag_ops.md"}]

    evaluation = next(
        item["Benchmarks & Evaluation"]
        for item in sections["Development"]
        if isinstance(item, dict) and "Benchmarks & Evaluation" in item
    )
    assert evaluation[0] == "evaluation/index.md"

    # Home uses its page outline as the left rail without duplicating "Home".
    home_source = (REPO_ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    home_metadata = yaml.safe_load(home_source.split("---", 2)[1])
    assert home_metadata["hide"] == ["navigation"]


def test_legacy_blog_routes_redirect_to_reference_deployments():
    redirects = {
        "blog/index.html": "/guides/reference-deployments/",
        "blog/local-code-intelligence-dgx-spark/index.html": (
            "/guides/reference-deployments/dgx-spark/"
        ),
    }

    for source, target in redirects.items():
        assert source in check_public_docs.ALLOWED_PUBLIC_STATIC_FILES
        html = (REPO_ROOT / "docs" / source).read_text(encoding="utf-8")
        assert f'content="0; url={target}"' in html
        assert f'href="{target}"' in html


def _write_api_site(site_dir, routes=None):
    for route in routes or check_public_docs.PUBLIC_API_ROUTES:
        page = site_dir / route / "index.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("<h1>API</h1>\n", encoding="utf-8")


def _write_search(site_dir, routes=None):
    path = site_dir / "search" / "search_index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    locations = routes or check_public_docs.PUBLIC_API_ROUTES
    path.write_text(
        json.dumps(
            {
                "docs": [
                    {
                        "location": f"https://docs.codenib.ai/{route}#symbol",
                    }
                    for route in locations
                ]
            }
        ),
        encoding="utf-8",
    )


def _write_sitemap(site_dir, routes=None):
    path = site_dir / "sitemap.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    locations = routes or check_public_docs.PUBLIC_API_ROUTES
    path.write_text(
        "<urlset>"
        + "".join(
            f"<url><loc>https://docs.codenib.ai/{route}</loc></url>"
            for route in locations
        )
        + "</urlset>",
        encoding="utf-8",
    )


def test_source_inventory_rejects_unapproved_nested_asset(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "index.md").write_text("# Home\n", encoding="utf-8")
    private = docs_dir / "assets" / "experiments" / "private.json"
    private.parent.mkdir(parents=True)
    private.write_text("{}", encoding="utf-8")

    errors = check_public_docs._check_source_inventory(docs_dir, _config())

    assert errors
    assert "assets/experiments/private.json" in errors[0]


def test_source_inventory_allows_pinned_swe_explore_case_set(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "index.md").write_text("# Home\n", encoding="utf-8")
    cases = docs_dir / "assets" / "swe_explore_cases.json"
    cases.parent.mkdir()
    cases.write_text('{"cases": []}\n', encoding="utf-8")

    assert check_public_docs._check_source_inventory(docs_dir, _config()) == []


def test_source_links_detect_encoded_autolink_html_and_repository_urls(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "index.md").write_text(
        "\n".join(
            [
                "<https://docs.codenib.ai/%65xperiments/private/>",
                "<a href=/product_roadmap/>roadmap</a>",
                "[raw]: https://raw.githubusercontent.com/sysevol-ai/"
                "CodeNib/main/docs/experiments/private.json",
            ]
        ),
        encoding="utf-8",
    )

    errors = check_public_docs._check_source_links(docs_dir, _config())

    assert errors
    assert "experiments" in errors[0]
    assert "product_roadmap" in errors[0]
    assert "raw.githubusercontent.com" in errors[0]


def test_generated_html_links_to_internal_location_are_rejected(tmp_path):
    site_dir = tmp_path / "site"
    page = site_dir / "guide" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text(
        '<a href="../%65xperiments/private/">private</a>',
        encoding="utf-8",
    )

    errors = check_public_docs._check_site_links(site_dir)

    assert errors
    # Relative targets are reported at the location they resolve to, which is
    # what makes the leak actionable; percent-encoding must not hide it.
    assert "experiments/private" in errors[0]


def test_relative_link_is_resolved_against_its_own_page_not_the_site_root(tmp_path):
    """A nested page may legitimately point at its own `experiments/` sibling.

    Normalizing the raw target against the site root turns `../../experiments/`
    into `experiments/`, which flags generated API pages such as
    `api/codenib/eval/**` even though they never leave the `api/` subtree.
    """

    site_dir = tmp_path / "site"
    page = site_dir / "api" / "codenib" / "eval" / "agent_runner" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text(
        '<a href="../../experiments/">sibling package</a>',
        encoding="utf-8",
    )

    assert check_public_docs._check_site_links(site_dir) == []


def test_relative_link_cannot_escape_above_the_public_site_root(tmp_path):
    site_dir = tmp_path / "site"
    page = site_dir / "guide" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text(
        '<a href="../../../../experiments/private/">private</a>',
        encoding="utf-8",
    )

    errors = check_public_docs._check_site_links(site_dir)

    assert errors
    assert "experiments/private" in errors[0]


def test_encoded_parent_segments_are_clamped_to_the_public_site_root(tmp_path):
    site_dir = tmp_path / "site"
    page = site_dir / "guide" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text(
        '<a href="%2e%2e%2f%2e%2e%2f%65xperiments/private/">private</a>',
        encoding="utf-8",
    )

    errors = check_public_docs._check_site_links(site_dir)

    assert errors
    assert "experiments/private" in errors[0]


def test_absolute_and_repository_links_to_internal_locations_still_rejected(tmp_path):
    site_dir = tmp_path / "site"
    page = site_dir / "api" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text(
        "\n".join(
            [
                '<a href="/product_roadmap/">absolute</a>',
                '<a href="https://github.com/sysevol-ai/CodeNib/blob/main/docs/'
                'experiments/private.json">repository</a>',
            ]
        ),
        encoding="utf-8",
    )

    errors = check_public_docs._check_site_links(site_dir)

    assert errors
    assert "/product_roadmap/" in errors[0]
    assert "github.com" in errors[0]


def test_api_inventory_accepts_exact_routes_in_all_generated_indexes(tmp_path):
    site_dir = tmp_path / "site"
    _write_api_site(site_dir)
    _write_search(site_dir)
    _write_sitemap(site_dir)

    assert check_public_docs._check_api_inventory(site_dir) == []


def test_api_inventory_rejects_unexpected_site_route_and_prefix_collision(tmp_path):
    site_dir = tmp_path / "site"
    routes = set(check_public_docs.PUBLIC_API_ROUTES)
    routes.update({"api/codenib/eval/", "api/codenib/modelx/"})
    _write_api_site(site_dir, routes)

    errors = check_public_docs._check_api_inventory(site_dir)

    assert any(
        "generated site exposes unexpected API routes" in error for error in errors
    )
    assert any("api/codenib/eval/" in error for error in errors)
    assert any("api/codenib/modelx/" in error for error in errors)


def test_api_inventory_rejects_missing_site_route(tmp_path):
    site_dir = tmp_path / "site"
    routes = set(check_public_docs.PUBLIC_API_ROUTES)
    routes.remove("api/codenib/model/")
    _write_api_site(site_dir, routes)

    errors = check_public_docs._check_api_inventory(site_dir)

    assert any(
        "generated site is missing public API routes" in error for error in errors
    )
    assert any("api/codenib/model/" in error for error in errors)


def test_api_inventory_rejects_encoded_search_only_route(tmp_path):
    site_dir = tmp_path / "site"
    _write_api_site(site_dir)
    routes = set(check_public_docs.PUBLIC_API_ROUTES)
    routes.add("api/codenib/%65val/")
    _write_search(site_dir, routes)

    errors = check_public_docs._check_api_inventory(site_dir)

    assert any(
        "search index exposes unexpected API routes" in error for error in errors
    )
    assert any("api/codenib/eval/" in error for error in errors)


def test_api_inventory_rejects_backslash_sitemap_only_route(tmp_path):
    site_dir = tmp_path / "site"
    _write_api_site(site_dir)
    _write_search(site_dir)
    routes = set(check_public_docs.PUBLIC_API_ROUTES)
    routes.add("api\\codenib\\eval\\")
    _write_sitemap(site_dir, routes)

    errors = check_public_docs._check_api_inventory(site_dir)

    assert any("sitemap exposes unexpected API routes" in error for error in errors)
    assert any("api/codenib/eval/" in error for error in errors)
