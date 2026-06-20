#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Smoke-test generic LSP graph backends on tiny generated projects."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from codeminer.graph.incremental.lsp_client import LSPClient
from codeminer.languages import normalize_graph_language
from codeminer.ls_router import LSIndexer
from codeminer.types import EDGE_TYPE_REFERENCE

SMOKE_LANGUAGES = ("java", "csharp", "ruby", "php", "kotlin")


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
            "namespace Smoke; class Invoice { public int Total() => 1; } "
            "class Program { static void Main() { } }\n",
        )
        return ("Invoice",)

    if language == "ruby":
        _write(
            root / "invoice.rb",
            "class Invoice\n  def total\n    1\n  end\nend\n\ndef normalize\nend\n",
        )
        return ("Invoice", "normalize")

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


def _run_smoke_in_root(
    language: str,
    *,
    command: list[str] | None,
    root: Path,
    include_references: bool,
    min_references: int,
) -> SmokeResult:
    expected = write_smoke_project(root, language)
    graph_path = root / "out" / "graph.pkl"
    try:
        graph = LSIndexer(
            root,
            language=language,
            output_dir=root / "out",
        ).run_pipeline(
            report_profile=False,
            include_references=include_references,
        )
    except Exception as exc:
        return SmokeResult(
            language=language,
            status="failed",
            command=command,
            min_references=min_references,
            expected_symbols=expected,
            error=str(exc),
        )

    if graph is None:
        return SmokeResult(
            language=language,
            status="failed",
            command=command,
            min_references=min_references,
            expected_symbols=expected,
            error="LSIndexer returned no graph",
        )

    names = tuple(vertex["name"] for vertex in graph.graph.vs)
    missing = tuple(
        fragment for fragment in expected if not any(fragment in name for name in names)
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
        command=command,
        vertices=graph.graph.vcount(),
        edges=graph.graph.ecount(),
        references=references,
        min_references=min_references,
        expected_symbols=expected,
        missing_symbols=missing,
        graph_path=str(graph_path) if graph_path.exists() else None,
        error=error,
    )


def _reference_count(graph) -> int:
    return sum(
        1
        for edge in graph.graph.es
        if edge.attributes().get("type") == EDGE_TYPE_REFERENCE
    )


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
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    reference_languages = set(args.reference_languages)
    min_references = _parse_min_references(args.min_references)
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
