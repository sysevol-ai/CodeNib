# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from codenib.graph.setup import diagnose_graph_setup


def _resolver(paths: dict[str, str]):
    return lambda command: paths.get(command)


def test_python_setup_does_not_accept_an_unrelated_graph_tool(tmp_path: Path) -> None:
    report = diagnose_graph_setup(
        tmp_path,
        ["python"],
        command_resolver=_resolver(
            {
                "scip-go": "/tools/scip-go",
            }
        ),
        module_checker=lambda name: name in {"igraph", "google.protobuf"},
    )

    assert report.ready is False
    assert report.partial_ready is False
    assert report.unavailable_languages == ["python"]
    assert report.languages[0].backend == "scip"
    assert report.languages[0].missing == ["scip-python"]


def test_unsupported_language_does_not_hide_ready_partial_coverage(
    tmp_path: Path,
) -> None:
    report = diagnose_graph_setup(
        tmp_path,
        ["python", "swift"],
        command_resolver=_resolver(
            {
                "scip-python": "/tools/scip-python",
            }
        ),
        module_checker=lambda name: name in {"igraph", "google.protobuf"},
    )

    assert report.ready is True
    assert report.partial_ready is True
    assert report.buildable_languages == ["python"]
    assert report.unsupported_languages == ["swift"]
    assert report.languages[1].state == "unsupported"


def test_cpp_setup_requires_a_compilation_database_route(tmp_path: Path) -> None:
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n")
    report = diagnose_graph_setup(
        tmp_path,
        ["cpp"],
        command_resolver=_resolver({"clangd": "/tools/clangd"}),
        module_checker=lambda name: name in {"igraph", "google.protobuf"},
    )

    assert report.ready is False
    assert report.languages[0].missing == ["compilation database"]

    (tmp_path / "compile_commands.json").write_text(
        '[{"directory": ".", "file": "main.cpp", "command": "c++ -c main.cpp"}]\n'
    )
    report = diagnose_graph_setup(
        tmp_path,
        ["cpp"],
        command_resolver=_resolver({"clangd": "/tools/clangd"}),
        module_checker=lambda name: name in {"igraph", "google.protobuf"},
    )

    assert report.ready is True
    assert report.languages[0].resolved_command == "/tools/clangd"


def test_setup_payload_contains_actionable_language_specific_data(
    tmp_path: Path,
) -> None:
    report = diagnose_graph_setup(
        tmp_path,
        ["typescript"],
        command_resolver=_resolver({}),
        module_checker=lambda _name: False,
    )

    payload = report.to_dict()

    assert payload["ready"] is False
    assert payload["languages"][0]["backend"] == "scip"
    assert payload["languages"][0]["missing"] == ["scip-typescript"]
    assert payload["commands"][0] == 'pip install "codenib[graph]"'
    assert any("scip-typescript" in hint for hint in payload["install_hints"])


@pytest.mark.parametrize(
    ("language", "tool", "marker"),
    [
        ("go", "scip-go", "go.mod"),
        ("rust", "rust-analyzer", "Cargo.toml"),
    ],
)
def test_module_languages_require_their_project_marker(
    tmp_path: Path,
    language: str,
    tool: str,
    marker: str,
) -> None:
    resolver = _resolver({tool: f"/tools/{tool}"})

    missing = diagnose_graph_setup(
        tmp_path,
        [language],
        command_resolver=resolver,
        module_checker=lambda _name: True,
    )
    assert missing.ready is False
    assert missing.languages[0].missing == [marker]

    (tmp_path / marker).write_text("\n")
    ready = diagnose_graph_setup(
        tmp_path,
        [language],
        command_resolver=resolver,
        module_checker=lambda _name: True,
    )
    assert ready.ready is True


def test_csharp_accepts_a_nested_project_file(tmp_path: Path) -> None:
    project = tmp_path / "src" / "Product"
    project.mkdir(parents=True)
    (project / "Product.csproj").write_text("<Project />\n")

    report = diagnose_graph_setup(
        tmp_path,
        ["csharp"],
        command_resolver=_resolver({"scip-dotnet": "/tools/scip-dotnet"}),
        module_checker=lambda _name: True,
    )

    assert report.ready is True
    assert report.languages[0].backend == "scip"


def test_ruby_loose_project_reports_the_lsp_fallback(tmp_path: Path) -> None:
    report = diagnose_graph_setup(
        tmp_path,
        ["ruby"],
        command_resolver=_resolver({"ruby-lsp": "/tools/ruby-lsp"}),
        module_checker=lambda _name: True,
    )

    assert report.ready is True
    assert report.languages[0].backend == "lsp"
    assert "unprepared bundle" in report.languages[0].note


def test_php_composer_project_reports_preparable_scip_route(tmp_path: Path) -> None:
    (tmp_path / "composer.json").write_text('{"name": "example/project"}\n')
    report = diagnose_graph_setup(
        tmp_path,
        ["php"],
        command_resolver=_resolver(
            {
                "php": "/tools/php",
                "composer": "/tools/composer",
            }
        ),
        module_checker=lambda _name: True,
    )

    assert report.ready is True
    assert report.languages[0].backend == "scip"
    assert report.languages[0].resolved_command == "/tools/composer with /tools/php"
