# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Quality checks shared by graph indexing and LSP replay experiments."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

TRANSLATION_UNIT_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".m", ".mm"})
_IGNORED_SOURCE_DIRS = frozenset(
    {
        ".cache",
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".svn",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
        "venv",
    }
)
_WERROR_RE = re.compile(r"(?<![A-Za-z0-9_])-Werror(?:=([A-Za-z0-9_-]+))?")


@dataclass(frozen=True)
class IndexQualityPolicy:
    """Thresholds for accepting a graph as an experiment artifact."""

    min_compile_db_entries: int = 1
    min_resolved_compile_db_ratio: float = 0.9
    min_source_coverage: float = 0.01
    min_graph_compdb_coverage: float = 0.5
    min_vertices: int = 1
    min_range_files: int = 1
    min_unified_symbols: int = 1
    min_baseline_vertex_ratio: float = 0.5
    min_baseline_edge_ratio: float = 0.5

    def __post_init__(self) -> None:
        if self.min_compile_db_entries < 0 or self.min_vertices < 0:
            raise ValueError("quality count thresholds must be non-negative")
        if self.min_range_files < 0 or self.min_unified_symbols < 0:
            raise ValueError("quality count thresholds must be non-negative")
        ratios = (
            self.min_resolved_compile_db_ratio,
            self.min_source_coverage,
            self.min_graph_compdb_coverage,
            self.min_baseline_vertex_ratio,
            self.min_baseline_edge_ratio,
        )
        if any(ratio < 0 or ratio > 1 for ratio in ratios):
            raise ValueError("quality ratio thresholds must be between 0 and 1")


def discover_compilation_database(
    project_root: str | Path,
    *,
    extra_candidates: Sequence[str | Path] = (),
) -> Optional[Path]:
    """Return the first parseable, non-empty compilation database."""

    root = Path(project_root).resolve()
    candidates = [
        root / "compile_commands.json",
        root / "build" / "compile_commands.json",
        *(Path(candidate) for candidate in extra_candidates),
    ]
    for candidate in candidates:
        if compilation_database_entry_count(candidate) > 0:
            return candidate.resolve()
    return None


def load_compilation_database(path: str | Path) -> list[dict[str, Any]]:
    """Load a compilation database, returning an empty list when invalid."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, list):
        return []
    return [dict(entry) for entry in payload if isinstance(entry, Mapping)]


def compilation_database_entry_count(path: str | Path) -> int:
    return len(load_compilation_database(path))


def prepare_compilation_database_for_indexing(
    source: str | Path,
    output: str | Path,
) -> tuple[Path, int]:
    """Write a clangd-facing copy with warning-as-error flags neutralized.

    Diagnostic severity does not change the translation unit represented by a
    compile command. Bear builds use non-fatal Make overrides where available;
    this derived copy strips any remaining ``-Werror`` flags from clangd input
    without changing the project's own build configuration.
    """

    entries = load_compilation_database(source)
    if not entries:
        raise ValueError(f"invalid or empty compilation database: {source}")

    rewrite_count = 0
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        updated = dict(entry)
        arguments = updated.get("arguments")
        if isinstance(arguments, list):
            rewritten = []
            for argument in arguments:
                value, count = _neutralize_werror(str(argument))
                rewritten.append(value)
                rewrite_count += count
            updated["arguments"] = rewritten
        command = updated.get("command")
        if isinstance(command, str):
            updated["command"], count = _neutralize_werror(command)
            rewrite_count += count
        normalized.append(updated)

    output_path = Path(output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(normalized, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path, rewrite_count


def compilation_database_stats(
    path: str | Path,
    *,
    project_root: str | Path,
) -> dict[str, Any]:
    """Measure compilation database size and repository source coverage."""

    root = Path(project_root).resolve()
    entries = load_compilation_database(path)
    files = _compilation_database_translation_units(entries, project_root=root)

    resolved_files = {path for path in files if path.is_file()}
    project_files = {path for path in resolved_files if _is_relative_to(path, root)}
    repository_sources = discover_translation_units(root)
    source_coverage = (
        len(project_files & repository_sources) / len(repository_sources)
        if repository_sources
        else None
    )
    resolved_ratio = len(resolved_files) / len(files) if files else None
    return {
        "path": str(Path(path).resolve()),
        "entry_count": len(entries),
        "translation_unit_count": len(files),
        "resolved_translation_unit_count": len(resolved_files),
        "project_translation_unit_count": len(project_files),
        "repository_translation_unit_count": len(repository_sources),
        "resolved_ratio": resolved_ratio,
        "source_coverage": source_coverage,
    }


def discover_translation_units(project_root: str | Path) -> set[Path]:
    """Discover source translation units while pruning generated directories."""

    root = Path(project_root).resolve()
    sources: set[Path] = set()
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in _IGNORED_SOURCE_DIRS
            and not name.startswith("build-")
            and not name.startswith("cmake-build-")
        ]
        base = Path(directory)
        for filename in filenames:
            path = base / filename
            if path.suffix.lower() in TRANSLATION_UNIT_SUFFIXES:
                sources.add(path.resolve())
    return sources


def assess_index_quality(
    graph: Any,
    *,
    project_root: str | Path,
    language: str,
    compdb_path: str | Path | None = None,
    baseline_graph: Any = None,
    policy: Optional[IndexQualityPolicy] = None,
) -> dict[str, Any]:
    """Return a JSON-ready quality report for an indexed graph."""

    selected_policy = policy or IndexQualityPolicy()
    language_key = language.lower()
    is_cpp = language_key in {"c", "c++", "cpp", "clang"}
    graph_stats = _graph_stats(graph)
    checks: list[dict[str, Any]] = []

    _minimum_check(
        checks,
        "graph_vertices",
        graph_stats["vertices"],
        selected_policy.min_vertices,
    )
    _minimum_check(
        checks,
        "range_metadata_files",
        graph_stats["range_file_count"],
        selected_policy.min_range_files,
    )
    if is_cpp:
        _minimum_check(
            checks,
            "unified_symbols",
            graph_stats["unified_symbol_count"],
            selected_policy.min_unified_symbols,
        )

    compdb_stats = None
    if is_cpp:
        if compdb_path is None:
            _failed_check(checks, "compile_db", "compilation database not found")
        else:
            compdb_stats = compilation_database_stats(
                compdb_path, project_root=project_root
            )
            compdb_files = _compilation_database_translation_units(
                load_compilation_database(compdb_path),
                project_root=Path(project_root).resolve(),
            )
            graph_files = _graph_range_paths(
                graph, project_root=Path(project_root).resolve()
            )
            graph_compdb_count = len(compdb_files & graph_files)
            graph_compdb_coverage = (
                graph_compdb_count / len(compdb_files) if compdb_files else None
            )
            compdb_stats.update(
                {
                    "graph_translation_unit_count": len(graph_files),
                    "graph_compdb_translation_unit_count": graph_compdb_count,
                    "graph_compdb_coverage": graph_compdb_coverage,
                }
            )
            _minimum_check(
                checks,
                "compile_db_entries",
                compdb_stats["entry_count"],
                selected_policy.min_compile_db_entries,
            )
            _ratio_check(
                checks,
                "compile_db_resolved_ratio",
                compdb_stats["resolved_ratio"],
                selected_policy.min_resolved_compile_db_ratio,
            )
            _ratio_check(
                checks,
                "source_coverage",
                compdb_stats["source_coverage"],
                selected_policy.min_source_coverage,
            )
            _ratio_check(
                checks,
                "graph_compdb_coverage",
                graph_compdb_coverage,
                selected_policy.min_graph_compdb_coverage,
            )

    baseline_stats = (
        _graph_stats(baseline_graph) if baseline_graph is not None else None
    )
    graph_ratios = None
    if baseline_stats is not None:
        graph_ratios = {
            "vertex_ratio": _safe_ratio(
                graph_stats["vertices"], baseline_stats["vertices"]
            ),
            "edge_ratio": _safe_ratio(graph_stats["edges"], baseline_stats["edges"]),
        }
        _ratio_check(
            checks,
            "baseline_vertex_ratio",
            graph_ratios["vertex_ratio"],
            selected_policy.min_baseline_vertex_ratio,
        )
        _ratio_check(
            checks,
            "baseline_edge_ratio",
            graph_ratios["edge_ratio"],
            selected_policy.min_baseline_edge_ratio,
        )

    failures = [check for check in checks if check["status"] == "fail"]
    return {
        "schema_version": 1,
        "passed": not failures,
        "language": language_key,
        "project_root": str(Path(project_root).resolve()),
        "policy": asdict(selected_policy),
        "graph": graph_stats,
        "compile_db": compdb_stats,
        "baseline_graph": baseline_stats,
        "graph_ratios": graph_ratios,
        "checks": checks,
        "failure_names": [check["name"] for check in failures],
    }


def _graph_stats(graph: Any) -> dict[str, int]:
    raw_graph = getattr(graph, "graph", None)
    vertices = len(raw_graph.vs) if raw_graph is not None else 0
    edges = len(raw_graph.es) if raw_graph is not None else 0
    file_nodes = getattr(graph, "_file_nodes", {}) or {}
    edge_anchors = getattr(graph, "_file_edge_anchors", {}) or {}
    unified = getattr(graph, "_unified_to_names", {}) or {}
    return {
        "vertices": vertices,
        "edges": edges,
        "file_node_count": len(file_nodes),
        "edge_anchor_file_count": len(edge_anchors),
        "range_file_count": len(set(file_nodes) | set(edge_anchors)),
        "unified_symbol_count": sum(len(names) for names in unified.values()),
    }


def _compilation_database_translation_units(
    entries: Sequence[Mapping[str, Any]],
    *,
    project_root: Path,
) -> set[Path]:
    files: set[Path] = set()
    for entry in entries:
        source = entry.get("file")
        if not source:
            continue
        source_path = Path(str(source))
        if not source_path.is_absolute():
            directory = Path(str(entry.get("directory") or project_root))
            if not directory.is_absolute():
                directory = project_root / directory
            source_path = directory / source_path
        if source_path.suffix.lower() in TRANSLATION_UNIT_SUFFIXES:
            files.add(source_path.resolve())
    return files


def _graph_range_paths(graph: Any, *, project_root: Path) -> set[Path]:
    file_nodes = getattr(graph, "_file_nodes", {}) or {}
    edge_anchors = getattr(graph, "_file_edge_anchors", {}) or {}
    paths: set[Path] = set()
    for value in set(file_nodes) | set(edge_anchors):
        path = Path(str(value))
        if not path.is_absolute():
            path = project_root / path
        if path.suffix.lower() in TRANSLATION_UNIT_SUFFIXES:
            paths.add(path.resolve())
    return paths


def _minimum_check(
    checks: list[dict[str, Any]], name: str, actual: int, minimum: int
) -> None:
    checks.append(
        {
            "name": name,
            "status": "pass" if actual >= minimum else "fail",
            "actual": actual,
            "minimum": minimum,
        }
    )


def _ratio_check(
    checks: list[dict[str, Any]],
    name: str,
    actual: Optional[float],
    minimum: float,
) -> None:
    checks.append(
        {
            "name": name,
            "status": "pass" if actual is not None and actual >= minimum else "fail",
            "actual": actual,
            "minimum": minimum,
        }
    )


def _failed_check(checks: list[dict[str, Any]], name: str, reason: str) -> None:
    checks.append({"name": name, "status": "fail", "reason": reason})


def _safe_ratio(value: int, baseline: int) -> Optional[float]:
    return value / baseline if baseline > 0 else None


def _neutralize_werror(value: str) -> tuple[str, int]:
    def replace(match: re.Match[str]) -> str:
        warning = match.group(1)
        return f"-Wno-error={warning}" if warning else "-Wno-error"

    return _WERROR_RE.subn(replace, value)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = [
    "IndexQualityPolicy",
    "assess_index_quality",
    "compilation_database_entry_count",
    "compilation_database_stats",
    "discover_compilation_database",
    "discover_translation_units",
    "load_compilation_database",
    "prepare_compilation_database_for_indexing",
]
