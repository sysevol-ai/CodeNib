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
    assert "%65xperiments/private" in errors[0]
