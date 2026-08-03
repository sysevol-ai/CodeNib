#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Build two graph routes for one project and compare backend alignment."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from codenib.graph.backend_alignment import (
    BackendAlignmentReport,
    BackendAlignmentTolerances,
    compare_backend_graphs,
)
from codenib.graph.code_graph import CodeGraph
from codenib.languages import normalize_graph_language, normalize_language
from codenib.ls_router import (
    ACTIVE_GRAPH_ROUTE,
    LSP_GRAPH_ROUTE,
    SCIP_CANDIDATE_GRAPH_ROUTE,
    build_graph_for_languages,
)
from codenib.paths import temp_state_dir

GRAPH_ROUTES = (ACTIVE_GRAPH_ROUTE, LSP_GRAPH_ROUTE, SCIP_CANDIDATE_GRAPH_ROUTE)


@dataclass(frozen=True, slots=True)
class RouteGraphSummary:
    """Small stable summary for one built route graph."""

    route: str
    output_dir: str
    graph_path: str
    vertices: int
    edges: int

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "output_dir": self.output_dir,
            "graph_path": self.graph_path,
            "vertices": self.vertices,
            "edges": self.edges,
        }


@dataclass(frozen=True, slots=True)
class RouteAlignmentResult:
    """Structured route-alignment report."""

    language: str
    project_root: str
    project_name: str
    target_dir: str | None
    exclude_patterns: tuple[str, ...]
    skip_level: str | None
    reference_include_references: bool
    candidate_include_references: bool
    reference: RouteGraphSummary
    candidate: RouteGraphSummary
    alignment: BackendAlignmentReport

    @property
    def ok(self) -> bool:
        return self.alignment.ok

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "language": self.language,
            "project_root": self.project_root,
            "project_name": self.project_name,
            "target_dir": self.target_dir,
            "exclude_patterns": list(self.exclude_patterns),
            "skip_level": self.skip_level,
            "reference_include_references": self.reference_include_references,
            "candidate_include_references": self.candidate_include_references,
            "reference": self.reference.to_dict(),
            "candidate": self.candidate.to_dict(),
            "alignment": self.alignment.to_dict(),
        }

    def summary(self) -> str:
        return (
            "route alignment "
            f"{'ok' if self.ok else 'mismatch'}: "
            f"language={self.language} "
            f"reference={self.reference.route} "
            f"candidate={self.candidate.route}; "
            f"graphs ref={self.reference.vertices}v/{self.reference.edges}e "
            f"cand={self.candidate.vertices}v/{self.candidate.edges}e; "
            f"{self.alignment.summary()}"
        )


def build_route_alignment(
    project_root: Path,
    language: str,
    *,
    output_dir: Path,
    reference_route: str = LSP_GRAPH_ROUTE,
    candidate_route: str = SCIP_CANDIDATE_GRAPH_ROUTE,
    project_name: str | None = None,
    skip_level: str | None = None,
    target_dir: str | None = None,
    exclude_patterns: Sequence[str] = (),
    reference_include_references: bool = False,
    candidate_include_references: bool = False,
    clean: bool = False,
    tolerances: BackendAlignmentTolerances | None = None,
) -> RouteAlignmentResult:
    """Build ``reference_route`` and ``candidate_route`` graphs, then compare."""

    project_root = project_root.resolve()
    if not project_root.exists():
        raise FileNotFoundError(f"project root does not exist: {project_root}")

    language_key = _display_language_key(language)
    pname = project_name or project_root.name
    base_output = output_dir / f"{pname}-{language_key}"
    reference_output = base_output / f"reference-{reference_route}"
    candidate_output = base_output / f"candidate-{candidate_route}"

    if clean:
        shutil.rmtree(reference_output, ignore_errors=True)
        shutil.rmtree(candidate_output, ignore_errors=True)

    reference_graph = _build_route_graph(
        project_root,
        reference_output,
        language=language,
        project_name=f"{pname}-{language_key}-{reference_route}",
        route=reference_route,
        skip_level=skip_level,
        target_dir=target_dir,
        exclude_patterns=exclude_patterns,
        include_references=reference_include_references,
    )
    candidate_graph = _build_route_graph(
        project_root,
        candidate_output,
        language=language,
        project_name=f"{pname}-{language_key}-{candidate_route}",
        route=candidate_route,
        skip_level=skip_level,
        target_dir=target_dir,
        exclude_patterns=exclude_patterns,
        include_references=candidate_include_references,
    )

    return RouteAlignmentResult(
        language=language_key,
        project_root=str(project_root),
        project_name=pname,
        target_dir=target_dir,
        exclude_patterns=tuple(exclude_patterns),
        skip_level=skip_level,
        reference_include_references=reference_include_references,
        candidate_include_references=candidate_include_references,
        reference=_summarize_route(reference_route, reference_output, reference_graph),
        candidate=_summarize_route(candidate_route, candidate_output, candidate_graph),
        alignment=compare_backend_graphs(
            reference_graph,
            candidate_graph,
            tolerances=tolerances,
        ),
    )


def _build_route_graph(
    project_root: Path,
    output_dir: Path,
    *,
    language: str,
    project_name: str,
    route: str,
    skip_level: str | None,
    target_dir: str | None,
    exclude_patterns: Sequence[str],
    include_references: bool,
) -> CodeGraph:
    graph = build_graph_for_languages(
        project_root,
        output_dir,
        languages=[language],
        project_name=project_name,
        skip_level=skip_level,
        target_dir=target_dir,
        include_references=include_references,
        exclude_patterns=list(exclude_patterns) if exclude_patterns else None,
        graph_route=route,
    )
    if graph is None:
        raise RuntimeError(f"graph route {route!r} returned no graph")
    return graph


def _summarize_route(
    route: str,
    output_dir: Path,
    graph: CodeGraph,
) -> RouteGraphSummary:
    graph_path = output_dir / "graph.pkl"
    return RouteGraphSummary(
        route=route,
        output_dir=str(output_dir),
        graph_path=str(graph_path),
        vertices=graph.graph.vcount(),
        edges=graph.graph.ecount(),
    )


def _display_language_key(language: str) -> str:
    return (
        normalize_graph_language(language) or normalize_language(language) or language
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--language", required=True)
    parser.add_argument(
        "--reference-route",
        choices=GRAPH_ROUTES,
        default=LSP_GRAPH_ROUTE,
    )
    parser.add_argument(
        "--candidate-route",
        choices=GRAPH_ROUTES,
        default=SCIP_CANDIDATE_GRAPH_ROUTE,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=temp_state_dir() / "graph-route-alignment",
    )
    parser.add_argument("--project-name")
    parser.add_argument(
        "--skip-level",
        choices=("none", "raw", "decode", "graph"),
        default="none",
        help="cache level to reuse; 'none' reruns the full route pipeline",
    )
    parser.add_argument(
        "--target-dir",
        help="relative source directory to index when a backend supports it",
    )
    parser.add_argument(
        "--exclude-pattern",
        action="append",
        default=[],
        help="fnmatch pattern to exclude from indexing; may be repeated",
    )
    parser.add_argument(
        "--reference-include-references",
        action="store_true",
        help="request reference-edge collection when the reference route supports it",
    )
    parser.add_argument(
        "--candidate-include-references",
        action="store_true",
        help="request reference-edge collection when the candidate route supports it",
    )
    parser.add_argument(
        "--clean", action="store_true", help="remove route caches first"
    )
    parser.add_argument("--max-missing-symbols", type=int, default=0)
    parser.add_argument("--max-extra-symbols", type=int, default=0)
    parser.add_argument("--max-missing-containment", type=int, default=0)
    parser.add_argument("--max-extra-containment", type=int, default=0)
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    tolerances = BackendAlignmentTolerances(
        max_missing_symbols=args.max_missing_symbols,
        max_extra_symbols=args.max_extra_symbols,
        max_missing_containment=args.max_missing_containment,
        max_extra_containment=args.max_extra_containment,
    )
    try:
        result = build_route_alignment(
            args.project_root,
            args.language,
            output_dir=args.output_dir,
            reference_route=args.reference_route,
            candidate_route=args.candidate_route,
            project_name=args.project_name,
            skip_level=None if args.skip_level == "none" else args.skip_level,
            target_dir=args.target_dir,
            exclude_patterns=tuple(args.exclude_pattern),
            reference_include_references=args.reference_include_references,
            candidate_include_references=args.candidate_include_references,
            clean=args.clean,
            tolerances=tolerances,
        )
    except Exception as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "project_root": str(args.project_root),
                        "language": args.language,
                        "reference_route": args.reference_route,
                        "candidate_route": args.candidate_route,
                        "error": str(exc),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"route alignment failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(result.summary())
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
