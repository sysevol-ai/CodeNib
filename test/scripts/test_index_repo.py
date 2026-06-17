# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

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
