#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Smoke-test registered SCIP cold-start candidates on tiny projects."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from codeminer.languages import (
    scip_cold_start_command_for_language,
    scip_cold_start_option,
)
from codeminer.ls_router import LSIndexer

SCIP_SMOKE_LANGUAGES = ("java", "kotlin", "scala", "csharp", "ruby", "php")
_SCIP_RUBY_VERSION = "0.4.7"
_SCIP_PHP_PACKAGE = "davidrjenni/scip-php:0.0.2"
_COMPOSER_DOCKER_IMAGE = "composer:2"
_JVM_ADD_EXPORTS = (
    "--add-exports=jdk.compiler/com.sun.tools.javac.model=ALL-UNNAMED",
    "--add-exports=jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED",
    "--add-exports=jdk.compiler/com.sun.tools.javac.tree=ALL-UNNAMED",
    "--add-exports=jdk.compiler/com.sun.tools.javac.util=ALL-UNNAMED",
    "--add-exports=jdk.compiler/com.sun.tools.javac.code=ALL-UNNAMED",
)


@dataclass(frozen=True, slots=True)
class ScipSmokeResult:
    language: str
    status: str
    command: list[str] | None
    index_path: str | None = None
    decoded_path: str | None = None
    graph_path: str | None = None
    index_bytes: int = 0
    vertices: int = 0
    edges: int = 0
    references: int = 0
    expected_files: tuple[str, ...] = ()
    expected_symbols: tuple[str, ...] = ()
    missing_symbols: tuple[str, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "skipped"}

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "status": self.status,
            "command": self.command,
            "index_path": self.index_path,
            "decoded_path": self.decoded_path,
            "graph_path": self.graph_path,
            "index_bytes": self.index_bytes,
            "vertices": self.vertices,
            "edges": self.edges,
            "references": self.references,
            "expected_files": list(self.expected_files),
            "expected_symbols": list(self.expected_symbols),
            "missing_symbols": list(self.missing_symbols),
            "error": self.error,
        }


def write_scip_smoke_project(root: Path, language: str) -> tuple[str, ...]:
    """Write a minimal SCIP-indexable project and return expected files."""

    if language == "java":
        _write(
            root / "pom.xml",
            """
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>app</groupId>
  <artifactId>scip-smoke</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.source>11</maven.compiler.source>
    <maven.compiler.target>11</maven.compiler.target>
  </properties>
</project>
""".strip()
            + "\n",
        )
        _write(
            root / "src/main/java/app/Foo.java",
            "package app; public class Foo { public int run() { return 1; } }\n",
        )
        _write(
            root / "src/main/java/app/Bar.java",
            (
                "package app; public class Bar { "
                "public int call() { return new Foo().run(); } }\n"
            ),
        )
        return ("pom.xml", "src/main/java/app/Foo.java", "src/main/java/app/Bar.java")

    if language == "kotlin":
        _write(
            root / "settings.gradle.kts",
            """
pluginManagement { repositories { gradlePluginPortal(); mavenCentral() } }
dependencyResolutionManagement { repositories { mavenCentral() } }
rootProject.name = "kotlin-scip-smoke"
""".strip()
            + "\n",
        )
        _write(
            root / "build.gradle.kts",
            """
plugins {
    kotlin("jvm") version "2.1.20"
}

java {
    toolchain { languageVersion.set(JavaLanguageVersion.of(21)) }
}
""".strip()
            + "\n",
        )
        _write(
            root / "src/main/kotlin/app/Invoice.kt",
            """
package app

class Invoice {
    fun total(): Int = 1
}

fun normalize(): String = Invoice().total().toString()
""".strip()
            + "\n",
        )
        _write(
            root / "src/test/kotlin/app/InvoiceTest.kt",
            """
package app

class InvoiceTest {
    fun testTotal(): Int = Invoice().total()
}
""".strip()
            + "\n",
        )
        return (
            "settings.gradle.kts",
            "build.gradle.kts",
            "src/main/kotlin/app/Invoice.kt",
            "src/test/kotlin/app/InvoiceTest.kt",
        )

    if language == "scala":
        _write(
            root / "settings.gradle",
            """
pluginManagement { repositories { gradlePluginPortal(); mavenCentral() } }
dependencyResolutionManagement { repositories { mavenCentral() } }
rootProject.name = "scala-scip-smoke"
""".strip()
            + "\n",
        )
        _write(
            root / "build.gradle",
            """
plugins {
    id "scala"
}

repositories { mavenCentral() }

dependencies {
    implementation "org.scala-lang:scala-library:2.13.12"
}

java {
    toolchain { languageVersion = JavaLanguageVersion.of(21) }
}
""".strip()
            + "\n",
        )
        _write(
            root / "src/main/scala/app/Invoice.scala",
            """
package app

class Invoice {
  def total(): Int = 1
}

object Helpers {
  def normalize(): String = new Invoice().total().toString
}
""".strip()
            + "\n",
        )
        return (
            "settings.gradle",
            "build.gradle",
            "src/main/scala/app/Invoice.scala",
        )

    if language == "csharp":
        _write(
            root / "Smoke.csproj",
            """
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <ImplicitUsings>enable</ImplicitUsings>
    <Nullable>enable</Nullable>
  </PropertyGroup>
</Project>
""".strip()
            + "\n",
        )
        _write(
            root / "Program.cs",
            """
namespace Smoke;

public class Invoice
{
    public int Total() => 1;
}

public static class Helpers
{
    public static string Normalize() => new Invoice().Total().ToString();
}
""".strip()
            + "\n",
        )
        return ("Smoke.csproj", "Program.cs")

    if language == "ruby":
        _write(
            root / "Gemfile",
            f"""
source "https://rubygems.org"

gemspec
gem "scip-ruby", {_SCIP_RUBY_VERSION!r}
""".strip()
            + "\n",
        )
        _write(
            root / "smoke.gemspec",
            """
Gem::Specification.new do |spec|
  spec.name = "smoke"
  spec.version = "0.1.0"
  spec.summary = "CodeMiner scip-ruby smoke project"
  spec.authors = ["CodeMiner"]
  spec.files = ["lib/invoice.rb"]
end
""".strip()
            + "\n",
        )
        _write(
            root / "lib/invoice.rb",
            """
module Smoke
  class Invoice
    def total
      1
    end
  end

  def self.normalize
    Invoice.new.total.to_s
  end
end
""".strip()
            + "\n",
        )
        return ("Gemfile", "smoke.gemspec", "lib/invoice.rb")

    if language == "php":
        _write(
            root / "composer.json",
            """
{
  "name": "codeminer/php-scip-smoke",
  "description": "CodeMiner scip-php smoke project",
  "type": "project",
  "autoload": {
    "psr-4": {
      "Smoke\\\\": "src/"
    }
  },
  "config": {
    "audit": { "block-insecure": false },
    "policy": { "advisories": { "block": false } }
  },
  "require": {}
}
""".strip()
            + "\n",
        )
        _write(
            root / "src/Billing/Invoice.php",
            """
<?php
namespace Smoke\\Billing;

class Invoice
{
    public function total(): int
    {
        return 1;
    }
}

function normalize(): string
{
    return (new Invoice())->total();
}
""".strip()
            + "\n",
        )
        return ("composer.json", "src/Billing/Invoice.php")

    raise ValueError(f"Unsupported SCIP smoke language: {language}")


def run_smoke(
    language: str,
    *,
    decode: bool = True,
    process_graph: bool = True,
    skip_unavailable: bool = False,
    output_root: Path | None = None,
    timeout: int = 180,
) -> ScipSmokeResult:
    option = scip_cold_start_option(language)
    if option is None:
        return ScipSmokeResult(
            language=language,
            status="failed",
            command=None,
            error=f"no SCIP cold-start option registered for {language}",
        )

    language = _canonical_smoke_language(language)
    if language not in SCIP_SMOKE_LANGUAGES:
        return ScipSmokeResult(
            language=language,
            status="failed",
            command=scip_cold_start_command_for_language(language),
            error=f"unsupported SCIP smoke language: {language}",
        )

    command = scip_cold_start_command_for_language(language)
    if not command:
        return ScipSmokeResult(
            language=language,
            status="failed",
            command=None,
            error=f"no SCIP command registered for {language}",
        )

    if not _smoke_command_available(language, command):
        status = "skipped" if skip_unavailable else "failed"
        return ScipSmokeResult(
            language=language,
            status=status,
            command=command,
            error="SCIP command is not available",
        )

    if output_root is not None:
        root = output_root / language
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        return _run_smoke_in_root(
            language,
            root=root,
            command=command,
            decode=decode,
            process_graph=process_graph,
            timeout=timeout,
        )

    with tempfile.TemporaryDirectory(prefix=f"codeminer-{language}-scip-") as tmp:
        return _run_smoke_in_root(
            language,
            root=Path(tmp),
            command=command,
            decode=decode,
            process_graph=process_graph,
            timeout=timeout,
        )


def run_project_smoke(
    language: str,
    project_root: Path,
    *,
    decode: bool = True,
    process_graph: bool = True,
    skip_unavailable: bool = False,
    output_root: Path | None = None,
    expected_symbols: Sequence[str] = (),
    timeout: int = 180,
) -> ScipSmokeResult:
    option = scip_cold_start_option(language)
    if option is None:
        return ScipSmokeResult(
            language=language,
            status="failed",
            command=None,
            error=f"no SCIP cold-start option registered for {language}",
        )

    language = _canonical_smoke_language(language)
    if language not in SCIP_SMOKE_LANGUAGES:
        return ScipSmokeResult(
            language=language,
            status="failed",
            command=scip_cold_start_command_for_language(language),
            error=f"unsupported SCIP smoke language: {language}",
        )

    command = scip_cold_start_command_for_language(language)
    if not command:
        return ScipSmokeResult(
            language=language,
            status="failed",
            command=None,
            error=f"no SCIP command registered for {language}",
        )
    if not _smoke_command_available(
        language,
        command,
        project_root=project_root,
        allow_docker=False,
    ):
        status = "skipped" if skip_unavailable else "failed"
        return ScipSmokeResult(
            language=language,
            status=status,
            command=command,
            error="SCIP command is not available",
        )

    project_root = project_root.resolve()
    if not project_root.exists():
        return ScipSmokeResult(
            language=language,
            status="failed",
            command=command,
            error=f"project root does not exist: {project_root}",
        )

    if output_root is not None:
        output_dir = output_root / f"{project_root.name}-{language}"
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True)
        return _run_project_smoke_to_output(
            language,
            project_root=project_root,
            output_dir=output_dir,
            command=command,
            decode=decode,
            process_graph=process_graph,
            expected_symbols=tuple(expected_symbols),
            timeout=timeout,
        )

    with tempfile.TemporaryDirectory(
        prefix=f"codeminer-{language}-scip-project-"
    ) as tmp:
        return _run_project_smoke_to_output(
            language,
            project_root=project_root,
            output_dir=Path(tmp),
            command=command,
            decode=decode,
            process_graph=process_graph,
            expected_symbols=tuple(expected_symbols),
            timeout=timeout,
        )


def _run_smoke_in_root(
    language: str,
    *,
    root: Path,
    command: list[str],
    decode: bool,
    process_graph: bool,
    timeout: int,
) -> ScipSmokeResult:
    expected_files = write_scip_smoke_project(root, language)
    expected_symbols = _expected_symbols(language)
    index_path = root / "index.scip"
    decoded_path = root / "index.decoded"
    graph_path = root / "graph.pkl"
    prepare_error = _prepare_smoke_project(language, root, command, timeout)
    if prepare_error:
        return ScipSmokeResult(
            language=language,
            status="failed",
            command=command,
            expected_files=expected_files,
            expected_symbols=expected_symbols,
            error=prepare_error,
        )
    run_command = _smoke_index_command(language, command, root, index_path)
    try:
        subprocess.run(
            run_command,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_smoke_env(language, root),
        )
    except subprocess.TimeoutExpired as exc:
        return ScipSmokeResult(
            language=language,
            status="failed",
            command=run_command,
            expected_files=expected_files,
            expected_symbols=expected_symbols,
            error=f"SCIP command timed out after {timeout}s: {exc}",
        )
    except subprocess.CalledProcessError as exc:
        detail = _combine_command_output(exc.stdout, exc.stderr) or str(exc)
        return ScipSmokeResult(
            language=language,
            status="failed",
            command=run_command,
            expected_files=expected_files,
            expected_symbols=expected_symbols,
            error=f"SCIP command failed: {detail}",
        )

    if not index_path.exists():
        return ScipSmokeResult(
            language=language,
            status="failed",
            command=run_command,
            expected_files=expected_files,
            expected_symbols=expected_symbols,
            error=f"SCIP command did not create {index_path.name}",
        )
    index_bytes = index_path.stat().st_size
    if index_bytes <= 0:
        return ScipSmokeResult(
            language=language,
            status="failed",
            command=run_command,
            index_path=str(index_path),
            index_bytes=index_bytes,
            expected_files=expected_files,
            expected_symbols=expected_symbols,
            error="SCIP index is empty",
        )

    if decode:
        decode_error = _decode_scip_index(index_path, decoded_path)
        if decode_error:
            return ScipSmokeResult(
                language=language,
                status="failed",
                command=run_command,
                index_path=str(index_path),
                index_bytes=index_bytes,
                expected_files=expected_files,
                expected_symbols=expected_symbols,
                error=decode_error,
            )

    vertices = 0
    edges = 0
    references = 0
    missing_symbols: tuple[str, ...] = ()
    if decode and process_graph:
        try:
            graph = _decode_graph(language, decoded_path, root)
            graph.build_range_indexes()
            graph.save_graph(str(graph_path))
            vertices = graph.graph.vcount()
            edges = graph.graph.ecount()
            references = _reference_count(graph)
            names = tuple(
                (vertex.attributes().get("unified_name") or vertex["name"])
                for vertex in graph.graph.vs
            )
            missing_symbols = tuple(
                fragment
                for fragment in expected_symbols
                if not any(fragment in name for name in names)
            )
        except Exception as exc:
            return ScipSmokeResult(
                language=language,
                status="failed",
                command=run_command,
                index_path=str(index_path),
                decoded_path=str(decoded_path) if decoded_path.exists() else None,
                index_bytes=index_bytes,
                expected_files=expected_files,
                expected_symbols=expected_symbols,
                error=f"SCIP graph decode failed: {exc}",
            )

        if missing_symbols:
            return ScipSmokeResult(
                language=language,
                status="failed",
                command=run_command,
                index_path=str(index_path),
                decoded_path=str(decoded_path),
                graph_path=str(graph_path) if graph_path.exists() else None,
                index_bytes=index_bytes,
                vertices=vertices,
                edges=edges,
                references=references,
                expected_files=expected_files,
                expected_symbols=expected_symbols,
                missing_symbols=missing_symbols,
                error="missing expected symbols",
            )

    return ScipSmokeResult(
        language=language,
        status="ok",
        command=run_command,
        index_path=str(index_path),
        decoded_path=str(decoded_path) if decode and decoded_path.exists() else None,
        graph_path=str(graph_path) if graph_path.exists() else None,
        index_bytes=index_bytes,
        vertices=vertices,
        edges=edges,
        references=references,
        expected_files=expected_files,
        expected_symbols=expected_symbols,
    )


def _run_project_smoke_to_output(
    language: str,
    *,
    project_root: Path,
    output_dir: Path,
    command: list[str],
    decode: bool,
    process_graph: bool,
    expected_symbols: tuple[str, ...],
    timeout: int,
) -> ScipSmokeResult:
    indexer = LSIndexer(
        project_root,
        output_dir=output_dir,
        language=language,
        graph_route="scip-candidate",
    )

    try:
        generated = indexer.generate_index(timeout=timeout)
    except Exception as exc:
        return ScipSmokeResult(
            language=language,
            status="failed",
            command=command,
            expected_symbols=expected_symbols,
            error=f"SCIP command failed: {exc}",
        )
    if not generated:
        return ScipSmokeResult(
            language=language,
            status="failed",
            command=command,
            expected_symbols=expected_symbols,
            error="SCIP command failed",
        )

    index_bytes = (
        indexer.index_file.stat().st_size if indexer.index_file.exists() else 0
    )
    if index_bytes <= 0:
        return ScipSmokeResult(
            language=language,
            status="failed",
            command=command,
            index_path=str(indexer.index_file) if indexer.index_file.exists() else None,
            index_bytes=index_bytes,
            expected_symbols=expected_symbols,
            error="SCIP index is empty",
        )

    if decode and not indexer.decode_index():
        return ScipSmokeResult(
            language=language,
            status="failed",
            command=command,
            index_path=str(indexer.index_file),
            index_bytes=index_bytes,
            expected_symbols=expected_symbols,
            error="failed to decode SCIP index",
        )

    vertices = 0
    edges = 0
    references = 0
    missing_symbols: tuple[str, ...] = ()
    if decode and process_graph:
        graph = indexer.process_index(output_file=str(indexer.graph_file))
        if graph is None:
            return ScipSmokeResult(
                language=language,
                status="failed",
                command=command,
                index_path=str(indexer.index_file),
                decoded_path=(
                    str(indexer.decoded_file) if indexer.decoded_file.exists() else None
                ),
                index_bytes=index_bytes,
                expected_symbols=expected_symbols,
                error="SCIP graph decode failed",
            )
        vertices = graph.graph.vcount()
        edges = graph.graph.ecount()
        references = _reference_count(graph)
        names = tuple(
            (vertex.attributes().get("unified_name") or vertex["name"])
            for vertex in graph.graph.vs
        )
        missing_symbols = tuple(
            fragment
            for fragment in expected_symbols
            if not any(fragment in name for name in names)
        )
        if missing_symbols:
            return ScipSmokeResult(
                language=language,
                status="failed",
                command=command,
                index_path=str(indexer.index_file),
                decoded_path=(
                    str(indexer.decoded_file) if indexer.decoded_file.exists() else None
                ),
                graph_path=(
                    str(indexer.graph_file) if indexer.graph_file.exists() else None
                ),
                index_bytes=index_bytes,
                vertices=vertices,
                edges=edges,
                references=references,
                expected_symbols=expected_symbols,
                missing_symbols=missing_symbols,
                error="missing expected symbols",
            )

    return ScipSmokeResult(
        language=language,
        status="ok",
        command=command,
        index_path=str(indexer.index_file),
        decoded_path=(
            str(indexer.decoded_file)
            if decode and indexer.decoded_file.exists()
            else None
        ),
        graph_path=str(indexer.graph_file) if indexer.graph_file.exists() else None,
        index_bytes=index_bytes,
        vertices=vertices,
        edges=edges,
        references=references,
        expected_symbols=expected_symbols,
    )


def _combine_command_output(stdout: str | None, stderr: str | None) -> str:
    parts = []
    if stdout and stdout.strip():
        parts.append(stdout.strip())
    if stderr and stderr.strip():
        parts.append(stderr.strip())
    return "\n".join(parts)


def _decode_scip_index(index_path: Path, decoded_path: Path) -> str | None:
    protoc = shutil.which("protoc")
    if protoc is None:
        return "protoc is not available to decode SCIP index"

    proto_dir = _PROJECT_ROOT / "codeminer" / "scip_interface"
    cmd = [
        protoc,
        "--decode=scip.Index",
        f"--proto_path={proto_dir}",
        "scip.proto",
    ]
    try:
        with index_path.open("rb") as stdin, decoded_path.open("w") as stdout:
            subprocess.run(
                cmd,
                cwd=proto_dir,
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.PIPE,
                text=False,
                check=True,
            )
    except subprocess.CalledProcessError as exc:
        decoded_path.unlink(missing_ok=True)
        return (
            "failed to decode SCIP index with protoc: "
            f"{exc.stderr.decode(errors='replace').strip()}"
        )
    return None


def _decode_graph(language: str, decoded_path: Path, root: Path):
    if language in {"java", "kotlin", "scala"}:
        from codeminer.scip_interface.scip_decode_java import SCIPJavaGraphDecoder

        return SCIPJavaGraphDecoder(str(decoded_path), project_root=str(root)).decode()
    if language == "csharp":
        from codeminer.scip_interface.scip_decode_csharp import SCIPCSharpGraphDecoder

        return SCIPCSharpGraphDecoder(
            str(decoded_path), project_root=str(root)
        ).decode()
    if language == "ruby":
        from codeminer.scip_interface.scip_decode_ruby import SCIPRubyGraphDecoder

        return SCIPRubyGraphDecoder(str(decoded_path), project_root=str(root)).decode()
    if language == "php":
        from codeminer.scip_interface.scip_decode_php import SCIPPHPGraphDecoder

        return SCIPPHPGraphDecoder(str(decoded_path), project_root=str(root)).decode()
    raise ValueError(f"Unsupported SCIP graph decode language: {language}")


def _reference_count(graph) -> int:
    return sum(
        1 for edge in graph.graph.es if edge.attributes().get("type") == "reference"
    )


def _expected_symbols(language: str) -> tuple[str, ...]:
    if language == "java":
        return ("Foo", "Foo.run()", "Bar", "Bar.call()")
    if language == "kotlin":
        return ("Invoice", "Invoice.total()", "normalize()")
    if language == "scala":
        return ("Invoice", "Invoice.total()", "Helpers", "Helpers.normalize()")
    if language == "csharp":
        return ("Invoice", "Invoice.Total()", "Helpers", "Helpers.Normalize()")
    if language == "ruby":
        return ("Smoke", "Smoke.Invoice", "Smoke.Invoice.total()", "Smoke.normalize()")
    if language == "php":
        return ("Invoice", "Invoice.total()", "normalize()")
    return ()


def _canonical_smoke_language(language: str) -> str:
    normalized = language.lower().strip()
    if normalized in {
        "java",
        "kotlin",
        "kt",
        "scala",
        "csharp",
        "c#",
        "cs",
        "ruby",
        "rb",
        "php",
    }:
        if normalized in {"c#", "cs"}:
            return "csharp"
        if normalized == "rb":
            return "ruby"
        return "kotlin" if normalized == "kt" else normalized
    return normalized


def _smoke_index_command(
    language: str,
    command: list[str],
    root: Path,
    index_path: Path,
) -> list[str]:
    if language != "csharp":
        if language == "php":
            if _php_uses_docker(command):
                return _php_docker_command(root, "vendor/bin/scip-php")
            return command

        if language != "ruby":
            return command

        run_command = list(command)
        if "--dir" not in run_command:
            run_command.extend(["--dir", "."])
        if "--index-file" not in run_command:
            run_command.extend(["--index-file", str(index_path)])
        if "--gem-metadata" not in run_command:
            run_command.extend(["--gem-metadata", "smoke@0.1.0"])
        for flag in ("--silence-dev-message", "--suppress-non-critical"):
            if flag not in run_command:
                run_command.append(flag)
        return run_command

    run_command = list(command)
    if "index" not in run_command[1:]:
        run_command.append("index")
    project = root / "Smoke.csproj"
    if str(project) not in run_command:
        run_command.append(str(project))
    if "--output" not in run_command:
        run_command.extend(["--output", str(index_path)])
    if "--working-directory" not in run_command:
        run_command.extend(["--working-directory", str(root)])
    return run_command


def _prepare_smoke_project(
    language: str,
    root: Path,
    command: list[str],
    timeout: int,
) -> str | None:
    if language != "ruby":
        if language == "php":
            return _prepare_php_smoke_project(root, command, timeout)
        return None

    bundle_prefix = _bundle_prefix(command)
    if not bundle_prefix:
        return (
            "Ruby SCIP smoke requires a Bundler command such as bundle exec scip-ruby"
        )
    for args in (
        [*bundle_prefix, "config", "set", "path", "vendor/bundle"],
        [*bundle_prefix, "install"],
    ):
        try:
            subprocess.run(
                args,
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=_smoke_env(language, root),
            )
        except subprocess.TimeoutExpired as exc:
            return f"Ruby bundle preparation timed out after {timeout}s: {exc}"
        except subprocess.CalledProcessError as exc:
            detail = _combine_command_output(exc.stdout, exc.stderr) or str(exc)
            return f"Ruby bundle preparation failed: {detail}"
    return None


def _smoke_command_available(
    language: str,
    command: list[str],
    *,
    project_root: Path | None = None,
    allow_docker: bool = True,
) -> bool:
    if language == "php":
        if _php_local_available(command, project_root=project_root):
            return True
        return (
            allow_docker
            and _php_docker_compatible(command)
            and shutil.which("docker") is not None
        )
    return bool(command and shutil.which(command[0]) is not None)


def _prepare_php_smoke_project(
    root: Path,
    command: list[str],
    timeout: int,
) -> str | None:
    git_error = _init_git_smoke_root(root, timeout=timeout)
    if git_error:
        return git_error

    if _php_uses_docker(command):
        commands = (
            _php_docker_shell(
                root,
                "git config --global --add safe.directory /app "
                f"&& composer require --dev {_SCIP_PHP_PACKAGE} "
                "--no-interaction --no-progress --no-security-blocking "
                "&& composer dump-autoload -o --no-interaction",
            ),
            _php_docker_shell(
                root,
                "composer install --no-interaction --no-progress "
                "--no-security-blocking",
                workdir="/app/vendor/davidrjenni/scip-php",
            ),
        )
    else:
        commands = (
            [
                "composer",
                "require",
                "--dev",
                _SCIP_PHP_PACKAGE,
                "--no-interaction",
                "--no-progress",
                "--no-security-blocking",
            ],
            ["composer", "dump-autoload", "-o", "--no-interaction"],
            [
                "composer",
                "install",
                "--no-interaction",
                "--no-progress",
                "--no-security-blocking",
            ],
        )

    for index, args in enumerate(commands):
        cwd = root
        if not _php_uses_docker(command) and index == 2:
            cwd = root / "vendor/davidrjenni/scip-php"
        try:
            subprocess.run(
                args,
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return f"PHP Composer preparation timed out after {timeout}s: {exc}"
        except subprocess.CalledProcessError as exc:
            detail = _combine_command_output(exc.stdout, exc.stderr) or str(exc)
            return f"PHP Composer preparation failed: {detail}"
    return None


def _init_git_smoke_root(root: Path, *, timeout: int) -> str | None:
    git = shutil.which("git")
    if git is None:
        return "git is required for scip-php smoke metadata"
    commands = (
        [git, "init"],
        [git, "config", "user.email", "codeminer@example.invalid"],
        [git, "config", "user.name", "CodeMiner"],
        [git, "add", "composer.json", "src/Billing/Invoice.php"],
        [git, "commit", "-m", "php scip smoke"],
    )
    for args in commands:
        try:
            subprocess.run(
                args,
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.CalledProcessError as exc:
            detail = _combine_command_output(exc.stdout, exc.stderr) or str(exc)
            return f"PHP git preparation failed: {detail}"
        except subprocess.TimeoutExpired as exc:
            return f"PHP git preparation timed out after {timeout}s: {exc}"
    return None


def _php_local_available(
    command: list[str],
    *,
    project_root: Path | None = None,
) -> bool:
    if shutil.which("php") is None or shutil.which("composer") is None:
        return False
    if not command:
        return False
    if "/" not in command[0]:
        return shutil.which(command[0]) is not None
    if project_root is None:
        return True
    return (project_root / command[0]).exists()


def _php_uses_docker(command: list[str]) -> bool:
    return (
        not _php_local_available(command)
        and _php_docker_compatible(command)
        and shutil.which("docker") is not None
    )


def _php_docker_compatible(command: list[str]) -> bool:
    return len(command) == 1 and Path(command[0]).name == "scip-php"


def _php_docker_command(root: Path, *args: str, workdir: str = "/app") -> list[str]:
    image = os.environ.get("CODEMINER_PHP_COMPOSER_IMAGE", _COMPOSER_DOCKER_IMAGE)
    return [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        "HOME=/tmp",
        "-e",
        "COMPOSER_HOME=/tmp/composer",
        "-v",
        f"{root}:/app",
        "-w",
        workdir,
        image,
        *args,
    ]


def _php_docker_shell(root: Path, script: str, *, workdir: str = "/app") -> list[str]:
    return _php_docker_command(root, "sh", "-lc", script, workdir=workdir)


def _smoke_env(language: str, root: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if language in {"java", "kotlin", "scala"}:
        existing = env.get("JDK_JAVA_OPTIONS", "").strip()
        additions = " ".join(_JVM_ADD_EXPORTS)
        env["JDK_JAVA_OPTIONS"] = (
            f"{existing} {additions}".strip() if existing else additions
        )
    if language == "csharp":
        env.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")
    if language == "ruby" and root is not None:
        env.pop("GEM_PATH", None)
        env["BUNDLE_GEMFILE"] = str(root / "Gemfile")
    return env


def _bundle_prefix(command: list[str]) -> list[str]:
    if not command or Path(command[0]).name != "bundle":
        return []
    try:
        exec_index = command.index("exec")
    except ValueError:
        return [command[0]]
    return command[:exec_index]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--languages",
        nargs="+",
        default=list(SCIP_SMOKE_LANGUAGES),
        help=f"languages to smoke-test; default: {' '.join(SCIP_SMOKE_LANGUAGES)}",
    )
    parser.add_argument(
        "--skip-unavailable",
        action="store_true",
        help="treat missing SCIP commands as skipped instead of failed",
    )
    parser.add_argument(
        "--skip-decode",
        action="store_true",
        help="only require index.scip creation; do not run protoc decode",
    )
    parser.add_argument(
        "--skip-graph",
        action="store_true",
        help="do not decode index.decoded into CodeGraph",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        help="run smoke against an existing project root instead of generating one",
    )
    parser.add_argument(
        "--expected-symbol",
        action="append",
        default=[],
        help="symbol fragment expected in the decoded graph; may be repeated",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="persist generated smoke projects or project artifacts under this directory",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="per-language SCIP command timeout in seconds",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.project_root is not None:
        if len(args.languages) != 1:
            raise SystemExit("--project-root supports exactly one language")
        results = [
            run_project_smoke(
                args.languages[0],
                args.project_root,
                decode=not args.skip_decode,
                process_graph=not args.skip_graph,
                skip_unavailable=args.skip_unavailable,
                output_root=args.output_dir,
                expected_symbols=tuple(args.expected_symbol),
                timeout=args.timeout,
            )
        ]
    else:
        results = [
            run_smoke(
                language,
                decode=not args.skip_decode,
                process_graph=not args.skip_graph,
                skip_unavailable=args.skip_unavailable,
                output_root=args.output_dir,
                timeout=args.timeout,
            )
            for language in args.languages
        ]
    _print_results(results, json_output=args.json)
    return 0 if all(result.ok for result in results) else 1


def _print_results(results: Iterable[ScipSmokeResult], *, json_output: bool) -> None:
    rows = list(results)
    if json_output:
        print(json.dumps([result.to_dict() for result in rows], indent=2))
        return
    for result in rows:
        detail = f"bytes={result.index_bytes}"
        if result.index_path:
            detail = f"{detail} index={result.index_path}"
        if result.decoded_path:
            detail = f"{detail} decoded={result.decoded_path}"
        if result.graph_path:
            detail = (
                f"{detail} graph={result.graph_path} "
                f"vertices={result.vertices} edges={result.edges} "
                f"references={result.references}"
            )
        if result.error:
            detail = f"{detail} error={result.error}"
        print(f"{result.language}: {result.status} {detail}")


if __name__ == "__main__":
    raise SystemExit(main())
