#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Profile large real repositories for SCIP acceleration decisions.

The generated report answers one specific question per repository: is local
SCIP decode/build time large enough to justify a C++ decoder, or is cold-start
time still dominated by the external language indexer?

This script is intentionally manifest-driven. It supports ``--dry-run`` so the
repo list and gate configuration can be checked in unit/CI without cloning or
indexing large upstream projects.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_CORE = _PROJECT_ROOT / "build" / "core"
if _CORE.exists() and str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

DEFAULT_MANIFEST = Path(__file__).with_name("large_scip_repos.yml")
DEFAULT_CACHE_ROOT = Path("/tmp/codeminer-large-scip-repos")
DEFAULT_OUTPUT_DIR = Path("/tmp/codeminer-large-scip-profile")
DEFAULT_TIMEOUT_S = 1800
DEFAULT_ACCELERATION_THRESHOLD = 0.20

LANGUAGE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "csharp": (".cs",),
    "java": (".java",),
    "kotlin": (".kt", ".kts"),
    "php": (".php", ".phtml"),
    "ruby": (".rb",),
    "scala": (".scala", ".sc"),
}


@dataclass(frozen=True, slots=True)
class LargeScipRepo:
    name: str
    language: str
    url: str
    ref: str = "HEAD"
    target_dir: str | None = None
    exclude_patterns: tuple[str, ...] = ()
    expected_min_source_files: int = 0
    notes: str = ""
    submodules: bool = False

    def to_plan_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "language": self.language,
            "url": self.url,
            "ref": self.ref,
            "target_dir": self.target_dir,
            "exclude_patterns": list(self.exclude_patterns),
            "expected_min_source_files": self.expected_min_source_files,
            "submodules": self.submodules,
            "notes": self.notes,
        }


@dataclass(slots=True)
class BackendProfile:
    backend: str
    status: str = "skipped"
    timings: dict[str, float] = field(default_factory=dict)
    graph: dict[str, int] = field(default_factory=dict)
    artifacts: dict[str, str | int | None] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_manifest(path: Path = DEFAULT_MANIFEST) -> list[LargeScipRepo]:
    """Load and validate a large-repo profiling manifest."""

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = data.get("repos")
    if not isinstance(rows, list):
        raise ValueError("large SCIP manifest must contain a 'repos' list.")

    repos: list[LargeScipRepo] = []
    names: set[str] = set()
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"manifest repo #{idx + 1} must be a mapping.")
        missing = [key for key in ("name", "language", "url") if not row.get(key)]
        if missing:
            raise ValueError(
                f"manifest repo #{idx + 1} missing required keys: {missing}"
            )
        name = str(row["name"])
        if name in names:
            raise ValueError(f"duplicate large SCIP repo name: {name}")
        names.add(name)
        repos.append(
            LargeScipRepo(
                name=name,
                language=str(row["language"]).strip().lower(),
                url=str(row["url"]),
                ref=str(row.get("ref") or "HEAD"),
                target_dir=(
                    str(row["target_dir"]).strip()
                    if row.get("target_dir") is not None
                    else None
                ),
                exclude_patterns=tuple(str(v) for v in row.get("exclude_patterns", ())),
                expected_min_source_files=int(
                    row.get("expected_min_source_files") or 0
                ),
                notes=str(row.get("notes") or ""),
                submodules=bool(row.get("submodules", False)),
            )
        )
    return repos


def select_repos(
    repos: Sequence[LargeScipRepo],
    *,
    names: Sequence[str] = (),
    languages: Sequence[str] = (),
    max_repos: int | None = None,
) -> list[LargeScipRepo]:
    """Filter manifest rows by repo name and/or language."""

    name_set = {name.strip() for name in names if name.strip()}
    language_set = {language.strip().lower() for language in languages if language}
    selected = [
        repo
        for repo in repos
        if (not name_set or repo.name in name_set)
        and (not language_set or repo.language in language_set)
    ]
    if max_repos is not None:
        selected = selected[: max(0, max_repos)]
    return selected


def dry_run_plan(
    repos: Sequence[LargeScipRepo],
    *,
    threshold: float = DEFAULT_ACCELERATION_THRESHOLD,
) -> dict[str, Any]:
    """Return a serializable execution plan without touching the network."""

    return {
        "mode": "dry-run",
        "acceleration_threshold": threshold,
        "repo_count": len(repos),
        "repos": [repo.to_plan_dict() for repo in repos],
    }


def acceleration_decision(
    *,
    generate_seconds: float | None,
    protoc_decode_seconds: float | None,
    serial_process_seconds: float | None,
    threshold: float = DEFAULT_ACCELERATION_THRESHOLD,
) -> dict[str, Any]:
    """Return the C++ acceleration gate decision for one serial profile."""

    if serial_process_seconds is None:
        return {
            "threshold": threshold,
            "crossed": False,
            "local_decode_build_fraction": None,
            "recommendation": "no-decision",
            "reason": "serial graph processing did not complete",
        }

    cold_start = sum(
        value or 0.0
        for value in (generate_seconds, protoc_decode_seconds, serial_process_seconds)
    )
    if cold_start <= 0:
        return {
            "threshold": threshold,
            "crossed": False,
            "local_decode_build_fraction": None,
            "recommendation": "no-decision",
            "reason": "cold-start timing is unavailable",
        }

    fraction = serial_process_seconds / cold_start
    crossed = fraction >= threshold
    return {
        "threshold": threshold,
        "crossed": crossed,
        "local_decode_build_fraction": fraction,
        "cold_start_seconds": cold_start,
        "local_decode_build_seconds": serial_process_seconds,
        "recommendation": (
            "profile-core-decoder" if crossed else "keep-serial-until-larger-profile"
        ),
        "reason": (
            "local decode/build crosses acceleration gate"
            if crossed
            else "external indexing dominates cold-start time"
        ),
    }


def profile_repositories(
    repos: Sequence[LargeScipRepo],
    *,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    backend_mode: str = "both",
    threshold: float = DEFAULT_ACCELERATION_THRESHOLD,
    timeout: int = DEFAULT_TIMEOUT_S,
    skip_clone: bool = False,
) -> dict[str, Any]:
    """Clone/profile selected repositories and write artifacts under output_dir."""

    output_dir.mkdir(parents=True, exist_ok=True)
    cache_root.mkdir(parents=True, exist_ok=True)

    records = []
    for repo in repos:
        records.append(
            profile_repository(
                repo,
                cache_root=cache_root,
                output_dir=output_dir,
                backend_mode=backend_mode,
                threshold=threshold,
                timeout=timeout,
                skip_clone=skip_clone,
            )
        )

    report = {
        "mode": "profile",
        "acceleration_threshold": threshold,
        "repo_count": len(records),
        "records": records,
    }
    (output_dir / "large_scip_profile.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "large_scip_profile.md").write_text(
        render_markdown_report(report),
        encoding="utf-8",
    )
    return report


def profile_repository(
    repo: LargeScipRepo,
    *,
    cache_root: Path,
    output_dir: Path,
    backend_mode: str,
    threshold: float,
    timeout: int,
    skip_clone: bool = False,
) -> dict[str, Any]:
    repo_root = cache_root / repo.name
    if not skip_clone:
        ensure_checkout(repo, repo_root, timeout=timeout)
    elif not repo_root.exists():
        raise FileNotFoundError(
            f"{repo_root} does not exist; omit --skip-clone to clone it"
        )

    commit = git_output(["rev-parse", "HEAD"], cwd=repo_root, timeout=60)
    source_files = count_source_files(repo_root, repo.language, repo.target_dir)
    repo_output = output_dir / repo.name
    repo_output.mkdir(parents=True, exist_ok=True)

    serial = run_serial_profile(repo, repo_root, repo_output, timeout=timeout)
    backends: dict[str, Any] = {"serial": serial.to_dict()}
    if backend_mode in {"both", "core"}:
        backends["core"] = run_core_profile(
            repo, repo_root, repo_output, serial=serial, timeout=timeout
        ).to_dict()

    serial_timings = serial.timings
    gate = acceleration_decision(
        generate_seconds=serial_timings.get("generate_index"),
        protoc_decode_seconds=serial_timings.get("decode_index"),
        serial_process_seconds=serial_timings.get("process_index_total"),
        threshold=threshold,
    )

    return {
        "repo": repo.to_plan_dict(),
        "commit": commit,
        "source_files": source_files,
        "source_file_gate": {
            "expected_min_source_files": repo.expected_min_source_files,
            "passed": source_files >= repo.expected_min_source_files,
        },
        "backends": backends,
        "acceleration_gate": gate,
    }


def ensure_checkout(repo: LargeScipRepo, repo_root: Path, *, timeout: int) -> None:
    """Clone or update one manifest repository to the requested ref."""

    if not repo_root.exists():
        repo_root.parent.mkdir(parents=True, exist_ok=True)
        run_command(
            ["git", "clone", "--filter=blob:none", repo.url, str(repo_root)],
            timeout=timeout,
        )

    run_command(
        ["git", "fetch", "--depth=1", "origin", repo.ref],
        cwd=repo_root,
        timeout=timeout,
    )
    run_command(["git", "checkout", "--force", "FETCH_HEAD"], cwd=repo_root)
    run_command(["git", "clean", "-fd"], cwd=repo_root)
    if repo.submodules:
        run_command(
            ["git", "submodule", "update", "--init", "--recursive", "--depth=1"],
            cwd=repo_root,
            timeout=timeout,
        )


def run_serial_profile(
    repo: LargeScipRepo,
    repo_root: Path,
    repo_output: Path,
    *,
    timeout: int,
) -> BackendProfile:
    from codeminer.profiler import Profiler

    profile = BackendProfile(backend="serial", status="failed")
    serial_output = repo_output / "serial"
    reset_output_dir(serial_output)
    profiler = Profiler(
        f"large_scip_{repo.name}_serial",
        emit_events=False,
        record_samples=True,
    )
    indexer = create_profile_indexer(
        repo,
        repo_root,
        serial_output,
        decoder_backend="serial",
        profiler=profiler,
    )
    try:
        profile.timings["generate_index"] = time_step(
            lambda: call_generate_index(indexer, timeout=timeout)
        )
        if not indexer.index_file.exists():
            raise RuntimeError("indexer did not produce index.scip")

        profile.timings["decode_index"] = time_step(indexer.decode_index)
        if not indexer.decoded_file.exists():
            raise RuntimeError("protoc did not produce index.decoded")

        set_target_dir(indexer, repo.target_dir)
        graph = process_profile_graph(indexer, profile)
        profile.graph = graph_stats(graph)
        profile.artifacts = artifact_stats(indexer)
        profile.timings.update(profiler_sections(profiler))
        profile.status = "ok"
    except Exception as exc:  # pragma: no cover - exercised by live profiles
        profile.error = str(exc)
        profile.artifacts = artifact_stats(indexer)
    return profile


def run_core_profile(
    repo: LargeScipRepo,
    repo_root: Path,
    repo_output: Path,
    *,
    serial: BackendProfile,
    timeout: int,
) -> BackendProfile:
    from codeminer.languages import core_decoder_languages
    from codeminer.profiler import Profiler

    profile = BackendProfile(backend="core", status="skipped")
    if repo.language not in core_decoder_languages(include_aliases=True):
        profile.error = f"{repo.language} has no accepted C++ core decoder"
        return profile
    if importlib.util.find_spec("codeminer_core") is None:
        profile.error = "codeminer_core pybind module is not importable"
        return profile
    if serial.status != "ok":
        profile.error = "serial profile did not complete; no decoded file to reuse"
        return profile

    serial_decoded = repo_output / "serial" / "index.decoded"
    serial_index = repo_output / "serial" / "index.scip"
    if not serial_decoded.exists():
        profile.error = "serial decoded artifact is missing"
        return profile

    core_output = repo_output / "core"
    reset_output_dir(core_output)
    shutil.copy2(serial_decoded, core_output / "index.decoded")
    if serial_index.exists():
        shutil.copy2(serial_index, core_output / "index.scip")

    profiler = Profiler(
        f"large_scip_{repo.name}_core",
        emit_events=False,
        record_samples=True,
    )
    indexer = create_profile_indexer(
        repo,
        repo_root,
        core_output,
        decoder_backend="core",
        profiler=profiler,
    )
    try:
        set_target_dir(indexer, repo.target_dir)
        graph = process_profile_graph(indexer, profile)
        profile.graph = graph_stats(graph)
        profile.artifacts = artifact_stats(indexer)
        profile.timings.update(profiler_sections(profiler))
        profile.status = "ok"
    except Exception as exc:  # pragma: no cover - exercised by live profiles
        profile.status = "failed"
        profile.error = str(exc)
        profile.artifacts = artifact_stats(indexer)
    return profile


def create_profile_indexer(
    repo: LargeScipRepo,
    repo_root: Path,
    output_dir: Path,
    *,
    decoder_backend: str,
    profiler: Any,
) -> Any:
    """Create the SCIP route whose decoder backend is being measured."""

    from codeminer.ls_router import SCIP_CANDIDATE_GRAPH_ROUTE, LSIndexer

    return LSIndexer(
        repo_root,
        output_dir=output_dir,
        exclude_patterns=list(repo.exclude_patterns),
        language=repo.language,
        decoder_backend=decoder_backend,
        graph_route=SCIP_CANDIDATE_GRAPH_ROUTE,
        profiler=profiler,
    )


def call_generate_index(indexer: Any, *, timeout: int) -> bool:
    """Call generate_index with timeout when the indexer supports it."""

    try:
        return bool(indexer.generate_index(timeout=timeout))
    except TypeError as exc:
        if "timeout" not in str(exc):
            raise
        return bool(indexer.generate_index())


def set_target_dir(indexer: Any, target_dir: str | None) -> None:
    if not target_dir:
        return
    delegate = indexer
    seen: set[int] = set()
    while id(delegate) not in seen:
        seen.add(id(delegate))
        nested = getattr(delegate, "_delegate", None)
        if nested is None or nested is delegate:
            break
        delegate = nested
    delegate._target_dir = target_dir


def process_profile_graph(indexer: Any, profile: BackendProfile) -> Any:
    """Measure graph construction separately from artifact persistence."""

    graph_box: dict[str, Any] = {}
    profile.timings["process_index_total"] = time_step(
        lambda: graph_box.setdefault("graph", indexer.process_index(output_file=None))
    )
    graph = graph_box.get("graph")
    if graph is None:
        raise RuntimeError(f"{profile.backend} graph processing returned None")
    profile.timings["save_graph"] = time_step(
        lambda: save_profile_graph(indexer, graph)
    )
    return graph


def save_profile_graph(indexer: Any, graph: Any) -> None:
    output_path = Path(indexer.graph_file)
    graph.save_graph(str(output_path))
    occurrence_index = getattr(graph, "lsp_occurrence_index", None)
    if occurrence_index is not None:
        occurrence_index.save(output_path.with_name("lsp_index.pkl"))


def time_step(fn: Any) -> float:
    start = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - start
    if result is False:
        raise RuntimeError("profile step returned False")
    return elapsed


def profiler_sections(profiler: Any) -> dict[str, float]:
    sections = profiler.report(reset=False)
    return {f"section.{label}": stats.total for label, stats in sections}


def graph_stats(graph: Any) -> dict[str, int]:
    edges = graph.graph.es
    references = 0
    for edge in edges:
        try:
            if edge.attributes().get("type") == "reference":
                references += 1
        except AttributeError:
            if edge["type"] == "reference":
                references += 1
    return {
        "vertices": graph.graph.vcount(),
        "edges": graph.graph.ecount(),
        "references": references,
    }


def artifact_stats(indexer: Any) -> dict[str, str | int | None]:
    def size(path: Path) -> int:
        return path.stat().st_size if path.exists() else 0

    return {
        "output_dir": str(indexer.output_dir),
        "index_path": str(indexer.index_file) if indexer.index_file.exists() else None,
        "decoded_path": (
            str(indexer.decoded_file) if indexer.decoded_file.exists() else None
        ),
        "graph_path": str(indexer.graph_file) if indexer.graph_file.exists() else None,
        "index_bytes": size(indexer.index_file),
        "decoded_bytes": size(indexer.decoded_file),
        "graph_bytes": size(indexer.graph_file),
    }


def count_source_files(
    repo_root: Path, language: str, target_dir: str | None = None
) -> int:
    root = repo_root / target_dir if target_dir else repo_root
    if not root.exists():
        return 0
    exts = LANGUAGE_EXTENSIONS.get(language, ())
    if not exts:
        return 0
    return sum(1 for path in root.rglob("*") if path.is_file() and path.suffix in exts)


def reset_output_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def git_output(args: Sequence[str], *, cwd: Path, timeout: int = 120) -> str:
    result = run_command(["git", *args], cwd=cwd, timeout=timeout)
    return result.stdout.strip()


def run_command(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd is not None else None,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def render_markdown_report(report: dict[str, Any]) -> str:
    rows = [
        "# Large SCIP Repository Profile",
        "",
        f"Acceleration threshold: {report['acceleration_threshold']:.0%}",
        "",
        "| Repository | Language | Source files | Serial process | Gate fraction | Decision |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for record in report.get("records", []):
        repo = record["repo"]
        serial = record["backends"].get("serial", {})
        gate = record["acceleration_gate"]
        process_s = serial.get("timings", {}).get("process_index_total")
        fraction = gate.get("local_decode_build_fraction")
        rows.append(
            "| {name} | {language} | {files} | {process} | {fraction} | {decision} |".format(
                name=repo["name"],
                language=repo["language"],
                files=record.get("source_files", 0),
                process=fmt_seconds(process_s),
                fraction=fmt_percent(fraction),
                decision=gate.get("recommendation", "unknown"),
            )
        )
    rows.append("")
    return "\n".join(rows)


def fmt_seconds(value: Any) -> str:
    return "" if value is None else f"{float(value):.3f}s"


def fmt_percent(value: Any) -> str:
    return "" if value is None else f"{float(value):.1%}"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--repo", action="append", default=[])
    parser.add_argument("--language", action="append", default=[])
    parser.add_argument("--max-repos", type=int)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    parser.add_argument(
        "--acceleration-threshold",
        type=float,
        default=DEFAULT_ACCELERATION_THRESHOLD,
    )
    parser.add_argument(
        "--backend",
        choices=("serial", "core", "both"),
        default="both",
        help="which decoder backend profiles to run after cloning",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print the selected manifest plan without cloning",
    )
    parser.add_argument(
        "--skip-clone",
        action="store_true",
        help="reuse existing checkouts under --cache-root",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repos = select_repos(
        load_manifest(args.manifest),
        names=args.repo,
        languages=args.language,
        max_repos=args.max_repos,
    )
    if args.dry_run:
        print(
            json.dumps(
                dry_run_plan(repos, threshold=args.acceleration_threshold),
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    report = profile_repositories(
        repos,
        cache_root=args.cache_root,
        output_dir=args.output_dir,
        backend_mode=args.backend,
        threshold=args.acceleration_threshold,
        timeout=args.timeout,
        skip_clone=args.skip_clone,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
