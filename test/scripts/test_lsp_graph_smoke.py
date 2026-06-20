# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the generic LSP graph smoke runner."""

from __future__ import annotations

from scripts import smoke_lsp_graph


def test_write_smoke_project_creates_expected_language_files(tmp_path):
    expected = smoke_lsp_graph.write_smoke_project(tmp_path, "php")

    assert expected == ("Invoice", "normalize")
    assert (tmp_path / "Invoice.php").read_text(encoding="utf-8").startswith("<?php")


def test_write_smoke_project_creates_bundler_ruby_project(tmp_path):
    expected = smoke_lsp_graph.write_smoke_project(tmp_path, "ruby")

    assert expected == ("Smoke", "Smoke.Invoice", "Smoke.normalize()")
    assert 'gem "ruby-lsp", "0.26.9"' in (tmp_path / "Gemfile").read_text(
        encoding="utf-8"
    )
    assert 'gem "scip-ruby", "0.4.7"' in (tmp_path / "Gemfile").read_text(
        encoding="utf-8"
    )
    assert "module Smoke" in (tmp_path / "lib/invoice.rb").read_text(encoding="utf-8")
    assert (tmp_path / "smoke.gemspec").exists()


def test_write_smoke_project_creates_csharp_alignment_project(tmp_path):
    expected = smoke_lsp_graph.write_smoke_project(tmp_path, "csharp")

    program = (tmp_path / "Program.cs").read_text(encoding="utf-8")
    assert expected == ("Invoice", "Helpers", "Normalize")
    assert "public class Invoice" in program
    assert "public static class Helpers" in program
    assert "Normalize()" in program


def test_run_smoke_skips_unavailable_lsp(monkeypatch):
    class FakeClient:
        @staticmethod
        def get_lsp_command(language):
            return ["missing-lsp"]

        @staticmethod
        def check_lsp_available(language):
            return False

    monkeypatch.setattr(smoke_lsp_graph, "LSPClient", FakeClient)

    result = smoke_lsp_graph.run_smoke("ruby", skip_unavailable=True)

    assert result.ok
    assert result.status == "skipped"
    assert result.command == ["missing-lsp"]


def test_run_smoke_normalizes_language_alias(monkeypatch):
    class FakeClient:
        @staticmethod
        def get_lsp_command(language):
            return [f"{language}-lsp"]

        @staticmethod
        def check_lsp_available(language):
            return False

    monkeypatch.setattr(smoke_lsp_graph, "LSPClient", FakeClient)

    result = smoke_lsp_graph.run_smoke("rb", skip_unavailable=True)

    assert result.ok
    assert result.language == "ruby"
    assert result.command == ["ruby-lsp"]


def test_run_smoke_fails_unknown_language():
    result = smoke_lsp_graph.run_smoke("unknownlang", skip_unavailable=True)

    assert result.status == "failed"
    assert result.error == "unsupported smoke language: unknownlang"


def test_run_smoke_uses_indexer_and_validates_symbols(monkeypatch):
    calls = []

    class FakeClient:
        @staticmethod
        def get_lsp_command(language):
            return [f"{language}-lsp"]

        @staticmethod
        def check_lsp_available(language):
            return True

    class FakeVertex(dict):
        pass

    class FakeEdge(dict):
        def attributes(self):
            return dict(self)

    class FakeGraphData:
        vs = [FakeVertex(name="Invoice"), FakeVertex(name="normalize")]
        es = [FakeEdge(type="reference")]

        def vcount(self):
            return len(self.vs)

        def ecount(self):
            return len(self.es)

    class FakeCodeGraph:
        graph = FakeGraphData()

    class FakeIndexer:
        def __init__(
            self,
            project_root,
            language,
            output_dir,
            exclude_patterns=None,
            graph_route="active",
        ):
            calls.append(
                (project_root, language, output_dir, exclude_patterns, graph_route)
            )

        def run_pipeline(self, report_profile, include_references, target_dir=None):
            calls.append((report_profile, include_references, target_dir))
            return FakeCodeGraph()

    monkeypatch.setattr(smoke_lsp_graph, "LSPClient", FakeClient)
    monkeypatch.setattr(smoke_lsp_graph, "LSIndexer", FakeIndexer)

    result = smoke_lsp_graph.run_smoke("php", include_references=True)

    assert result.status == "ok"
    assert result.vertices == 2
    assert result.edges == 1
    assert result.references == 1
    assert calls[-1] == (False, True, None)


def test_run_smoke_can_persist_graph_artifact(tmp_path, monkeypatch):
    calls = []

    class FakeClient:
        @staticmethod
        def get_lsp_command(language):
            return [f"{language}-lsp"]

        @staticmethod
        def check_lsp_available(language):
            return True

    class FakeVertex(dict):
        pass

    class FakeGraphData:
        vs = [FakeVertex(name="Invoice"), FakeVertex(name="normalize")]
        es = []

        def vcount(self):
            return len(self.vs)

        def ecount(self):
            return len(self.es)

    class FakeCodeGraph:
        graph = FakeGraphData()

    class FakeIndexer:
        def __init__(
            self,
            project_root,
            language,
            output_dir,
            exclude_patterns=None,
            graph_route="active",
        ):
            self.output_dir = output_dir
            calls.append((project_root, language, output_dir, graph_route))

        def run_pipeline(self, report_profile, include_references, target_dir=None):
            self.output_dir.mkdir(parents=True)
            (self.output_dir / "graph.pkl").write_bytes(b"graph")
            return FakeCodeGraph()

    monkeypatch.setattr(smoke_lsp_graph, "LSPClient", FakeClient)
    monkeypatch.setattr(smoke_lsp_graph, "LSIndexer", FakeIndexer)

    result = smoke_lsp_graph.run_smoke(
        "php",
        skip_unavailable=True,
        output_root=tmp_path / "smoke",
    )

    assert result.status == "ok"
    assert result.graph_path == str(tmp_path / "smoke/php/out/graph.pkl")
    assert calls[0][-1] == "lsp"


def test_run_project_smoke_uses_existing_project_root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "src").mkdir()
    (project / "src/ProjectInvoice.php").write_text("<?php\n", encoding="utf-8")
    calls = []

    class FakeClient:
        @staticmethod
        def get_lsp_command(language):
            return [f"{language}-lsp"]

        @staticmethod
        def check_lsp_available(language):
            return True

    class FakeVertex(dict):
        pass

    class FakeGraphData:
        vs = [FakeVertex(name="ProjectInvoice")]
        es = []

        def vcount(self):
            return len(self.vs)

        def ecount(self):
            return len(self.es)

    class FakeCodeGraph:
        graph = FakeGraphData()

    class FakeIndexer:
        def __init__(
            self,
            project_root,
            language,
            output_dir,
            exclude_patterns=None,
            graph_route="active",
        ):
            calls.append(
                (project_root, language, output_dir, exclude_patterns, graph_route)
            )
            self.output_dir = output_dir

        def run_pipeline(self, report_profile, include_references, target_dir=None):
            calls.append((report_profile, include_references, target_dir))
            self.output_dir.mkdir(parents=True, exist_ok=True)
            (self.output_dir / "graph.pkl").write_bytes(b"graph")
            return FakeCodeGraph()

    monkeypatch.setattr(smoke_lsp_graph, "LSPClient", FakeClient)
    monkeypatch.setattr(smoke_lsp_graph, "LSIndexer", FakeIndexer)

    result = smoke_lsp_graph.run_project_smoke(
        "php",
        project,
        output_root=tmp_path / "out",
        expected_symbols=("ProjectInvoice",),
        target_dir="src",
        exclude_patterns=("vendor/**",),
    )

    assert result.status == "ok"
    assert result.graph_path == str(tmp_path / "out/project-php/graph.pkl")
    assert not (project / "Invoice.php").exists()
    assert calls[0] == (
        project.resolve(),
        "php",
        tmp_path / "out/project-php",
        ["vendor/**"],
        "lsp",
    )
    assert calls[1] == (False, False, "src")


def test_run_project_smoke_fails_missing_project_root(monkeypatch, tmp_path):
    class FakeClient:
        @staticmethod
        def get_lsp_command(language):
            return [f"{language}-lsp"]

        @staticmethod
        def check_lsp_available(language):
            return True

    monkeypatch.setattr(smoke_lsp_graph, "LSPClient", FakeClient)

    result = smoke_lsp_graph.run_project_smoke("php", tmp_path / "missing")

    assert result.status == "failed"
    assert "project root does not exist" in (result.error or "")


def test_run_smoke_fails_when_reference_floor_is_not_met(monkeypatch):
    class FakeClient:
        @staticmethod
        def get_lsp_command(language):
            return [f"{language}-lsp"]

        @staticmethod
        def check_lsp_available(language):
            return True

    class FakeVertex(dict):
        pass

    class FakeGraphData:
        vs = [FakeVertex(name="Invoice"), FakeVertex(name="normalize")]
        es = []

        def vcount(self):
            return len(self.vs)

        def ecount(self):
            return len(self.es)

    class FakeCodeGraph:
        graph = FakeGraphData()

    class FakeIndexer:
        def __init__(
            self,
            project_root,
            language,
            output_dir,
            exclude_patterns=None,
            graph_route="active",
        ):
            pass

        def run_pipeline(self, report_profile, include_references, target_dir=None):
            return FakeCodeGraph()

    monkeypatch.setattr(smoke_lsp_graph, "LSPClient", FakeClient)
    monkeypatch.setattr(smoke_lsp_graph, "LSIndexer", FakeIndexer)

    result = smoke_lsp_graph.run_smoke("php", min_references=1)

    assert result.status == "failed"
    assert result.references == 0
    assert result.min_references == 1
    assert result.error == "expected at least 1 reference edges"


def test_effective_lsp_command_runs_ruby_lsp_through_bundle(tmp_path):
    ruby = tmp_path / "ruby"
    bundle = tmp_path / "bundle"
    ruby_lsp = tmp_path / "ruby-lsp"
    ruby.write_text("#!/usr/bin/env ruby\n", encoding="utf-8")
    bundle.write_text("#!/usr/bin/env ruby\n", encoding="utf-8")
    ruby_lsp.write_text(f"#!{ruby}\n", encoding="utf-8")

    command = smoke_lsp_graph._effective_lsp_command("ruby", [str(ruby_lsp)])

    assert command == [str(bundle), "exec", "ruby-lsp"]


def test_parse_min_references_accepts_language_counts():
    assert smoke_lsp_graph._parse_min_references(["java=1", "php=0"]) == {
        "java": 1,
        "php": 0,
    }
