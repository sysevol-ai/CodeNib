# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import sys

import pytest

from scripts import index_repo


def test_detect_languages_keeps_typescript_distinct(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const app = 1;\n")
    (tmp_path / "src" / "component.tsx").write_text("export const C = () => null;\n")
    (tmp_path / "src" / "config.js").write_text("module.exports = {};\n")

    assert index_repo.detect_languages(str(tmp_path)) == [
        "typescript",
        "javascript",
    ]


def test_normalize_languages_accepts_combined_js_ts_override():
    assert index_repo.normalize_languages("js/ts") == ["javascript", "typescript"]
    assert index_repo.normalize_languages("C++/C") == ["cpp"]
    assert index_repo.normalize_languages("cc") == ["cpp"]


def test_detect_languages_uses_the_shared_registry_without_python_fallback(
    tmp_path,
):
    (tmp_path / "Main.java").write_text("class Main {}\n")
    (tmp_path / "template.phtml").write_text("<?php echo 'hello'; ?>\n")

    assert index_repo.detect_languages(str(tmp_path)) == ["java", "php"]

    empty = tmp_path / "empty"
    empty.mkdir()
    assert index_repo.detect_languages(str(empty)) == []


def test_main_rejects_a_repo_without_supported_sources(
    tmp_path,
    monkeypatch,
    capsys,
):
    (tmp_path / "README.md").write_text("# docs only\n")
    monkeypatch.setattr(sys, "argv", ["index_repo.py", str(tmp_path)])

    with pytest.raises(SystemExit) as exc_info:
        index_repo.main()

    assert exc_info.value.code == 2
    assert "no supported source language was detected" in capsys.readouterr().err
