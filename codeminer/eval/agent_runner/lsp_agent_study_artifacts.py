# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Prepare snapshot-addressed graphs for the Base LSP agent study."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from codeminer.compiler.snapshot_store import (
    ArtifactProfile,
    SnapshotArtifactStore,
    SourceSnapshot,
)
from codeminer.eval.agent_runner.prebuilt import load_code_graph_artifact
from codeminer.eval.experiments.lsp_agent_study_policy import STATIC_NATIVE_BACKENDS
from codeminer.graph.code_graph import _SCHEMA_VERSION, CodeGraph
from codeminer.ls_router import ACTIVE_GRAPH_ROUTE, LSIndexer
from codeminer.scip_interface.lsp_occurrence_index import (
    LSP_OCCURRENCE_INDEX_SCHEMA_VERSION,
    SCIPOccurrenceIndex,
)

ARTIFACT_PROFILE_NAME = "lsp-agent-base-scip-v2"
CPP_ARTIFACT_PROFILE_NAME = "lsp-agent-base-clangd-graph-v1"


def static_native_backend_for_language(language: str) -> str:
    """Return the pinned static backend for one normalized language."""

    normalized = "typescript" if language in {"javascript", "js", "ts"} else language
    try:
        return STATIC_NATIVE_BACKENDS[normalized]
    except KeyError as exc:
        raise ValueError(f"no native LSP backend for language {language!r}") from exc


def requires_lsp_occurrence_index(language: str) -> bool:
    """Return whether the language profile persists exact SCIP occurrences."""

    return static_native_backend_for_language(language) == "scip_occurrence_index_v1"


def lsp_agent_artifact_profile(language: str) -> ArtifactProfile:
    """Return the compatibility identity for one study graph."""

    if language in {"c", "cpp", "c++"}:
        return ArtifactProfile.create(
            ("cpp",),
            name=CPP_ARTIFACT_PROFILE_NAME,
            schema_versions={"code_graph": str(_SCHEMA_VERSION)},
            options={
                "exclude_patterns": [],
                "graph_route": ACTIVE_GRAPH_ROUTE,
                "static_native_backend": static_native_backend_for_language("cpp"),
            },
        )
    languages = (
        ("javascript", "typescript")
        if language in {"javascript", "typescript", "js", "ts"}
        else (language,)
    )
    return ArtifactProfile.create(
        languages,
        name=ARTIFACT_PROFILE_NAME,
        schema_versions={
            "code_graph": str(_SCHEMA_VERSION),
            "lsp_occurrence_index": str(LSP_OCCURRENCE_INDEX_SCHEMA_VERSION),
        },
        options={"exclude_patterns": [], "graph_route": ACTIVE_GRAPH_ROUTE},
    )


def find_source_repository(
    source_root: str | Path,
    *,
    repo: str,
    commit: str,
) -> Path:
    """Find a local clone containing the requested benchmark commit."""

    root = Path(source_root).expanduser().resolve()
    slug = repo.replace("/", "__")
    candidates = [root / slug / "repo", root / slug]
    candidates.extend(sorted(root.glob(f"{slug}-*/repo")))
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        resolved = candidate.resolve()
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={resolved}",
                "-C",
                str(candidate),
                "cat-file",
                "-e",
                f"{commit}^{{commit}}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return candidate
    raise FileNotFoundError(f"no local clone for {repo}@{commit} under {root}")


def prepare_subject_artifact(
    subject: Mapping[str, Any],
    *,
    source_root: str | Path,
    output_root: str | Path,
    reuse_root: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Build or verify one snapshot graph and return its build record."""

    instance_id = str(subject["instance_id"])
    repo = str(subject["repo"])
    commit = str(subject["base_commit"])
    language = str(subject["language"])
    source = find_source_repository(source_root, repo=repo, commit=commit)
    store = SnapshotArtifactStore(output_root)
    profile = lsp_agent_artifact_profile(language)
    binding = store.bind(
        instance_id,
        SourceSnapshot(repo, commit),
        profile,
    )
    worktree = store.ensure_worktree(
        binding,
        source_repo=source,
        commit=commit,
    )
    graph_path = binding.profile_dir / "graph.pkl"
    lsp_index_path = binding.profile_dir / "lsp_index.pkl"
    decoded_path = binding.profile_dir / "index.decoded"
    if reuse_root is not None and not force and not graph_path.is_file():
        _reuse_scip_artifacts(
            Path(reuse_root).expanduser().resolve() / instance_id,
            binding.profile_dir,
            worktree=worktree,
            expected_commit=commit,
        )
    started = time.perf_counter()
    cache_hit = False
    graph = None
    occurrence_index = None
    occurrence_required = requires_lsp_occurrence_index(language)
    if (
        graph_path.is_file()
        and (lsp_index_path.is_file() or not occurrence_required)
        and not force
    ):
        try:
            graph = CodeGraph.load_graph(graph_path)
            graph.project_root = str(worktree)
            if lsp_index_path.is_file():
                occurrence_index = SCIPOccurrenceIndex.load(lsp_index_path)
                graph.lsp_occurrence_index = occurrence_index
            cache_hit = True
        except Exception:
            graph = None
            occurrence_index = None
    elif (
        occurrence_required
        and graph_path.is_file()
        and decoded_path.is_file()
        and not force
    ):
        try:
            graph = CodeGraph.load_graph(graph_path)
            graph.project_root = str(worktree)
            graph.save_graph(graph_path)
            occurrence_index = SCIPOccurrenceIndex.from_decoded_file(decoded_path)
            occurrence_index.save(lsp_index_path)
            graph.lsp_occurrence_index = occurrence_index
        except Exception:
            graph = None
            occurrence_index = None
    if graph is None:
        indexer = LSIndexer(
            worktree,
            output_dir=binding.profile_dir,
            exclude_patterns=[],
            language=language,
            graph_route=ACTIVE_GRAPH_ROUTE,
        )
        graph = indexer.run_pipeline(skip_level=None, report_profile=False)
        quality = indexer.index_quality_report
        occurrence_index = getattr(graph, "lsp_occurrence_index", None)
    else:
        quality = None
    if (
        graph is None
        or len(graph.graph.vs) == 0
        or (
            occurrence_required
            and (occurrence_index is None or not lsp_index_path.is_file())
        )
    ):
        raise RuntimeError(f"empty graph for {instance_id}")

    record = {
        "base_commit": commit,
        "cache_hit": cache_hit,
        "duration_seconds": time.perf_counter() - started,
        "edge_count": len(graph.graph.es),
        "graph_path": str(graph_path),
        "instance_id": instance_id,
        "language": language,
        "lsp_index_path": str(lsp_index_path) if lsp_index_path.is_file() else None,
        "lsp_index_sha256": (
            _sha256_file(lsp_index_path) if lsp_index_path.is_file() else None
        ),
        "lsp_occurrence_count": (
            len(occurrence_index.occurrences) if occurrence_index is not None else 0
        ),
        "node_count": len(graph.graph.vs),
        "profile_id": binding.profile_id,
        "quality": quality,
        "repo": repo,
        "snapshot_id": binding.snapshot_id,
        "source_repo": str(source),
        "static_native_backend": static_native_backend_for_language(language),
        "status": "ready",
    }
    report_path = binding.profile_dir / "build_report.json"
    report_path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return record


def _reuse_scip_artifacts(
    source_instance: Path,
    destination: Path,
    *,
    worktree: Path,
    expected_commit: str,
) -> bool:
    """Seed a new profile from an exact-snapshot SCIP/graph artifact."""

    try:
        source = source_instance.resolve(strict=True)
    except OSError:
        return False
    source_repo = source / "repo"
    if not source_repo.is_dir():
        return False
    actual_commit = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={source_repo.resolve()}",
            "-C",
            str(source_repo),
            "rev-parse",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if actual_commit != expected_commit:
        return False
    graph_path = source / "graph.pkl"
    decoded_path = source / "index.decoded"
    if not graph_path.is_file() or not decoded_path.is_file():
        return False
    graph = load_code_graph_artifact(graph_path, project_root=str(worktree))
    graph.project_root = str(worktree)
    graph.save_graph(destination / "graph.pkl")
    shutil.copy2(decoded_path, destination / "index.decoded")
    raw_index = source / "index.scip"
    if raw_index.is_file():
        shutil.copy2(raw_index, destination / "index.scip")
    return True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _prepare_repository_group(
    rows: Sequence[Mapping[str, Any]],
    source_root: str | Path,
    output_root: str | Path,
    reuse_root: str | Path | None,
    force: bool,
) -> list[dict[str, Any]]:
    return [
        prepare_subject_artifact(
            row,
            source_root=source_root,
            output_root=output_root,
            reuse_root=reuse_root,
            force=force,
        )
        for row in sorted(rows, key=lambda item: str(item["instance_id"]))
    ]


def prepare_manifest_artifacts(
    subjects: Iterable[Mapping[str, Any]],
    *,
    source_root: str | Path,
    output_root: str | Path,
    reuse_root: str | Path | None = None,
    workers: int = 4,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Prepare repository groups in processes and commits within each serially."""

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for subject in subjects:
        grouped.setdefault(str(subject["repo"]), []).append(subject)

    records: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    with ProcessPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                _prepare_repository_group,
                rows,
                source_root,
                output_root,
                reuse_root,
                force,
            ): repo
            for repo, rows in grouped.items()
        }
        for future in as_completed(futures):
            repo = futures[future]
            try:
                records.extend(future.result())
            except Exception as exc:
                failures.append({"repo": repo, "error": str(exc)})
    if failures:
        detail = "; ".join(f"{row['repo']}: {row['error']}" for row in failures)
        raise RuntimeError(f"artifact preparation failed: {detail}")
    return sorted(records, key=lambda row: row["instance_id"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-json", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument(
        "--reuse-root",
        help="Optional exact-snapshot artifact root used to seed SCIP files.",
    )
    parser.add_argument("--summary-json")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--language", action="append")
    parser.add_argument("--instance", action="append")
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = json.loads(Path(args.manifest_json).read_text(encoding="utf-8"))
    languages = set(args.language or [])
    instances = set(args.instance or [])
    subjects = [
        row
        for row in manifest["subjects"]
        if (not languages or row["language"] in languages)
        and (not instances or row["instance_id"] in instances)
    ]
    if not subjects:
        raise SystemExit("no manifest subjects matched the requested filters")
    output_root = Path(args.output_root).expanduser().resolve()
    records = prepare_manifest_artifacts(
        subjects,
        source_root=args.source_root,
        output_root=output_root,
        reuse_root=args.reuse_root,
        workers=args.workers,
        force=args.force,
    )
    summary_path = (
        Path(args.summary_json).expanduser()
        if args.summary_json
        else output_root / "build_summary.json"
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps({"count": len(records), "records": records}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"prepared {len(records)} artifacts; summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
