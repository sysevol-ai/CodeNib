#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Smoke-test generic LSP graph backends on generated or existing projects."""

from __future__ import annotations

import argparse
import json
import os
import shlex
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

from codeminer.graph.incremental.lsp_client import LSPClient
from codeminer.languages import normalize_graph_language
from codeminer.ls_router import LSIndexer
from codeminer.types import EDGE_TYPE_REFERENCE

SMOKE_LANGUAGES = ("java", "csharp", "ruby", "php", "kotlin")
_RUBY_LSP_VERSION = "0.26.9"
_SCIP_RUBY_VERSION = "0.4.7"


@dataclass(frozen=True, slots=True)
class SmokeResult:
    language: str
    status: str
    command: list[str] | None
    vertices: int = 0
    edges: int = 0
    references: int = 0
    min_references: int = 0
    expected_symbols: tuple[str, ...] = ()
    missing_symbols: tuple[str, ...] = ()
    graph_path: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "skipped"}

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "status": self.status,
            "command": self.command,
            "vertices": self.vertices,
            "edges": self.edges,
            "references": self.references,
            "min_references": self.min_references,
            "expected_symbols": list(self.expected_symbols),
            "missing_symbols": list(self.missing_symbols),
            "graph_path": self.graph_path,
            "error": self.error,
        }


def write_smoke_project(root: Path, language: str) -> tuple[str, ...]:
    """Write a minimal project for ``language`` and return symbol fragments."""

    if language == "java":
        _write(
            root / "pom.xml",
            """
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>app</groupId>
  <artifactId>smoke</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.source>21</maven.compiler.source>
    <maven.compiler.target>21</maven.compiler.target>
  </properties>
</project>
""".strip()
            + "\n",
        )
        _write(
            root / "src/main/java/app/Foo.java",
            "package app; public class Foo { public void run() {} }\n",
        )
        _write(
            root / "src/main/java/app/Bar.java",
            "package app; public class Bar { void call() { new Foo().run(); } }\n",
        )
        return ("Foo", "Bar")

    if language == "csharp":
        _write(
            root / "Smoke.csproj",
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
            "<TargetFramework>net10.0</TargetFramework>"
            "</PropertyGroup></Project>\n",
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
""".lstrip(),
        )
        return ("Invoice", "Helpers", "Normalize")

    if language == "ruby":
        _write(
            root / "Gemfile",
            """
source "https://rubygems.org"

gem "ruby-lsp", "{ruby_lsp_version}"
gem "scip-ruby", "{scip_ruby_version}"
""".format(
                ruby_lsp_version=_RUBY_LSP_VERSION,
                scip_ruby_version=_SCIP_RUBY_VERSION,
            ).strip()
            + "\n",
        )
        _write(
            root / "smoke.gemspec",
            """
Gem::Specification.new do |spec|
  spec.name = "smoke"
  spec.version = "0.1.0"
  spec.summary = "CodeNib ruby-lsp smoke project"
  spec.authors = ["CodeNib"]
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
        return ("Smoke", "Smoke.Invoice", "Smoke.normalize()")

    if language == "php":
        _write(
            root / "Invoice.php",
            "<?php\nclass Invoice { public function total(): int { return 1; } }\n"
            "function normalize(): void {}\n",
        )
        return ("Invoice", "normalize")

    if language == "kotlin":
        _write(
            root / "Invoice.kt",
            "class Invoice { fun total(): Int = 1 }\nfun normalize() {}\n",
        )
        return ("Invoice", "normalize")

    raise ValueError(f"Unsupported smoke language: {language}")


def run_smoke(
    language: str,
    *,
    include_references: bool = False,
    min_references: int = 0,
    skip_unavailable: bool = False,
    output_root: Path | None = None,
) -> SmokeResult:
    graph_language = normalize_graph_language(language)
    if graph_language not in SMOKE_LANGUAGES:
        return SmokeResult(
            language=language,
            status="failed",
            command=None,
            min_references=min_references,
            error=f"unsupported smoke language: {language}",
        )

    language = graph_language
    command = LSPClient.get_lsp_command(language)
    if not LSPClient.check_lsp_available(language):
        status = "skipped" if skip_unavailable else "failed"
        return SmokeResult(
            language=language,
            status=status,
            command=command,
            min_references=min_references,
            error="LSP command is not available",
        )

    if output_root is not None:
        root = output_root / language
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        return _run_smoke_in_root(
            language,
            command=command,
            root=root,
            include_references=include_references,
            min_references=min_references,
        )

    with tempfile.TemporaryDirectory(prefix=f"codeminer-{language}-lsp-") as tmp:
        return _run_smoke_in_root(
            language,
            command=command,
            root=Path(tmp),
            include_references=include_references,
            min_references=min_references,
        )


def run_project_smoke(
    language: str,
    project_root: Path,
    *,
    include_references: bool = False,
    min_references: int = 0,
    skip_unavailable: bool = False,
    output_root: Path | None = None,
    expected_symbols: Sequence[str] = (),
    target_dir: str | None = None,
    exclude_patterns: Sequence[str] = (),
) -> SmokeResult:
    graph_language = normalize_graph_language(language)
    if graph_language not in SMOKE_LANGUAGES:
        return SmokeResult(
            language=language,
            status="failed",
            command=None,
            min_references=min_references,
            expected_symbols=tuple(expected_symbols),
            error=f"unsupported smoke language: {language}",
        )

    language = graph_language
    command = LSPClient.get_lsp_command(language)
    if not LSPClient.check_lsp_available(language):
        status = "skipped" if skip_unavailable else "failed"
        return SmokeResult(
            language=language,
            status=status,
            command=command,
            min_references=min_references,
            expected_symbols=tuple(expected_symbols),
            error="LSP command is not available",
        )

    project_root = project_root.resolve()
    if not project_root.exists():
        return SmokeResult(
            language=language,
            status="failed",
            command=command,
            min_references=min_references,
            expected_symbols=tuple(expected_symbols),
            error=f"project root does not exist: {project_root}",
        )

    if output_root is not None:
        output_dir = output_root / f"{project_root.name}-{language}"
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True)
        return _run_lsp_indexer_smoke(
            language,
            command=command,
            root=project_root,
            output_dir=output_dir,
            include_references=include_references,
            min_references=min_references,
            expected_symbols=tuple(expected_symbols),
            target_dir=target_dir,
            exclude_patterns=tuple(exclude_patterns),
        )

    with tempfile.TemporaryDirectory(
        prefix=f"codeminer-{language}-lsp-project-"
    ) as tmp:
        return _run_lsp_indexer_smoke(
            language,
            command=command,
            root=project_root,
            output_dir=Path(tmp),
            include_references=include_references,
            min_references=min_references,
            expected_symbols=tuple(expected_symbols),
            target_dir=target_dir,
            exclude_patterns=tuple(exclude_patterns),
        )


def _run_smoke_in_root(
    language: str,
    *,
    command: list[str] | None,
    root: Path,
    include_references: bool,
    min_references: int,
) -> SmokeResult:
    expected = write_smoke_project(root, language)
    prepare_error = _prepare_smoke_project(language, root, command)
    if prepare_error:
        return SmokeResult(
            language=language,
            status="failed",
            command=command,
            min_references=min_references,
            expected_symbols=expected,
            error=prepare_error,
        )

    return _run_lsp_indexer_smoke(
        language,
        command=command,
        root=root,
        output_dir=root / "out",
        include_references=include_references,
        min_references=min_references,
        expected_symbols=expected,
        target_dir=_target_dir(language),
        exclude_patterns=tuple(_exclude_patterns(language)),
    )


def _run_lsp_indexer_smoke(
    language: str,
    *,
    command: list[str] | None,
    root: Path,
    output_dir: Path,
    include_references: bool,
    min_references: int,
    expected_symbols: tuple[str, ...],
    target_dir: str | None,
    exclude_patterns: Sequence[str],
) -> SmokeResult:
    effective_command = _effective_lsp_command(language, command)
    graph_path = output_dir / "graph.pkl"
    try:
        with _temporary_lsp_command(language, effective_command):
            graph = LSIndexer(
                root,
                language=language,
                output_dir=output_dir,
                exclude_patterns=list(exclude_patterns),
                graph_route="lsp",
            ).run_pipeline(
                report_profile=False,
                include_references=include_references,
                target_dir=target_dir,
            )
    except Exception as exc:
        return SmokeResult(
            language=language,
            status="failed",
            command=effective_command,
            min_references=min_references,
            expected_symbols=expected_symbols,
            error=str(exc),
        )

    if graph is None:
        return SmokeResult(
            language=language,
            status="failed",
            command=effective_command,
            min_references=min_references,
            expected_symbols=expected_symbols,
            error="LSIndexer returned no graph",
        )

    names = _graph_names(graph)
    missing = tuple(
        fragment
        for fragment in expected_symbols
        if not any(fragment in name for name in names)
    )
    references = _reference_count(graph)
    reference_gap = references < min_references
    status = "failed" if missing or reference_gap else "ok"
    error = None
    if missing:
        error = "missing expected symbols"
    elif reference_gap:
        error = f"expected at least {min_references} reference edges"
    return SmokeResult(
        language=language,
        status=status,
        command=effective_command,
        vertices=graph.graph.vcount(),
        edges=graph.graph.ecount(),
        references=references,
        min_references=min_references,
        expected_symbols=expected_symbols,
        missing_symbols=missing,
        graph_path=str(graph_path) if graph_path.exists() else None,
        error=error,
    )


def _graph_names(graph) -> tuple[str, ...]:
    names = []
    for vertex in graph.graph.vs:
        if hasattr(vertex, "attributes"):
            attrs = vertex.attributes()
        else:
            attrs = vertex
        for key in ("name", "unified_name"):
            value = attrs.get(key)
            if value:
                names.append(value)
    return tuple(names)


def _reference_count(graph) -> int:
    return sum(
        1
        for edge in graph.graph.es
        if edge.attributes().get("type") == EDGE_TYPE_REFERENCE
    )


def _prepare_smoke_project(
    language: str,
    root: Path,
    command: list[str] | None,
) -> str | None:
    if language != "ruby":
        return None

    bundle = _ruby_bundle_command(command)
    if bundle is None:
        return (
            "Ruby LSP smoke requires Bundler. Run make lsp-smoke-tools "
            "or set CODEMINER_RUBY_LSP_CMD to a Ruby installation with bundle."
        )

    for args in (
        [*bundle, "config", "set", "path", "vendor/bundle"],
        [*bundle, "install"],
    ):
        try:
            subprocess.run(
                args,
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired as exc:
            return f"Ruby bundle preparation timed out after 180s: {exc}"
        except subprocess.CalledProcessError as exc:
            detail = _combine_output(exc.stdout, exc.stderr)
            return f"Ruby bundle preparation failed: {detail or exc}"
    return None


def _effective_lsp_command(
    language: str,
    command: list[str] | None,
) -> list[str] | None:
    if language != "ruby" or not command:
        return command
    if Path(command[0]).name == "bundle":
        return command
    bundle = _ruby_bundle_command(command)
    if bundle is None:
        return command
    return [*bundle, "exec", "ruby-lsp"]


class _temporary_lsp_command:
    def __init__(self, language: str, command: list[str] | None):
        self._env_name = f"CODEMINER_{language.upper()}_LSP_CMD" if command else None
        self._command = command
        self._previous: str | None = None

    def __enter__(self):
        if self._env_name is None or self._command is None:
            return
        self._previous = os.environ.get(self._env_name)
        os.environ[self._env_name] = shlex.join(self._command)

    def __exit__(self, *exc):
        if self._env_name is None:
            return
        if self._previous is None:
            os.environ.pop(self._env_name, None)
        else:
            os.environ[self._env_name] = self._previous


def _ruby_bundle_command(command: list[str] | None) -> list[str] | None:
    if command and Path(command[0]).name == "bundle":
        return [command[0]]

    if not command:
        return None
    ruby_lsp = shutil.which(command[0]) or command[0]
    path = Path(ruby_lsp)
    try:
        resolved = path.resolve(strict=True)
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        first_line = lines[0]
    except (OSError, IndexError):
        first_line = ""
    if first_line.startswith("#!"):
        sibling = Path(first_line[2:].strip()).parent / "bundle"
        if sibling.exists():
            return [str(sibling)]

    bundle = shutil.which("bundle")
    if bundle:
        return [bundle]
    return None


def _combine_output(stdout: str | None, stderr: str | None) -> str:
    parts = []
    if stdout and stdout.strip():
        parts.append(stdout.strip())
    if stderr and stderr.strip():
        parts.append(stderr.strip())
    return "\n".join(parts)


def _exclude_patterns(language: str) -> list[str]:
    if language == "ruby":
        return ["vendor/**"]
    return []


def _target_dir(language: str) -> str | None:
    if language == "ruby":
        return "lib"
    return None


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--languages",
        nargs="+",
        default=list(SMOKE_LANGUAGES),
        help=f"languages to smoke-test; default: {' '.join(SMOKE_LANGUAGES)}",
    )
    parser.add_argument(
        "--reference-languages",
        nargs="*",
        default=[],
        help="languages that should collect reference edges during the smoke",
    )
    parser.add_argument(
        "--min-references",
        action="append",
        default=[],
        metavar="LANG=N",
        help="minimum reference-edge count required for a language",
    )
    parser.add_argument(
        "--skip-unavailable",
        action="store_true",
        help="treat missing LSP commands as skipped instead of failed",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="persist generated smoke projects and graph.pkl files under this directory",
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
        "--target-dir",
        help="relative source directory to index when running with --project-root",
    )
    parser.add_argument(
        "--exclude-pattern",
        action="append",
        default=[],
        help="fnmatch pattern to exclude from --project-root indexing; may be repeated",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    reference_languages = set(args.reference_languages)
    min_references = _parse_min_references(args.min_references)
    if args.project_root is not None:
        if len(args.languages) != 1:
            raise SystemExit("--project-root supports exactly one language")
        language = args.languages[0]
        results = [
            run_project_smoke(
                language,
                args.project_root,
                include_references=language in reference_languages,
                min_references=min_references.get(language, 0),
                skip_unavailable=args.skip_unavailable,
                output_root=args.output_dir,
                expected_symbols=tuple(args.expected_symbol),
                target_dir=args.target_dir,
                exclude_patterns=tuple(args.exclude_pattern),
            )
        ]
    else:
        results = [
            run_smoke(
                language,
                include_references=language in reference_languages,
                min_references=min_references.get(language, 0),
                skip_unavailable=args.skip_unavailable,
                output_root=args.output_dir,
            )
            for language in args.languages
        ]
    _print_results(results, json_output=args.json)
    return 0 if all(result.ok for result in results) else 1


def _print_results(results: Iterable[SmokeResult], *, json_output: bool) -> None:
    rows = list(results)
    if json_output:
        print(json.dumps([result.to_dict() for result in rows], indent=2))
        return
    for result in rows:
        detail = (
            f"vertices={result.vertices} edges={result.edges} "
            f"references={result.references}"
        )
        if result.error:
            detail = f"{detail} error={result.error}"
        print(f"{result.language}: {result.status} {detail}")


def _parse_min_references(values: Iterable[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for raw in values:
        language, separator, count_text = raw.partition("=")
        if not separator or not language or not count_text:
            raise SystemExit(f"Invalid --min-references value: {raw!r}")
        try:
            count = int(count_text)
        except ValueError as exc:
            raise SystemExit(f"Invalid reference count in {raw!r}") from exc
        if count < 0:
            raise SystemExit(f"Invalid negative reference count in {raw!r}")
        result[language] = count
    return result


if __name__ == "__main__":
    raise SystemExit(main())
