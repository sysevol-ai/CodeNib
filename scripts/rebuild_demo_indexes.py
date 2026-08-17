#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rebuild current demo views with source fingerprint v2 and publish safely."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from time import perf_counter

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from codenib.compiler.cache_lock import compiler_cache_lock  # noqa: E402
from codenib.compiler.index_builders import (  # noqa: E402
    IndexBuilderRegistry,
    SymbolGraphBuilder,
    VectorIndexBuilder,
    register_default_builders,
)
from codenib.compiler.index_compiler import IndexCompiler  # noqa: E402
from codenib.compiler.index_compiler import IndexCompilerConfig
from codenib.compiler.manifest import MANIFEST_FILENAME, RepoManifest  # noqa: E402
from codenib.index.embedding.artifact_integrity import (  # noqa: E402
    require_complete_vector_view,
)
from codenib.log_utils import set_console_log_level  # noqa: E402
from codenib.source_fingerprint import is_secure_source_fingerprint_v2  # noqa: E402
from codenib.toolchains import activate_managed_toolchain  # noqa: E402
from codenib.web.config import load_config  # noqa: E402
from codenib.web.native_authority import authorize_local_manifest_vector  # noqa: E402
from codenib.web.repo_registry import RepoRegistry  # noqa: E402

_SUPPORTED_VIEWS = ("bm25", "vector", "symbol_graph")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument(
        "--repo",
        action="append",
        dest="repos",
        help="Rebuild one current registry id; repeat to select more",
    )
    parser.add_argument("--max-repos", type=int)
    parser.add_argument("--output-root")
    parser.add_argument(
        "--min-graph-nodes",
        type=int,
        default=25,
        help=(
            "Refuse publication when a rebuilt demo symbol graph has fewer "
            "nodes (default: 25; use 0 only for an intentional tiny fixture)"
        ),
    )
    parser.add_argument(
        "--ignore-dir",
        action="append",
        default=[],
        metavar="REPO_ID:DIR_NAME",
        help=(
            "Exclude a dependency/generated directory name from one demo "
            "repository's BM25 and vector views; repeat as needed"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--compact", action="store_true")
    return parser


def _configure_process_safe_directories(repo_paths: list[str]) -> None:
    """Authorize exact shared checkouts for child git commands in this process."""

    try:
        offset = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        offset = 0
    for index, repo_path in enumerate(dict.fromkeys(repo_paths), start=offset):
        os.environ[f"GIT_CONFIG_KEY_{index}"] = "safe.directory"
        os.environ[f"GIT_CONFIG_VALUE_{index}"] = os.path.realpath(repo_path)
    os.environ["GIT_CONFIG_COUNT"] = str(offset + len(set(repo_paths)))


def _select_registry_bundles(
    registry,
    repo_ids: list[str] | None,
    max_repos: int | None,
) -> list:
    """Validate selectors against the full registry before limiting work."""

    infos = list(registry.list_infos())
    selected = {str(repo_id) for repo_id in repo_ids or ()}
    available = {str(info.id) for info in infos}
    missing = sorted(selected - available)
    if missing:
        raise ValueError(f"Unknown repository ids: {missing}")
    bundles = [
        registry.get(str(info.id))
        for info in infos
        if not selected or str(info.id) in selected
    ]
    bundles = [bundle for bundle in bundles if bundle is not None]
    if max_repos is not None:
        bundles = bundles[: max(0, max_repos)]
    return bundles


def _builders(
    config,
    manifest: RepoManifest,
    *,
    additional_chunk_ignore_dirs: list[str] | None = None,
) -> IndexBuilderRegistry:
    registry = IndexBuilderRegistry()
    graph_entry = manifest.indexes.get("symbol_graph")
    graph_config = (graph_entry.config if graph_entry else {}) or {}
    graph_route = str(graph_config.get("graph_route") or "active")
    register_default_builders(
        registry,
        languages=list(manifest.languages),
        graph_route=graph_route,
        embedding_model=config.embedding_model,
        embedding_provider=config.embedding_provider,
        embedding_endpoint=config.embedding_base_url,
        embedding_dimension=config.embedding_dimension,
        additional_chunk_ignore_dirs=additional_chunk_ignore_dirs,
    )
    vector_builder = registry.get("vector")
    if isinstance(vector_builder, VectorIndexBuilder) and config.embedding_api_key:
        vector_builder.embedding_runtime_kwargs["api_key"] = config.embedding_api_key
    if graph_entry is not None:
        graph_languages = list(
            graph_config.get("languages")
            or [graph_config.get("language") or manifest.languages[0]]
        )
        registry.register(
            "symbol_graph",
            SymbolGraphBuilder(
                language=graph_languages[0],
                languages=graph_languages,
                graph_route=graph_route,
                allow_partial_languages=bool(
                    graph_config.get("allow_partial_languages", False)
                ),
                allow_partial_index=bool(
                    graph_config.get("allow_partial_index", False)
                ),
                source_coverage_fallback=bool(
                    graph_config.get("source_coverage_fallback", False)
                ),
                # Demo checkouts are shared, immutable source inputs. Package
                # manager preparation can rewrite lockfiles (and would make
                # the source fingerprint race its own build), so only consume
                # dependencies already present in the checkout.
                allow_project_preparation=False,
            ),
        )
    return registry


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parse_repo_ignore_dirs(values: list[str]) -> dict[str, list[str]]:
    """Parse explicit per-repository directory basenames."""

    parsed: dict[str, list[str]] = {}
    for value in values:
        repo_id, separator, directory = value.partition(":")
        if (
            not separator
            or not repo_id.strip()
            or not directory.strip()
            or directory in {".", ".."}
            or "/" in directory
            or "\\" in directory
        ):
            raise ValueError(
                "--ignore-dir must use REPO_ID:DIR_NAME with one directory basename"
            )
        parsed.setdefault(repo_id.strip(), []).append(directory.strip())
    return {repo: sorted(set(directories)) for repo, directories in parsed.items()}


def _validate_repo_ignore_dirs(
    repo_ignore_dirs: dict[str, list[str]],
    available_repo_ids: set[str],
) -> None:
    """Reject exclusions that would otherwise be silently ignored."""

    unknown = sorted(set(repo_ignore_dirs) - available_repo_ids)
    if unknown:
        raise ValueError(
            "Unknown repository ids in --ignore-dir: " + ", ".join(unknown)
        )


def _validated_graph_node_count(graph_dir: str, minimum: int) -> int:
    """Load the published graph surface and enforce the demo quality floor."""

    from codenib.graph.code_graph import CodeGraph

    graph_path = Path(graph_dir) / "graph.pkl"
    node_count = CodeGraph.load_graph(graph_path).get_graph().vcount()
    required = max(0, minimum)
    if node_count < required:
        raise RuntimeError(
            f"rebuilt symbol graph has only {node_count} nodes; "
            f"publication gate requires {required}"
        )
    return node_count


def _rebuild_one(
    *,
    config,
    bundle,
    output_root: Path,
    dry_run: bool,
    min_graph_nodes: int,
    additional_chunk_ignore_dirs: list[str],
) -> dict[str, object]:
    started = perf_counter()
    repo_id = str(bundle.entry.instance_id)
    target = Path(bundle.entry.manifest_path)
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"manifest must be a regular file: {target}")
    original = target.read_bytes()
    manifest = RepoManifest.from_dict(json.loads(original))
    unsupported = sorted(set(manifest.indexes) - set(_SUPPORTED_VIEWS))
    if unsupported:
        raise ValueError(f"unsupported existing views: {unsupported}")
    views = [name for name in _SUPPORTED_VIEWS if name in manifest.indexes]
    if not views:
        raise ValueError("manifest has no rebuildable views")
    if dry_run:
        return {
            "repo": repo_id,
            "status": "planned",
            "views": views,
            "manifest": str(target),
            "additional_chunk_ignore_dirs": additional_chunk_ignore_dirs,
        }

    stage = output_root / repo_id
    stage.mkdir(parents=True, exist_ok=True)
    stage_manifest_path = stage / MANIFEST_FILENAME
    staged = None
    if stage_manifest_path.is_file():
        try:
            candidate = RepoManifest.load(stage_manifest_path)
            if (
                os.path.realpath(candidate.repo_path)
                == os.path.realpath(bundle.entry.repo_dir)
                and candidate.commit == bundle.entry.base_commit
                and is_secure_source_fingerprint_v2(candidate.source_fingerprint)
            ):
                staged = candidate
        except (OSError, ValueError, KeyError, TypeError):
            staged = None
    if staged is None:
        manifest.save(stage_manifest_path)
    compiler = IndexCompiler(
        _builders(
            config,
            manifest,
            additional_chunk_ignore_dirs=additional_chunk_ignore_dirs,
        ),
        IndexCompilerConfig(
            index_types=views,
            languages=list(manifest.languages),
        ),
    )
    # This is an explicit repair/rebuild command, so do not let a previously
    # completed private generation turn a rerun into a no-op. That matters when
    # an upstream indexer exited zero but a later quality gate found a
    # pathologically small graph.
    rebuilt = compiler.compile_repo(
        bundle.entry.repo_dir,
        index_types=views,
        cache_dir=str(stage),
    )
    if not is_secure_source_fingerprint_v2(rebuilt.source_fingerprint):
        raise RuntimeError("compiler did not publish source fingerprint v2")
    failed = [
        name
        for name in views
        if not rebuilt.index_is_current(name)
        or rebuilt.indexes[name].status != "fresh"
        or rebuilt.indexes[name].source_fingerprint != rebuilt.source_fingerprint
    ]
    if failed:
        raise RuntimeError(f"rebuilt views failed source binding: {failed}")
    if "vector" in views:
        require_complete_vector_view(rebuilt.indexes["vector"].path)
    graph_node_count = None
    if "symbol_graph" in views:
        graph_node_count = _validated_graph_node_count(
            rebuilt.indexes["symbol_graph"].path,
            min_graph_nodes,
        )
    expected_commit = str(bundle.entry.base_commit or "")
    if expected_commit and rebuilt.commit != expected_commit:
        raise RuntimeError(
            f"rebuilt commit {rebuilt.commit!r} does not match registry "
            f"commit {expected_commit!r}"
        )

    with compiler_cache_lock(target.parent):
        current = target.read_bytes()
        if _digest(current) != _digest(original):
            raise RuntimeError("manifest changed during rebuild; refusing publication")
        backup = target.with_name(f"{target.name}.pre-source-v2")
        if not backup.exists():
            shutil.copy2(target, backup)
        rebuilt.save(target)

    return {
        "repo": repo_id,
        "status": "rebuilt",
        "views": views,
        "manifest": str(target),
        "source_fingerprint": rebuilt.source_fingerprint,
        "duration_seconds": round(perf_counter() - started, 2),
        "graph_node_count": graph_node_count,
        "additional_chunk_ignore_dirs": additional_chunk_ignore_dirs,
        "artifacts": {name: rebuilt.indexes[name].path for name in views},
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    set_console_log_level("INFO")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    activate_managed_toolchain()
    config = load_config(args.config)
    try:
        repo_ignore_dirs = _parse_repo_ignore_dirs(args.ignore_dir)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    registry = RepoRegistry(
        config,
        native_index_authorization_resolver=authorize_local_manifest_vector,
        allow_missing_native_index_authorization=True,
    )
    registry.load_all()
    try:
        _validate_repo_ignore_dirs(
            repo_ignore_dirs,
            {str(info.id) for info in registry.list_infos()},
        )
        bundles = _select_registry_bundles(registry, args.repos, args.max_repos)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _configure_process_safe_directories(
        [str(bundle.entry.repo_dir) for bundle in bundles]
    )
    output_root = Path(
        args.output_root
        or os.path.join(os.path.abspath(config.data_dir), "rebuilds-source-v2")
    )
    results = []
    for bundle in bundles:
        repo_id = str(bundle.entry.instance_id)
        print(f"Rebuilding {repo_id}…", file=sys.stderr, flush=True)
        try:
            results.append(
                _rebuild_one(
                    config=config,
                    bundle=bundle,
                    output_root=output_root,
                    dry_run=args.dry_run,
                    min_graph_nodes=args.min_graph_nodes,
                    additional_chunk_ignore_dirs=repo_ignore_dirs.get(repo_id, []),
                )
            )
        except Exception as exc:  # noqa: BLE001 - report the rest of the batch
            results.append({"repo": repo_id, "status": "error", "error": str(exc)})
            if args.stop_on_error:
                break
    report = {
        "schema_version": 1,
        "dry_run": args.dry_run,
        "selected": len(bundles),
        "counts": {
            status: sum(1 for item in results if item["status"] == status)
            for status in sorted({str(item["status"]) for item in results})
        },
        "repositories": results,
    }
    print(
        json.dumps(
            report,
            indent=None if args.compact else 2,
            separators=(",", ":") if args.compact else None,
            sort_keys=True,
        )
    )
    return 1 if any(item["status"] == "error" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
