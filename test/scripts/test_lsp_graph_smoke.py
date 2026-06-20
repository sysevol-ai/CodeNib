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
        def __init__(self, project_root, language, output_dir):
            calls.append((project_root, language, output_dir))

        def run_pipeline(self, report_profile, include_references):
            calls.append((report_profile, include_references))
            return FakeCodeGraph()

    monkeypatch.setattr(smoke_lsp_graph, "LSPClient", FakeClient)
    monkeypatch.setattr(smoke_lsp_graph, "LSIndexer", FakeIndexer)

    result = smoke_lsp_graph.run_smoke("php", include_references=True)

    assert result.status == "ok"
    assert result.vertices == 2
    assert result.edges == 1
    assert result.references == 1
    assert calls[-1] == (False, True)


def test_run_smoke_can_persist_graph_artifact(tmp_path, monkeypatch):
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
        def __init__(self, project_root, language, output_dir):
            self.output_dir = output_dir

        def run_pipeline(self, report_profile, include_references):
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
        def __init__(self, project_root, language, output_dir):
            pass

        def run_pipeline(self, report_profile, include_references):
            return FakeCodeGraph()

    monkeypatch.setattr(smoke_lsp_graph, "LSPClient", FakeClient)
    monkeypatch.setattr(smoke_lsp_graph, "LSIndexer", FakeIndexer)

    result = smoke_lsp_graph.run_smoke("php", min_references=1)

    assert result.status == "failed"
    assert result.references == 0
    assert result.min_references == 1
    assert result.error == "expected at least 1 reference edges"


def test_parse_min_references_accepts_language_counts():
    assert smoke_lsp_graph._parse_min_references(["java=1", "php=0"]) == {
        "java": 1,
        "php": 0,
    }
