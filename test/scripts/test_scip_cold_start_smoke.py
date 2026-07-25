# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the SCIP cold-start candidate smoke runner."""

from __future__ import annotations

import sys

from scripts import smoke_scip_cold_start


def test_write_scip_smoke_project_creates_maven_java_project(tmp_path):
    expected = smoke_scip_cold_start.write_scip_smoke_project(tmp_path, "java")

    assert expected == (
        "pom.xml",
        "src/main/java/app/Foo.java",
        "src/main/java/app/Bar.java",
    )
    pom = (tmp_path / "pom.xml").read_text(encoding="utf-8")
    assert "<maven.compiler.source>11</maven.compiler.source>" in pom
    assert "<maven.compiler.target>11</maven.compiler.target>" in pom
    assert "new Foo().run()" in (tmp_path / "src/main/java/app/Bar.java").read_text(
        encoding="utf-8"
    )


def test_write_scip_smoke_project_creates_gradle_kotlin_project(tmp_path):
    expected = smoke_scip_cold_start.write_scip_smoke_project(tmp_path, "kotlin")

    assert expected == (
        "settings.gradle.kts",
        "build.gradle.kts",
        "src/main/kotlin/app/Invoice.kt",
        "src/test/kotlin/app/InvoiceTest.kt",
    )
    assert 'kotlin("jvm") version "2.1.20"' in (
        tmp_path / "build.gradle.kts"
    ).read_text(encoding="utf-8")
    assert "fun normalize()" in (tmp_path / "src/main/kotlin/app/Invoice.kt").read_text(
        encoding="utf-8"
    )


def test_write_scip_smoke_project_creates_gradle_scala_project(tmp_path):
    expected = smoke_scip_cold_start.write_scip_smoke_project(tmp_path, "scala")

    assert expected == (
        "settings.gradle",
        "build.gradle",
        "src/main/scala/app/Invoice.scala",
    )
    build = (tmp_path / "build.gradle").read_text(encoding="utf-8")
    assert 'id "scala"' in build
    assert "scala-library:2.13.12" in build
    source = (tmp_path / "src/main/scala/app/Invoice.scala").read_text(encoding="utf-8")
    assert "object Helpers" in source
    assert "def normalize()" in source


def test_write_scip_smoke_project_creates_csharp_project(tmp_path):
    expected = smoke_scip_cold_start.write_scip_smoke_project(tmp_path, "csharp")

    assert expected == ("Smoke.csproj", "Program.cs")
    assert "<TargetFramework>net8.0</TargetFramework>" in (
        tmp_path / "Smoke.csproj"
    ).read_text(encoding="utf-8")
    source = (tmp_path / "Program.cs").read_text(encoding="utf-8")
    assert "public class Invoice" in source
    assert "public static string Normalize()" in source


def test_write_scip_smoke_project_creates_ruby_project(tmp_path):
    expected = smoke_scip_cold_start.write_scip_smoke_project(tmp_path, "ruby")

    assert expected == ("Gemfile", "smoke.gemspec", "lib/invoice.rb")
    gemfile = (tmp_path / "Gemfile").read_text(encoding="utf-8")
    assert "gem \"scip-ruby\", '0.4.7'" in gemfile
    source = (tmp_path / "lib/invoice.rb").read_text(encoding="utf-8")
    assert "module Smoke" in source
    assert "def self.normalize" in source


def test_write_scip_smoke_project_creates_php_project(tmp_path):
    expected = smoke_scip_cold_start.write_scip_smoke_project(tmp_path, "php")

    assert expected == ("composer.json", "src/Billing/Invoice.php")
    composer = (tmp_path / "composer.json").read_text(encoding="utf-8")
    assert '"Smoke\\\\": "src/"' in composer
    source = (tmp_path / "src/Billing/Invoice.php").read_text(encoding="utf-8")
    assert "namespace Smoke\\Billing;" in source
    assert "public function total()" in source


def test_run_smoke_skips_unavailable_scip_command(monkeypatch):
    monkeypatch.setattr(
        smoke_scip_cold_start,
        "scip_cold_start_command_for_language",
        lambda language: ["definitely-missing-scip-java"],
    )

    result = smoke_scip_cold_start.run_smoke("java", skip_unavailable=True)

    assert result.ok
    assert result.status == "skipped"
    assert result.command == ["definitely-missing-scip-java"]


def test_run_smoke_runs_registered_command_and_validates_index(tmp_path, monkeypatch):
    writer = tmp_path / "write_index.py"
    writer.write_text(
        "from pathlib import Path\nPath('index.scip').write_bytes(b'scip')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        smoke_scip_cold_start,
        "scip_cold_start_command_for_language",
        lambda language: [sys.executable, str(writer)],
    )

    result = smoke_scip_cold_start.run_smoke(
        "java",
        decode=False,
        process_graph=False,
        output_root=tmp_path / "out",
    )

    assert result.status == "ok"
    assert result.index_bytes == 4
    assert result.index_path == str(tmp_path / "out/java/index.scip")


def test_run_smoke_fails_when_index_is_missing(tmp_path, monkeypatch):
    noop = tmp_path / "noop.py"
    noop.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        smoke_scip_cold_start,
        "scip_cold_start_command_for_language",
        lambda language: [sys.executable, str(noop)],
    )

    result = smoke_scip_cold_start.run_smoke(
        "java",
        decode=False,
        process_graph=False,
        output_root=tmp_path / "out",
    )

    assert result.status == "failed"
    assert result.error == "SCIP command did not create index.scip"


def test_run_project_smoke_uses_candidate_indexer(tmp_path, monkeypatch):
    calls = []

    class FakeVertex(dict):
        def attributes(self):
            return dict(self)

    class FakeEdge(dict):
        def attributes(self):
            return dict(self)

    class FakeGraphData:
        vs = [FakeVertex(name="src/main/java/app/App.java:App")]
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
            *,
            output_dir,
            language,
            graph_route,
        ):
            calls.append((project_root, output_dir, language, graph_route))
            self.output_dir = output_dir
            self.index_file = output_dir / "index.scip"
            self.decoded_file = output_dir / "index.decoded"
            self.graph_file = output_dir / "graph.pkl"

        def generate_index(self, timeout=None):
            calls.append(("generate", timeout))
            self.index_file.write_bytes(b"scip")
            return True

        def decode_index(self):
            calls.append("decode")
            self.decoded_file.write_text("decoded", encoding="utf-8")
            return True

        def process_index(self, output_file):
            calls.append(("process", output_file))
            self.graph_file.write_bytes(b"graph")
            return FakeCodeGraph()

    monkeypatch.setattr(
        smoke_scip_cold_start,
        "scip_cold_start_command_for_language",
        lambda language: [sys.executable],
    )
    monkeypatch.setattr(
        smoke_scip_cold_start,
        "LSIndexer",
        FakeIndexer,
    )

    project = tmp_path / "real-project"
    project.mkdir()
    result = smoke_scip_cold_start.run_project_smoke(
        "java",
        project,
        output_root=tmp_path / "out",
        expected_symbols=("App",),
        timeout=12,
    )

    assert result.status == "ok"
    assert result.vertices == 1
    assert result.references == 1
    assert result.graph_path == str(tmp_path / "out/real-project-java/graph.pkl")
    assert calls[0] == (
        project.resolve(),
        tmp_path / "out/real-project-java",
        "java",
        "scip-candidate",
    )
    assert ("generate", 12) in calls


def test_run_project_smoke_reports_missing_expected_symbol(tmp_path, monkeypatch):
    class FakeVertex(dict):
        def attributes(self):
            return dict(self)

    class FakeGraphData:
        vs = [FakeVertex(name="OnlyOtherSymbol")]
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
            *,
            output_dir,
            language,
            graph_route,
        ):
            self.index_file = output_dir / "index.scip"
            self.decoded_file = output_dir / "index.decoded"
            self.graph_file = output_dir / "graph.pkl"

        def generate_index(self, timeout=None):
            self.index_file.write_bytes(b"scip")
            return True

        def decode_index(self):
            self.decoded_file.write_text("decoded", encoding="utf-8")
            return True

        def process_index(self, output_file):
            return FakeCodeGraph()

    monkeypatch.setattr(
        smoke_scip_cold_start,
        "scip_cold_start_command_for_language",
        lambda language: [sys.executable],
    )
    monkeypatch.setattr(
        smoke_scip_cold_start,
        "LSIndexer",
        FakeIndexer,
    )

    project = tmp_path / "real-project"
    project.mkdir()
    result = smoke_scip_cold_start.run_project_smoke(
        "java",
        project,
        output_root=tmp_path / "out",
        expected_symbols=("Missing",),
    )

    assert result.status == "failed"
    assert result.missing_symbols == ("Missing",)
    assert result.error == "missing expected symbols"


def test_run_smoke_normalizes_kotlin_alias_for_missing_command(monkeypatch):
    monkeypatch.setattr(
        smoke_scip_cold_start,
        "scip_cold_start_command_for_language",
        lambda language: ["definitely-missing-scip-java"],
    )

    result = smoke_scip_cold_start.run_smoke("kt", skip_unavailable=True)

    assert result.ok
    assert result.language == "kotlin"
    assert result.command == ["definitely-missing-scip-java"]


def test_run_smoke_normalizes_csharp_alias_for_missing_command(monkeypatch):
    monkeypatch.setattr(
        smoke_scip_cold_start,
        "scip_cold_start_command_for_language",
        lambda language: ["definitely-missing-scip-dotnet"],
    )

    result = smoke_scip_cold_start.run_smoke("cs", skip_unavailable=True)

    assert result.ok
    assert result.language == "csharp"
    assert result.command == ["definitely-missing-scip-dotnet"]


def test_run_smoke_normalizes_ruby_alias_for_missing_command(monkeypatch):
    monkeypatch.setattr(
        smoke_scip_cold_start,
        "scip_cold_start_command_for_language",
        lambda language: ["definitely-missing-bundle"],
    )

    result = smoke_scip_cold_start.run_smoke("rb", skip_unavailable=True)

    assert result.ok
    assert result.language == "ruby"
    assert result.command == ["definitely-missing-bundle"]


def test_ruby_smoke_env_unsets_gem_path_for_bundler(tmp_path, monkeypatch):
    monkeypatch.setenv("GEM_HOME", "/tmp/codenib-tools/gems")
    monkeypatch.setenv("GEM_PATH", "/tmp/codenib-tools/gems")

    env = smoke_scip_cold_start._smoke_env("ruby", tmp_path)

    assert env["GEM_HOME"] == "/tmp/codenib-tools/gems"
    assert "GEM_PATH" not in env
    assert env["BUNDLE_GEMFILE"] == str(tmp_path / "Gemfile")


def test_run_smoke_skips_php_when_no_local_or_docker_command(monkeypatch):
    monkeypatch.setattr(
        smoke_scip_cold_start,
        "scip_cold_start_command_for_language",
        lambda language: ["vendor/bin/scip-php"],
    )
    monkeypatch.setattr(
        smoke_scip_cold_start,
        "_php_local_available",
        lambda command, project_root=None: False,
    )
    monkeypatch.setattr(smoke_scip_cold_start.shutil, "which", lambda command: None)

    result = smoke_scip_cold_start.run_smoke("php", skip_unavailable=True)

    assert result.ok
    assert result.language == "php"
    assert result.command == ["vendor/bin/scip-php"]


def test_smoke_index_command_adds_csharp_project_and_output(tmp_path):
    project = tmp_path / "Smoke.csproj"
    project.write_text('<Project Sdk="Microsoft.NET.Sdk" />\n', encoding="utf-8")

    assert smoke_scip_cold_start._smoke_index_command(
        "csharp",
        ["scip-dotnet"],
        tmp_path,
        tmp_path / "index.scip",
    ) == [
        "scip-dotnet",
        "index",
        str(project),
        "--output",
        str(tmp_path / "index.scip"),
        "--working-directory",
        str(tmp_path),
    ]


def test_smoke_index_command_adds_ruby_index_options(tmp_path):
    assert smoke_scip_cold_start._smoke_index_command(
        "ruby",
        ["bundle", "exec", "scip-ruby"],
        tmp_path,
        tmp_path / "index.scip",
    ) == [
        "bundle",
        "exec",
        "scip-ruby",
        "--dir",
        ".",
        "--index-file",
        str(tmp_path / "index.scip"),
        "--gem-metadata",
        "smoke@0.1.0",
        "--silence-dev-message",
        "--suppress-non-critical",
    ]


def test_smoke_index_command_uses_php_docker_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke_scip_cold_start, "_php_uses_docker", lambda command: True)

    assert smoke_scip_cold_start._smoke_index_command(
        "php",
        ["vendor/bin/scip-php"],
        tmp_path,
        tmp_path / "index.scip",
    ) == [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{smoke_scip_cold_start.os.getuid()}:{smoke_scip_cold_start.os.getgid()}",
        "-e",
        "HOME=/tmp",
        "-e",
        "COMPOSER_HOME=/tmp/composer",
        "-v",
        f"{tmp_path}:/app",
        "-w",
        "/app",
        "composer:2",
        "vendor/bin/scip-php",
    ]


def test_main_returns_success_for_skipped_unavailable_command(monkeypatch, capsys):
    monkeypatch.setattr(
        smoke_scip_cold_start,
        "scip_cold_start_command_for_language",
        lambda language: ["definitely-missing-scip-java"],
    )

    assert smoke_scip_cold_start.main(["--skip-unavailable", "--json"]) == 0
    assert '"status": "skipped"' in capsys.readouterr().out
