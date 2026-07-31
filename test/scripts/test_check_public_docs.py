# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from scripts import check_public_docs


def _config() -> dict:
    return {
        "exclude_docs": "\n".join(sorted(check_public_docs.REQUIRED_EXCLUSIONS)),
        "nav": [{"Home": "index.md"}],
    }


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
