#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Build CodeNib indexes for code-QA demo repos.

Selects a varied set of instances from the **codenib-base-dataset**, checks
out each repo at its ``base_commit`` (via ``CodeNibBaseDataset`` /
``process_instance``), builds CodeNib indexes with ``IndexCompiler``, and
writes ``<data_dir>/qa_registry.json`` describing what was indexed. The server
(``codenib-web``) reads that registry.

Selection: explicit ``instances:`` in ``qa_config.yaml`` if present, otherwise
``per_language`` instances from each of ``languages`` (a varied sample).

Usage::

    python scripts/build_qa_index.py                 # uses qa_config.yaml
    python scripts/build_qa_index.py --per-language 2
    python scripts/build_qa_index.py --instances django__django-11099,...
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from codenib.compiler.index_builders import (  # noqa: E402
    BM25IndexBuilder,
    IndexBuilderRegistry,
    VectorIndexBuilder,
)
from codenib.compiler.index_compiler import (  # noqa: E402
    IndexCompiler,
    IndexCompilerConfig,
)
from codenib.compiler.manifest import (  # noqa: E402
    MANIFEST_FILENAME,
    IndexEntry,
    RepoManifest,
)
from codenib.dataset.codenib_base import CodeNibBaseDataset  # noqa: E402
from codenib.web.config import (  # noqa: E402
    CACHE_DIR_NAME,
    RepoEntry,
    load_config,
    save_registry,
)

# Map dataset language_group -> tree-sitter chunker languages.
# Keys are normalized single-label tokens (lower-case, no slashes). The dataset
# uses slash-separated labels like "TypeScript/JavaScript" or "C++/C" — we split
# on '/' and merge the per-token chunker lists.
_LANG_MAP: Dict[str, List[str]] = {
    "python": ["python"],
    "javascript": ["javascript", "typescript"],
    "typescript": ["typescript", "javascript"],
    "go": ["go"],
    "rust": ["rust"],
    "c": ["cpp"],  # the cpp chunker handles .c too (CLAUDE.md)
    "c#": ["csharp"],
    "csharp": ["csharp"],
    "cs": ["csharp"],
    "cpp": ["cpp"],
    "c++": ["cpp"],
    "java": ["java"],
    "ruby": ["ruby"],
    "php": ["php"],
    "kotlin": ["kotlin"],
    "kt": ["kotlin"],
}


def _chunker_languages(language_group: str) -> List[str]:
    raw = (language_group or "").lower()
    if not raw:
        return ["python"]
    seen: List[str] = []
    for token in raw.split("/"):
        for lang in _LANG_MAP.get(token.strip(), []):
            if lang not in seen:
                seen.append(lang)
    return seen or ["python"]


def _prebuilt_paths(prebuilt_dir: str, instance_id: str, embedding_model: str):
    """Return the (repo, vector-output-dir, l2 faiss probe) paths under a prebuilt tree.

    The pre-built tree on disk looks like::

        <prebuilt_dir>/<instance_id>/repo/                       # source @ base_commit
        <prebuilt_dir>/<instance_id>/config_<model_suffix>.json   # vector store top-level
        <prebuilt_dir>/<instance_id>/l0/index_<model_suffix>.faiss
        <prebuilt_dir>/<instance_id>/l2/index_<model_suffix>.faiss

    ``CodeVectorStore.load`` is given the parent (``<prebuilt_dir>/<instance_id>``)
    and discovers the per-level files itself.
    """
    inst_dir = os.path.join(os.path.abspath(prebuilt_dir), instance_id)
    repo_dir = os.path.join(inst_dir, "repo")
    suffix = embedding_model.replace("/", "__")
    l2_faiss = os.path.join(inst_dir, "l2", f"index_{suffix}.faiss")
    return inst_dir, repo_dir, l2_faiss


def _has_prebuilt(cfg, instance_id: str) -> bool:
    """True iff the pre-built tree has both source + vector for this instance."""
    if not cfg.prebuilt_dir:
        return False
    _, repo_dir, l2_faiss = _prebuilt_paths(
        cfg.prebuilt_dir, instance_id, cfg.embedding_model
    )
    return os.path.isdir(repo_dir) and os.path.isfile(l2_faiss)


def select_rows(dataset, cfg, per_language: int, instances: List[str]):
    """Return the dataset rows to index.

    When ``cfg.prebuilt_dir`` is set, the per-language sampler restricts itself
    to instances whose pre-built tree exists so the demo never falls back to
    cloning huge repos. Explicit ``--instances`` lists are honored regardless.
    """
    rows = list(dataset)
    by_id = {r["instance_id"]: r for r in rows}

    if instances:
        chosen = [by_id[i] for i in instances if i in by_id]
        missing = [i for i in instances if i not in by_id]
        if missing:
            print(f"  WARNING: instance ids not found: {missing}")
        return chosen

    chosen_ids = set()
    chosen = []
    for lang in cfg.languages:
        target = lang.lower()
        picked = 0
        for r in rows:
            if r["instance_id"] in chosen_ids:
                continue
            # language_group is sometimes a slash-separated multi-label
            # (e.g. "TypeScript/JavaScript", "C++/C"). Match any segment.
            parts = [
                p.strip().lower() for p in (r.get("language_group") or "").split("/")
            ]
            if target not in parts:
                continue
            if cfg.prebuilt_dir and not _has_prebuilt(cfg, r["instance_id"]):
                continue
            chosen.append(r)
            chosen_ids.add(r["instance_id"])
            picked += 1
            if picked >= per_language:
                break
    return chosen


def _count_files(repo_path: str) -> int:
    count = 0
    for _, _, files in os.walk(repo_path):
        count += len(files)
    return count


def build_one_prebuilt(cfg, row, force: bool) -> RepoEntry:
    """Reuse <prebuilt_dir>/<instance_id>/ — skip clone, build BM25 only,
    and write a manifest whose ``vector`` entry points at the pre-built tree.

    The manifest + BM25 land under ``<data_dir>/cache/<instance_id>/`` so we
    never need write access to the (potentially shared, foreign-owned) pre-built
    tree itself.
    """
    instance_id = row["instance_id"]
    repo = row["repo"]
    base_commit = row["base_commit"]
    language = row.get("language_group", "")
    languages = _chunker_languages(language)

    inst_dir, repo_path, _ = _prebuilt_paths(
        cfg.prebuilt_dir, instance_id, cfg.embedding_model
    )
    cache_dir = os.path.join(os.path.abspath(cfg.data_dir), "cache", instance_id)
    os.makedirs(cache_dir, exist_ok=True)
    manifest_path = os.path.join(cache_dir, MANIFEST_FILENAME)

    print(
        f"[prebuilt] {instance_id}: repo={repo_path} " f"vector={inst_dir} ({language})"
    )

    if os.path.exists(manifest_path) and not force:
        print(f"  [skip] manifest exists at {manifest_path}")
        return RepoEntry(
            instance_id=instance_id,
            repo=repo,
            base_commit=base_commit,
            language=language,
            repo_dir=repo_path,
            manifest_path=manifest_path,
            problem_statement=(row.get("problem_statement") or "")[:4000],
        )

    # BM25 is missing from the pre-built tree — build it locally.
    bm25_dir = os.path.join(cache_dir, "bm25")
    bm25_builder = BM25IndexBuilder(languages=languages)
    print(f"  [index] bm25 -> {bm25_dir} languages={languages}")
    bm25_status = bm25_builder.build(
        scope="current_repo", repo_path=repo_path, output_dir=bm25_dir
    )

    now = datetime.now(timezone.utc)
    manifest = RepoManifest(
        repo_path=repo_path,
        commit=base_commit,
        last_indexed_commit=base_commit,
        languages=list(languages),
        file_count=_count_files(repo_path),
    )
    manifest.indexes["bm25"] = IndexEntry(
        index_type="bm25",
        path=bm25_dir,
        built_at=now.isoformat(),
        built_at_epoch=now.timestamp(),
        status="fresh",
        config=dict(bm25_status.metadata),
        metadata={**bm25_status.metadata},
    )
    # Vector index entry points at the pre-built dir. CodeVectorStore.load()
    # reads <path>/config_<suffix>.json + <path>/{l0,l2}/{index,documents}_<suffix>.*
    vector_config = {
        "embedding_model": cfg.embedding_model,
        "embedding_provider": cfg.embedding_provider,
        "embedding_dimension": cfg.embedding_dimension,
    }
    if cfg.embedding_base_url:
        vector_config["embedding_endpoint"] = cfg.embedding_base_url
    manifest.indexes["vector"] = IndexEntry(
        index_type="vector",
        path=inst_dir,
        built_at=now.isoformat(),
        built_at_epoch=now.timestamp(),
        status="fresh",
        config=vector_config,
        metadata={
            "embedding_model": cfg.embedding_model,
            "embedding_provider": cfg.embedding_provider,
            "levels": ["l0", "l2"],
            "source": "prebuilt",
        },
    )
    manifest.derive_capabilities()
    manifest.compiled_at = now.isoformat()
    manifest.compiled_at_epoch = now.timestamp()
    manifest.save(manifest_path)
    print(
        f"  [done] caps={manifest.capabilities} "
        f"(bm25={bm25_dir!r}, vector={inst_dir!r})"
    )

    return RepoEntry(
        instance_id=instance_id,
        repo=repo,
        base_commit=base_commit,
        language=language,
        repo_dir=repo_path,
        manifest_path=manifest_path,
        problem_statement=(row.get("problem_statement") or "")[:4000],
    )


def build_one(cfg, row, force: bool) -> RepoEntry:
    instance_id = row["instance_id"]
    repo = row["repo"]
    base_commit = row["base_commit"]
    language = row.get("language_group", "")

    ds = CodeNibBaseDataset(dataset=cfg.dataset, split=cfg.split)
    repo_root = cfg.repo_dir(instance_id)
    os.makedirs(repo_root, exist_ok=True)

    print(f"[checkout] {instance_id}: {repo} @ {base_commit[:8]} ({language})")
    ds.process_instance(row, repo_root=repo_root)
    repo_path = ds.get_repo_path(row, repo_root=repo_root)

    manifest_path = os.path.join(repo_path, CACHE_DIR_NAME, "repo_manifest.json")
    manifest_exists = os.path.exists(manifest_path)
    if manifest_exists and not force:
        # Advance the existing indexes to HEAD instead of skipping outright.
        # Builders with a real delta path (vector) do incremental work; the
        # others fall back to a rebuild internally, and an unchanged HEAD is a
        # no-op, so this stays cheap when there is nothing to do.
        languages = _chunker_languages(language)
        index_types = cfg.index_types()
        builders = IndexBuilderRegistry()
        builders.register("bm25", BM25IndexBuilder(languages=languages))
        if "vector" in index_types:
            builders.register(
                "vector",
                VectorIndexBuilder(
                    languages=languages,
                    embedding_model=cfg.embedding_model,
                    embedding_dimension=cfg.embedding_dimension,
                ),
            )
        compiler = IndexCompiler(
            builders,
            IndexCompilerConfig(index_types=index_types, languages=languages),
        )
        print(f"  [update] advancing indexes to HEAD ({index_types})")
        manifest = compiler.update_repo(
            repo_path, cache_dir=os.path.join(repo_path, CACHE_DIR_NAME)
        )
        print(f"  [done] {manifest.file_count} files, caps={manifest.capabilities}")
    else:
        languages = _chunker_languages(language)
        index_types = cfg.index_types()
        builders = IndexBuilderRegistry()
        builders.register("bm25", BM25IndexBuilder(languages=languages))
        if "vector" in index_types:
            builders.register(
                "vector",
                VectorIndexBuilder(
                    languages=languages,
                    embedding_model=cfg.embedding_model,
                    embedding_dimension=cfg.embedding_dimension,
                ),
            )
        compiler = IndexCompiler(
            builders,
            IndexCompilerConfig(index_types=index_types, languages=languages),
        )
        print(f"  [index] {index_types} languages={languages}")
        manifest = compiler.compile_repo(
            repo_path, cache_dir=os.path.join(repo_path, CACHE_DIR_NAME)
        )
        print(f"  [done] {manifest.file_count} files, caps={manifest.capabilities}")

    return RepoEntry(
        instance_id=instance_id,
        repo=repo,
        base_commit=base_commit,
        language=language,
        repo_dir=repo_path,
        manifest_path=manifest_path,
        problem_statement=(row.get("problem_statement") or "")[:4000],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build code-QA demo indexes")
    parser.add_argument("--config", default=None, help="Path to qa_config.yaml")
    parser.add_argument("--per-language", type=int, default=None)
    parser.add_argument(
        "--instances", default=None, help="Comma-separated instance ids"
    )
    parser.add_argument("--force", action="store_true", help="Rebuild indexes")
    args = parser.parse_args()

    cfg = load_config(args.config)
    per_language = args.per_language or cfg.per_language
    instances = (
        [s.strip() for s in args.instances.split(",") if s.strip()]
        if args.instances
        else list(cfg.instances)
    )

    print(f"Loading dataset {cfg.dataset} (split={cfg.split})…")
    dataset = CodeNibBaseDataset(dataset=cfg.dataset, split=cfg.split).load()
    rows = select_rows(dataset, cfg, per_language, instances)
    if not rows:
        print("No instances selected. Check qa_config.yaml.", file=sys.stderr)
        sys.exit(1)
    print(f"Selected {len(rows)} instance(s).")

    entries: List[RepoEntry] = []
    for row in rows:
        try:
            if _has_prebuilt(cfg, row["instance_id"]):
                entries.append(build_one_prebuilt(cfg, row, args.force))
            else:
                entries.append(build_one(cfg, row, args.force))
        except Exception as exc:  # noqa: BLE001 - keep building the rest
            print(f"  ERROR building {row.get('instance_id')}: {exc}")

    if entries:
        save_registry(cfg.registry_path, entries)
        print(f"\nWrote {cfg.registry_path} with {len(entries)} repo(s).")
    else:
        print("No repos indexed.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
