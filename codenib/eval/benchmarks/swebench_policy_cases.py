# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Prepare fixed SWE-bench Lite cases for policy compatibility studies.

The preparation stage is intentionally separate from the model runner. It
freezes a label-independent repository-stratified sample, creates one detached
checkout per case, derives localization labels from the golden patch, builds
the CodeNib views required by the policy, and validates every manifest before
any model request is sent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from codenib.compiler.artifact_quality import graph_file_paths
from codenib.compiler.index_builders import (
    IndexBuilderRegistry,
    register_default_builders,
)
from codenib.compiler.index_compiler import IndexCompiler, IndexCompilerConfig
from codenib.compiler.manifest import MANIFEST_FILENAME, RepoManifest
from codenib.dataset.gt_locate import GTLocator, language_for_file
from codenib.eval.benchmarks.policy_compat import (
    PolicyCaseSet,
    inspect_policy_run_preflight,
    write_json_atomic,
)
from codenib.eval.retrieval_eval import collect_targets
from codenib.git_snapshot import GitSourceSurface
from codenib.graph.code_graph import CodeGraph
from codenib.languages import get_language_spec
from codenib.repository_filters import repository_path_is_visible
from codenib.scip_interface.scip_pb2 import Index as SCIPIndex

SWE_BENCH_LITE_DATASET = "princeton-nlp/SWE-bench_Lite"
SWE_BENCH_LITE_REVISION = "6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2"
DEFAULT_SELECTION_SEED = "codenib-locagent-swebench-lite-50-v1"
SELECTION_ALGORITHM = "balanced-repository-stratified-v1"
PREPARATION_SCHEMA_VERSION = 1
MIN_GRAPH_SOURCE_COVERAGE = 0.95

_DATASET_FIELDS = frozenset(
    {"repo", "instance_id", "base_commit", "patch", "problem_statement"}
)


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_rank(seed: str, *parts: str) -> str:
    value = "\0".join((seed, *parts)).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _scip_python_provenance() -> dict[str, Any]:
    path = shutil.which("scip-python")
    if path is None:
        raise RuntimeError(
            "SWE-bench policy preparation requires scip-python on PATH; "
            "run `make scip-python-tool` and prepend CODENIB_SCIP_TOOLS_DIR"
        )
    result = subprocess.run(
        [path, "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    version = (result.stdout or result.stderr).strip().splitlines()
    if result.returncode != 0 or not version:
        raise RuntimeError(f"could not resolve scip-python version from {path}")
    return {
        "path": str(Path(path).resolve()),
        "version": version[0],
        "index_timeout_seconds": os.environ.get(
            "CODENIB_SCIP_PYTHON_INDEX_TIMEOUT_SECONDS",
            "default",
        ),
    }


def load_dataset_records(path: str | Path) -> tuple[list[dict[str, Any]], str]:
    """Load a JSON array or JSONL dataset and return its byte-level digest."""

    source = Path(path).expanduser().resolve()
    raw = source.read_text(encoding="utf-8")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = [json.loads(line) for line in raw.splitlines() if line.strip()]

    if isinstance(decoded, Mapping):
        records = decoded.get("data") or decoded.get("cases")
    else:
        records = decoded
    if not isinstance(records, list) or not records:
        raise ValueError(f"{source} does not contain dataset records")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise ValueError(f"dataset record {position} is not an object")
        missing = _DATASET_FIELDS - set(record)
        if missing:
            raise ValueError(f"dataset record {position} is missing {sorted(missing)}")
        instance_id = str(record["instance_id"]).strip()
        if not instance_id or instance_id in seen:
            raise ValueError(f"invalid or duplicate instance_id: {instance_id!r}")
        seen.add(instance_id)
        normalized.append(dict(record))
    return normalized, _file_sha256(source)


def select_balanced_repository_cases(
    records: Iterable[Mapping[str, Any]],
    *,
    count: int,
    seed: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Select a deterministic, broad sample without consulting gold labels.

    Each repository first receives an equal quota capped by its population.
    Remaining slots are distributed one per round to strata with the greatest
    residual capacity. Cases within a repository are ordered by a seeded hash,
    and the final sample is emitted round-robin across repositories.
    """

    if count < 1:
        raise ValueError("selection count must be positive")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in records:
        record = dict(raw)
        repo = str(record.get("repo") or "").strip()
        instance_id = str(record.get("instance_id") or "").strip()
        if not repo or not instance_id:
            raise ValueError("selection records require repo and instance_id")
        grouped[repo].append(record)
    if count > sum(len(values) for values in grouped.values()):
        raise ValueError("selection count exceeds the dataset population")
    if count < len(grouped):
        raise ValueError("selection count is smaller than the repository stratum count")

    repos = sorted(grouped)
    for _repo, values in grouped.items():
        values.sort(
            key=lambda row: (
                _stable_rank(seed, str(row["instance_id"])),
                str(row["instance_id"]),
            )
        )

    equal_quota = count // len(repos)
    quotas = {repo: min(equal_quota, len(grouped[repo])) for repo in repos}
    remaining = count - sum(quotas.values())
    while remaining:
        eligible = [repo for repo in repos if quotas[repo] < len(grouped[repo])]
        if not eligible:
            raise RuntimeError("selection quota allocation exhausted all strata")
        eligible.sort(
            key=lambda repo: (
                -(len(grouped[repo]) - quotas[repo]),
                _stable_rank(seed, repo),
                repo,
            )
        )
        for repo in eligible:
            if not remaining:
                break
            quotas[repo] += 1
            remaining -= 1

    selected: list[dict[str, Any]] = []
    for rank in range(max(quotas.values(), default=0)):
        for repo in repos:
            if rank < quotas[repo]:
                selected.append(grouped[repo][rank])
    if len(selected) != count:
        raise RuntimeError(f"selected {len(selected)} cases, expected {count}")
    return selected, quotas


def _run_git(
    repo: Path,
    *args: str,
    input_text: str | None = None,
    check: bool = True,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _source_path(source_root: Path, repository: str) -> Path:
    return source_root / repository.replace("/", "__")


def ensure_source_repository(
    source_root: Path,
    *,
    repository: str,
    commits: Sequence[str],
) -> Path:
    """Create one object-sharing source clone and ensure all commits exist."""

    source = _source_path(source_root, repository)
    source_root.mkdir(parents=True, exist_ok=True)
    if not (source / ".git").is_dir():
        if source.exists():
            raise RuntimeError(f"source path exists but is not a Git clone: {source}")
        print(f"clone {repository} -> {source}", flush=True)
        subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--filter=blob:none",
                "--no-checkout",
                f"https://github.com/{repository}.git",
                str(source),
            ],
            check=True,
            timeout=1800,
        )

    for commit in sorted(set(commits)):
        probe = _run_git(
            source,
            "cat-file",
            "-e",
            f"{commit}^{{commit}}",
            check=False,
        )
        if probe.returncode == 0:
            continue
        print(f"fetch {repository}@{commit[:12]}", flush=True)
        _run_git(source, "fetch", "origin", commit, timeout=1800)
    return source


def ensure_snapshot_worktree(source: Path, snapshot: Path, commit: str) -> Path:
    """Create or verify an experiment-owned detached worktree."""

    if snapshot.is_dir():
        head = _run_git(snapshot, "rev-parse", "HEAD", check=False)
        status = _run_git(snapshot, "status", "--porcelain", check=False)
        if (
            head.returncode == 0
            and head.stdout.strip() == commit
            and status.returncode == 0
            and not status.stdout.strip()
        ):
            return snapshot
        _run_git(source, "worktree", "remove", "--force", str(snapshot), check=False)
        if snapshot.exists():
            shutil.rmtree(snapshot)
        _run_git(source, "worktree", "prune", check=False)

    snapshot.parent.mkdir(parents=True, exist_ok=True)
    _run_git(source, "worktree", "add", "--detach", str(snapshot), commit)
    return snapshot


def _extract_symbols(locator: GTLocator, checkout: Path, files: Sequence[str]):
    symbols = {}
    for relative_path in files:
        source = checkout / relative_path
        if source.is_file():
            symbols.update(
                locator.extract_symbols_from_file(str(source), relative_path)
            )
    return symbols


def extract_golden_patch_labels(
    checkout: str | Path,
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive file and existing-function labels, restoring the checkout exactly."""

    root = Path(checkout).expanduser().resolve()
    expected_commit = str(record["base_commit"])
    actual_commit = _run_git(root, "rev-parse", "HEAD").stdout.strip()
    if actual_commit != expected_commit:
        raise RuntimeError(
            f"snapshot commit mismatch: {actual_commit} != {expected_commit}"
        )
    if _run_git(root, "status", "--porcelain").stdout.strip():
        raise RuntimeError(f"snapshot is dirty before label extraction: {root}")

    patch = str(record["patch"])
    patch_input = patch if patch.endswith("\n") else patch + "\n"
    locator = GTLocator(work_dir=str(root.parent))
    target_files = locator.get_target_files(patch)
    supported_files = [path for path in target_files if language_for_file(path)]
    symbols_before = _extract_symbols(locator, root, supported_files)
    changed_ranges = locator.get_changed_line_ranges(patch)

    applied = False
    try:
        apply_result = _run_git(
            root,
            "apply",
            "-",
            input_text=patch_input,
            check=False,
            timeout=60,
        )
        if apply_result.returncode != 0:
            raise RuntimeError(
                "gold patch did not apply: " + apply_result.stderr.strip()
            )
        applied = True
        symbols_after = _extract_symbols(locator, root, supported_files)
        modified, added, deleted = locator.compare_symbols(
            symbols_before,
            symbols_after,
            changed_ranges,
        )
        analysis = {
            "target_files": target_files,
            "symbols_modified": [name for name, _chunk in modified],
            "symbols_added": [name for name, _chunk in added],
            "symbols_deleted": [name for name, _chunk in deleted],
        }
        gold_files, gold_functions = collect_targets(
            analysis,
            simplified_symbols=True,
        )
        result = {
            "gold_files": list(dict.fromkeys(gold_files)),
            "gold_functions": list(dict.fromkeys(gold_functions)),
            "method": "gold-patch-tree-sitter-existing-functions-v1",
            "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
            "supported_target_files": supported_files,
            "symbols_added": analysis["symbols_added"],
        }
    finally:
        if applied:
            reverse = _run_git(
                root,
                "apply",
                "--reverse",
                "-",
                input_text=patch_input,
                check=False,
                timeout=60,
            )
            if reverse.returncode != 0:
                raise RuntimeError(
                    "failed to restore snapshot after label extraction: "
                    + reverse.stderr.strip()
                )

    final_commit = _run_git(root, "rev-parse", "HEAD").stdout.strip()
    final_status = _run_git(root, "status", "--porcelain").stdout.strip()
    if final_commit != expected_commit or final_status:
        raise RuntimeError("gold-label extraction did not restore the source snapshot")
    return result


def _snapshot_errors(checkout: Path, commit: str) -> list[str]:
    errors: list[str] = []
    head = _run_git(checkout, "rev-parse", "HEAD", check=False)
    if head.returncode != 0:
        errors.append("snapshot HEAD cannot be resolved")
    elif head.stdout.strip() != commit:
        errors.append(f"snapshot commit mismatch: {head.stdout.strip()} != {commit}")
    status = _run_git(checkout, "status", "--porcelain", check=False)
    if status.returncode != 0:
        errors.append("snapshot status cannot be resolved")
    elif status.stdout.strip():
        errors.append("snapshot is dirty after index construction")
    return errors


def _manifest_errors(manifest_path: Path, checkout: Path, commit: str) -> list[str]:
    errors = _snapshot_errors(checkout, commit)
    try:
        manifest = RepoManifest.load(manifest_path)
    except Exception as exc:  # noqa: BLE001 - preparation audit records exact error
        return [f"manifest cannot be loaded: {type(exc).__name__}: {exc}"]
    if Path(manifest.repo_path).resolve() != checkout.resolve():
        errors.append("manifest repository path does not match the snapshot")
    if manifest.commit != commit:
        errors.append(f"manifest commit mismatch: {manifest.commit} != {commit}")
    for view in ("bm25", "symbol_graph"):
        if not manifest.index_is_current(view):
            errors.append(f"manifest view is not current: {view}")
    for capability in ("sparse_search", "symbol_navigation"):
        if not manifest.capabilities.get(capability, False):
            errors.append(f"manifest lacks capability: {capability}")
    return errors


def _graph_source_coverage_report(
    expected: Iterable[str],
    represented: Iterable[str],
    *,
    outside_commit: Iterable[str] = (),
    minimum: float = MIN_GRAPH_SOURCE_COVERAGE,
) -> dict[str, Any]:
    expected_paths = frozenset(expected)
    represented_paths = frozenset(represented)
    covered = expected_paths & represented_paths
    missing = sorted(expected_paths - represented_paths)
    extra = sorted(represented_paths - expected_paths)
    outside = sorted(set(outside_commit))
    coverage = len(covered) / len(expected_paths) if expected_paths else 1.0
    return {
        "minimum": minimum,
        "expected_source_files": len(expected_paths),
        "represented_source_files": len(covered),
        "graph_file_nodes": len(represented_paths),
        "coverage": coverage,
        "missing_source_files": missing,
        "extra_graph_files": extra,
        "outside_commit_files": outside,
        "passed": bool(expected_paths and coverage >= minimum and not outside),
    }


def _scip_producer(index_path: Path) -> dict[str, Any]:
    index = SCIPIndex()
    index.ParseFromString(index_path.read_bytes())
    tool = index.metadata.tool_info
    if not tool.name or not tool.version:
        raise RuntimeError(f"SCIP index does not declare its producer: {index_path}")
    return {
        "name": tool.name,
        "version": tool.version,
        "arguments": list(tool.arguments),
    }


def _graph_producer(
    index_path: Path,
    graph_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    if index_path.is_file():
        return _scip_producer(index_path)
    coverage = graph_metadata.get("source_coverage_report")
    if (
        isinstance(coverage, Mapping)
        and coverage.get("compiler_graph_available") is False
    ):
        return {
            "name": "codenib-syntax-fallback",
            "version": "1",
            "arguments": [],
        }
    raise RuntimeError(f"symbol graph has no compiler provenance: {index_path}")


def _graph_coverage_mode(
    index_path: Path,
    graph_metadata: Mapping[str, Any],
) -> str:
    """Describe which source supplied the published graph surface."""

    coverage = graph_metadata.get("source_coverage_report")
    if not index_path.is_file():
        if (
            isinstance(coverage, Mapping)
            and coverage.get("compiler_graph_available") is False
        ):
            return "syntax"
        raise RuntimeError(f"symbol graph has no compiler provenance: {index_path}")
    if not isinstance(coverage, Mapping):
        return "compiler"
    if coverage.get("supplemented_files"):
        return "compiler-prefix+syntax"
    if coverage.get("compiler_index_complete") is False:
        return "compiler-prefix"
    return "compiler"


def assess_policy_graph(
    manifest_path: Path,
    checkout: Path,
    commit: str,
) -> dict[str, Any]:
    """Audit graph file nodes against the label-independent Git surface."""

    manifest = RepoManifest.load(manifest_path)
    graph = CodeGraph.load_graph(
        Path(manifest.indexes["symbol_graph"].path) / "graph.pkl"
    )
    surface = GitSourceSurface.load(checkout, commit)
    suffixes = frozenset(get_language_spec("python").graph_extensions)
    expected = {
        path
        for path in surface.tracked_files
        if Path(path).suffix in suffixes and repository_path_is_visible(path)
    }
    represented = set(graph_file_paths(graph))
    outside_commit = {path for path in represented if not surface.contains(path)}
    report = _graph_source_coverage_report(
        expected,
        represented,
        outside_commit=outside_commit,
    )
    graph_entry = manifest.indexes["symbol_graph"]
    index_path = Path(graph_entry.path) / "index.scip"
    report["producer"] = _graph_producer(
        index_path,
        graph_entry.metadata,
    )
    report["coverage_mode"] = _graph_coverage_mode(
        index_path,
        graph_entry.metadata,
    )
    if not report["passed"]:
        raise RuntimeError(
            "symbol graph source coverage failed: "
            f"coverage={report['coverage']:.3f}, "
            f"minimum={report['minimum']:.3f}, "
            f"outside={len(report['outside_commit_files'])}"
        )
    return report


def build_policy_manifest(
    checkout: str | Path,
    artifact_dir: str | Path,
    *,
    commit: str,
) -> tuple[Path, dict[str, Any]]:
    """Build or resume the BM25 and symbol-graph views for one snapshot."""

    root = Path(checkout).expanduser().resolve()
    output = Path(artifact_dir).expanduser().resolve() / "indexes"
    manifest_path = output / MANIFEST_FILENAME
    started = time.perf_counter()

    if manifest_path.is_file():
        errors = _manifest_errors(manifest_path, root, commit)
        if not errors:
            try:
                graph_quality = assess_policy_graph(manifest_path, root, commit)
            except (OSError, RuntimeError, ValueError):
                pass
            else:
                manifest = RepoManifest.load(manifest_path)
                return manifest_path, _policy_build_report(
                    manifest,
                    graph_quality,
                    duration_seconds=time.perf_counter() - started,
                    reused_existing=True,
                )

    registry = IndexBuilderRegistry()
    register_default_builders(
        registry,
        languages=["python"],
        allow_partial_graph_languages=False,
        allow_partial_graph_index=True,
        graph_source_coverage_fallback=True,
    )
    compiler = IndexCompiler(
        registry,
        IndexCompilerConfig(
            index_types=["bm25", "symbol_graph"],
            languages=["python"],
        ),
    )
    if manifest_path.is_file():
        manifest = compiler.update_repo(
            str(root),
            index_types=["bm25", "symbol_graph"],
            cache_dir=str(output),
        )
    else:
        manifest = compiler.compile_repo(
            str(root),
            index_types=["bm25", "symbol_graph"],
            cache_dir=str(output),
        )
    errors = _manifest_errors(manifest_path, root, commit)
    if errors:
        raise RuntimeError("; ".join(errors))
    graph_quality = assess_policy_graph(manifest_path, root, commit)
    return manifest_path, _policy_build_report(
        manifest,
        graph_quality,
        duration_seconds=time.perf_counter() - started,
        reused_existing=False,
    )


def _policy_build_report(
    manifest: RepoManifest,
    graph_quality: Mapping[str, Any],
    *,
    duration_seconds: float,
    reused_existing: bool,
) -> dict[str, Any]:
    graph_metadata = manifest.indexes["symbol_graph"].metadata
    return {
        "duration_seconds": duration_seconds,
        "reused_existing": reused_existing,
        "file_count": manifest.file_count,
        "capabilities": dict(manifest.capabilities),
        "graph_quality": dict(graph_quality),
        "source_coverage_report": graph_metadata.get("source_coverage_report"),
        "views": {
            name: {
                "status": manifest.indexes[name].status,
                "build_duration_seconds": manifest.indexes[name].metadata.get(
                    "build_duration_seconds"
                ),
                "item_count": manifest.indexes[name].metadata.get(
                    "chunk_count",
                    manifest.indexes[name].metadata.get("node_count"),
                ),
            }
            for name in ("bm25", "symbol_graph")
        },
    }


def prepare_policy_case(
    record: Mapping[str, Any],
    *,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Prepare one already-created source snapshot and return its case record."""

    root = Path(output_dir).expanduser().resolve()
    instance_id = str(record["instance_id"])
    case_dir = root / "prepared" / instance_id
    checkout = root / "snapshots" / instance_id / "repo"
    case_path = case_dir / "case.json"
    labels_path = case_dir / "ground_truth.json"
    report_path = case_dir / "build_report.json"
    failure_path = case_dir / "build_failure.json"
    case_dir.mkdir(parents=True, exist_ok=True)

    patch_sha256 = hashlib.sha256(str(record["patch"]).encode("utf-8")).hexdigest()
    labels: dict[str, Any]
    if labels_path.is_file():
        labels = json.loads(labels_path.read_text(encoding="utf-8"))
        if labels.get("patch_sha256") != patch_sha256:
            raise RuntimeError("cached ground truth belongs to a different patch")
    else:
        labels = extract_golden_patch_labels(checkout, record)
        write_json_atomic(labels_path, labels)

    manifest_path, build_report = build_policy_manifest(
        checkout,
        case_dir,
        commit=str(record["base_commit"]),
    )
    case = {
        "instance_id": instance_id,
        "repo": str(record["repo"]),
        "base_commit": str(record["base_commit"]),
        "problem_statement": str(record["problem_statement"]),
        "repo_path": str(checkout),
        "manifest_path": str(manifest_path),
        "ground_truth": {
            "gold_files": labels["gold_files"],
            "gold_functions": labels["gold_functions"],
        },
        "ground_truth_method": labels["method"],
        "gold_patch_sha256": labels["patch_sha256"],
    }
    write_json_atomic(case_path, case)
    write_json_atomic(
        report_path,
        {
            "instance_id": instance_id,
            "status": "ready",
            "labels": {
                "file_count": len(labels["gold_files"]),
                "function_count": len(labels["gold_functions"]),
                "supported_target_file_count": len(labels["supported_target_files"]),
                "added_symbol_count": len(labels["symbols_added"]),
            },
            "index": build_report,
        },
    )
    failure_path.unlink(missing_ok=True)
    return case


def _selection_record(
    selected: Sequence[Mapping[str, Any]],
    *,
    source_path: Path,
    source_sha256: str,
    dataset_revision: str,
    seed: str,
    quotas: Mapping[str, int],
) -> dict[str, Any]:
    semantic = [
        {
            "instance_id": str(row["instance_id"]),
            "repo": str(row["repo"]),
            "base_commit": str(row["base_commit"]),
            "patch_sha256": hashlib.sha256(
                str(row["patch"]).encode("utf-8")
            ).hexdigest(),
        }
        for row in selected
    ]
    return {
        "schema_version": PREPARATION_SCHEMA_VERSION,
        "dataset": SWE_BENCH_LITE_DATASET,
        "split": "test",
        "dataset_revision": dataset_revision,
        "dataset_source": str(source_path),
        "dataset_source_sha256": source_sha256,
        "selection_algorithm": SELECTION_ALGORITHM,
        "selection_seed": seed,
        "selection_count": len(selected),
        "selection_sha256": _canonical_sha256(semantic),
        "per_repository_count": dict(sorted(quotas.items())),
        "instances": semantic,
    }


def _case_failure(instance_id: str, exc: BaseException) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "status": "failed",
        "error": f"{type(exc).__name__}: {exc}",
    }


def prepare_cases(args: argparse.Namespace) -> int:
    dataset_path = args.dataset_json.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    environment = {
        "schema_version": PREPARATION_SCHEMA_VERSION,
        "python": sys.version.split()[0],
        "parallel_workers": args.jobs,
        "scip_python": _scip_python_provenance(),
    }
    write_json_atomic(output / "preparation_environment.json", environment)
    print(
        "toolchain: "
        f"scip-python={environment['scip_python']['version']} "
        f"path={environment['scip_python']['path']} "
        f"timeout={environment['scip_python']['index_timeout_seconds']}",
        flush=True,
    )
    records, source_sha256 = load_dataset_records(dataset_path)
    selected, quotas = select_balanced_repository_cases(
        records,
        count=args.count,
        seed=args.seed,
    )
    selection = _selection_record(
        selected,
        source_path=dataset_path,
        source_sha256=source_sha256,
        dataset_revision=args.dataset_revision,
        seed=args.seed,
        quotas=quotas,
    )
    selection_path = output / "selection.json"
    if selection_path.is_file():
        existing = json.loads(selection_path.read_text(encoding="utf-8"))
        if existing != selection:
            raise RuntimeError(
                "existing selection does not match the requested dataset/configuration"
            )
    else:
        write_json_atomic(selection_path, selection)

    by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in selected:
        by_repo[str(row["repo"])].append(row)
    source_root = output / "sources"
    for repo in sorted(by_repo):
        rows = by_repo[repo]
        source = ensure_source_repository(
            source_root,
            repository=repo,
            commits=[str(row["base_commit"]) for row in rows],
        )
        for row in rows:
            instance_id = str(row["instance_id"])
            snapshot = output / "snapshots" / instance_id / "repo"
            ensure_snapshot_worktree(
                source,
                snapshot,
                str(row["base_commit"]),
            )
            print(f"snapshot ready {instance_id}", flush=True)

    prepared: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(prepare_policy_case, row, output_dir=output): str(
                row["instance_id"]
            )
            for row in selected
        }
        for future in as_completed(futures):
            instance_id = futures[future]
            try:
                prepared[instance_id] = future.result()
                print(f"prepared {instance_id}", flush=True)
            except Exception as exc:  # noqa: BLE001 - preserve every failed case
                failure = _case_failure(instance_id, exc)
                failures.append(failure)
                failure_path = output / "prepared" / instance_id / "build_failure.json"
                failure_path.parent.mkdir(parents=True, exist_ok=True)
                write_json_atomic(failure_path, failure)
                print(f"FAILED {instance_id}: {failure['error']}", flush=True)

    ordered_cases = [
        prepared[str(row["instance_id"])]
        for row in selected
        if str(row["instance_id"]) in prepared
    ]
    report = {
        "schema_version": PREPARATION_SCHEMA_VERSION,
        "selection_sha256": selection["selection_sha256"],
        "requested_count": len(selected),
        "prepared_count": len(ordered_cases),
        "failed_count": len(failures),
        "failures": failures,
    }
    write_json_atomic(output / "prepare_report.json", report)
    if failures or len(ordered_cases) != len(selected):
        return 1

    cases_payload = {
        "schema_version": PREPARATION_SCHEMA_VERSION,
        "dataset": SWE_BENCH_LITE_DATASET,
        "split": "test",
        "dataset_revision": args.dataset_revision,
        "dataset_source_sha256": source_sha256,
        "selection_algorithm": SELECTION_ALGORITHM,
        "selection_seed": args.seed,
        "selection_sha256": selection["selection_sha256"],
        "per_repository_count": selection["per_repository_count"],
        "cases": ordered_cases,
    }
    cases_path = output / "cases.json"
    write_json_atomic(cases_path, cases_payload)
    case_set = PolicyCaseSet.load(cases_path)
    preflight = inspect_policy_run_preflight(
        case_set.cases,
        agent="locagent",
        providers=("codenib",),
        required_capabilities={"codenib": ("sparse_search", "symbol_navigation")},
    )
    write_json_atomic(output / "preflight.json", preflight.to_record())
    preflight.require_eligible()
    print(
        f"ready: {len(case_set.cases)} cases, selection={selection['selection_sha256']}",
        flush=True,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-json", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--seed", default=DEFAULT_SELECTION_SEED)
    parser.add_argument("--dataset-revision", default=SWE_BENCH_LITE_REVISION)
    parser.add_argument(
        "--jobs",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="parallel label/index workers",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.jobs < 1:
        raise ValueError("--jobs must be positive")
    return prepare_cases(args)


if __name__ == "__main__":
    raise SystemExit(main())
