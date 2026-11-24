# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""People write the Wiki: page edits, document edits and folded answers."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codenib.annotations import AnnotationError
from codenib.annotations.edits import (
    apply_edit,
    fold_into,
    markdown_hash,
    page_source,
    revert_page,
    save_page,
)

PAGE = {"id": "math", "title": "Math", "markdown": "# Math\n\n## Add\n\nAdds.\n"}


def test_an_edit_replaces_the_generated_page_until_reverted(tmp_path: Path) -> None:
    source = page_source(tmp_path, "math", PAGE)
    assert source["kind"] == "page" and source["edited"] is False

    saved = save_page(tmp_path, "math", PAGE, "# Math\n\nMine.\n", source["base"])

    stored = tmp_path / ".codenib" / "wiki" / "math.md"
    front = yaml.safe_load(stored.read_text().split("---\n")[1])
    assert front["source"] == "human"
    assert front["base"] == markdown_hash(PAGE["markdown"])
    assert saved["edited"] is True and saved["markdown"] == "# Math\n\nMine."
    shown = apply_edit(tmp_path, "math", PAGE)
    assert shown["markdown"] == "# Math\n\nMine."
    assert shown["edited"]["stale"] is False

    regenerated = {**PAGE, "markdown": "# Math\n\nNew text.\n"}
    assert apply_edit(tmp_path, "math", regenerated)["edited"]["stale"] is True

    assert revert_page(tmp_path, "math") is True
    assert apply_edit(tmp_path, "math", PAGE) is PAGE
    assert revert_page(tmp_path, "math") is False


def test_a_save_from_an_old_version_is_refused(tmp_path: Path) -> None:
    first = page_source(tmp_path, "math", PAGE)
    save_page(tmp_path, "math", PAGE, "# One\n", first["base"])
    with pytest.raises(AnnotationError, match="changed since you opened it"):
        save_page(tmp_path, "math", PAGE, "# Two\n", first["base"])
    with pytest.raises(AnnotationError, match="some text"):
        save_page(tmp_path, "math", PAGE, "  ", "")
    with pytest.raises(AnnotationError, match="no editable page"):
        save_page(tmp_path, "../etc", PAGE, "x", "")


def test_a_document_saves_to_its_own_file(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Widget\n\n<b>raw html stays</b>\n")
    source = page_source(tmp_path, "doc--readme", None)
    assert source["kind"] == "document" and source["path"] == "README.md"
    assert "<b>raw html stays</b>" in source["markdown"]

    save_page(
        tmp_path, "doc--readme", None, source["markdown"] + "\nMore.", source["base"]
    )

    assert (tmp_path / "README.md").read_text().endswith("More.\n")
    assert not (tmp_path / ".codenib" / "wiki").exists()
    with pytest.raises(AnnotationError, match="changed since"):
        save_page(tmp_path, "doc--readme", None, "# Old\n", source["base"])


def test_an_answer_goes_at_the_end_of_its_section() -> None:
    markdown = "# T\n\nIntro.\n\n## A\n\nText a.\n\n## B\n\nText b.\n"
    by_heading = fold_into(
        markdown, section="A", quote="", question="Why?", answer="Because."
    )
    assert by_heading == (
        "# T\n\nIntro.\n\n## A\n\nText a.\n\n**Why?**\n\nBecause.\n\n## B\n\nText b.\n"
    )
    by_quote = fold_into(markdown, section="", quote="text b", question="Q", answer="A")
    assert by_quote.endswith("Text b.\n\n**Q**\n\nA\n")
    demoted = fold_into("# T\n", section="", quote="", question="Q", answer="## H\nx")
    assert "**H**\n\nx" in demoted and "## H" not in demoted
