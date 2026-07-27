# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from codenib.wiki.builder import Symbol
from codenib.wiki.outline import _fallback_outline, _validate_outline


def test_outline_requires_real_source_anchors(tmp_path):
    (tmp_path / "codenib").mkdir()
    (tmp_path / "codenib" / "config.py").write_text("class RuntimeConfig: pass\n")
    (tmp_path / "codenib" / "runner.py").write_text("class AgentRunner: pass\n")
    symbols = [
        Symbol(
            file="codenib/config.py",
            name="RuntimeConfig",
            type="class",
            start_line=0,
            end_line=0,
            content="class RuntimeConfig: pass",
        ),
        Symbol(
            file="codenib/runner.py",
            name="AgentRunner",
            type="class",
            start_line=0,
            end_line=0,
            content="class AgentRunner: pass",
        ),
    ]
    data = {
        "pages": [
            {
                "id": "overview",
                "title": "Overview",
                "summary": "Repository purpose",
                "keywords": ["architecture"],
                "files": [],
                "children": [],
            },
            {
                "id": "configuration",
                "title": "Configuration",
                "summary": "Runtime configuration",
                "keywords": ["RuntimeConfig"],
                "files": ["invented/settings.py"],
                "children": [
                    {
                        "id": "plugins",
                        "title": "Plugin Marketplace",
                        "summary": "A generic unsupported extension page",
                        "keywords": ["marketplace"],
                        "files": [],
                        "children": [],
                    }
                ],
            },
            {
                "id": "communication",
                "title": "Agent Communication",
                "summary": "A generic unsupported section",
                "keywords": ["communication"],
                "files": [],
                "children": [],
            },
        ]
    }

    result = _validate_outline(
        data,
        str(tmp_path),
        symbols=symbols,
        fallback_files=["codenib/runner.py"],
    )

    assert [page["id"] for page in result["pages"]] == [
        "overview",
        "configuration",
    ]
    assert result["pages"][0]["files"] == ["codenib/runner.py"]
    assert result["pages"][1]["files"] == ["codenib/config.py"]
    assert result["pages"][1]["children"] == []


def test_fallback_outline_remains_navigable_without_model():
    result = _fallback_outline(
        [
            "src/core.py",
            "src/config.py",
            "tests/test_core.py",
            "README.md",
        ]
    )

    assert result["mode"] == "fallback"
    assert result["pages"][0]["id"] == "overview"
    assert {page["title"] for page in result["pages"]} >= {
        "Overview",
        "Src",
        "Tests",
    }


def test_overview_prefers_repository_root_readme(tmp_path):
    (tmp_path / "landing").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("Repository overview.\n")
    (tmp_path / "landing" / "README.md").write_text("Static landing page.\n")
    (tmp_path / "src" / "core.py").write_text("def run(): pass\n")

    result = _validate_outline(
        {
            "pages": [
                {
                    "id": "overview",
                    "title": "Overview",
                    "summary": "Repository purpose",
                    "keywords": ["architecture"],
                    "files": ["landing/README.md", "src/core.py"],
                    "children": [],
                }
            ]
        },
        str(tmp_path),
        symbols=[],
        fallback_files=["src/core.py"],
    )

    assert result["pages"][0]["files"][0] == "README.md"
