# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from codenib.wiki.repository_visuals import (
    attach_repository_visual,
    discover_overview_visual,
)


def _png(width: int = 1200, height: int = 700) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"preview"
    )


def test_discovers_architecture_visual_before_product_screenshot(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "screen.png").write_bytes(_png())
    (tmp_path / "docs" / "architecture.png").write_bytes(_png())
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "logo.png").write_bytes(_png())
    (tmp_path / "README.md").write_text(
        """# Demo

<img src="assets/logo.png" alt="Demo logo">

## Preview

![Application screenshot](docs/screen.png)
![Small logo](docs/architecture.png)

## System design

<img src="docs/architecture.png" alt="Runtime architecture">
""",
        encoding="utf-8",
    )

    visual = discover_overview_visual(tmp_path, repository="owner/demo")

    assert visual is not None
    assert visual.path == "docs/architecture.png"
    assert visual.role == "architecture"
    assert visual.mime_type == "image/png"


def test_discovers_an_alternate_root_readme_name(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "workflow.png").write_bytes(_png())
    (tmp_path / "README.mdx").write_text(
        "![Request workflow](docs/workflow.png)\n",
        encoding="utf-8",
    )

    visual = discover_overview_visual(tmp_path, repository="owner/demo")

    assert visual is not None
    assert visual.source_document == "README.mdx"
    assert visual.path == "docs/workflow.png"


def test_resolves_same_repository_raw_url_without_network(tmp_path: Path):
    (tmp_path / "assets").mkdir()
    expected = _png()
    (tmp_path / "assets" / "overview.png").write_bytes(expected)
    (tmp_path / "README.md").write_text(
        "<img "
        'src="https://raw.githubusercontent.com/owner/demo/main/assets/overview.png" '
        'alt="System overview">\n',
        encoding="utf-8",
    )

    visual = discover_overview_visual(tmp_path, repository="owner/demo")

    assert visual is not None
    assert visual.path == "assets/overview.png"
    assert visual.payload == expected


def test_ignores_external_traversal_badges_and_svg(tmp_path: Path):
    (tmp_path / "README.md").write_text(
        """# Demo

![Architecture](https://raw.githubusercontent.com/other/demo/main/architecture.png)
![Architecture](../outside.png)
![Build status](assets/status.png)
![Architecture](assets/architecture.svg)
""",
        encoding="utf-8",
    )

    assert discover_overview_visual(tmp_path, repository="owner/demo") is None


def test_invalid_top_candidate_falls_back_to_valid_visual(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "architecture.png").write_bytes(b"not an image")
    (tmp_path / "docs" / "preview.png").write_bytes(_png())
    (tmp_path / "README.md").write_text(
        """# Demo

![Architecture](docs/architecture.png)
![Product preview](docs/preview.png)
""",
        encoding="utf-8",
    )

    visual = discover_overview_visual(tmp_path, repository="owner/demo")

    assert visual is not None
    assert visual.path == "docs/preview.png"


def test_attach_adds_materialized_lead_without_mutating_page(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "architecture.png").write_bytes(_png())
    (tmp_path / "README.md").write_text(
        "![Architecture](docs/architecture.png)\n",
        encoding="utf-8",
    )
    visual = discover_overview_visual(tmp_path, repository="owner/demo")
    assert visual is not None
    page = {"id": "overview", "media_slots": [{"id": "generated"}]}

    attached = attach_repository_visual(
        page,
        visual,
        uri="api/repos/demo/wiki-source-assets/docs/architecture.png",
    )

    assert [slot["id"] for slot in attached["media_slots"]] == [
        "overview-repository-visual",
        "generated",
    ]
    asset = attached["media_slots"][0]["asset"]
    assert asset["provider"] == "repository"
    assert asset["metadata"]["source_path"] == "docs/architecture.png"
    assert asset["metadata"]["selection_score"] == visual.score
    assert page["media_slots"] == [{"id": "generated"}]
