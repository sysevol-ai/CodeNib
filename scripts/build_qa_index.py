#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Build CodeMiner indexes for code-QA demo repos.

Selects a varied set of instances from the **codeminer-base-dataset**, checks
out each repo at its ``base_commit`` (via ``CodeMinerBaseDataset`` /
``process_instance``), builds CodeMiner indexes with ``IndexCompiler``, and
writes ``<data_dir>/qa_registry.json`` describing what was indexed. The server
(``codeminer-web``) reads that registry.

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
from pathlib import Path
from typing import Dict, List

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from codeminer.compiler.index_builders import (  # noqa: E402
    BM25IndexBuilder,
    IndexBuilderRegistry,
    VectorIndexBuilder,
)
from codeminer.compiler.index_compiler import (  # noqa: E402
    IndexCompiler,
    IndexCompilerConfig,
)
from codeminer.dataset.codeminer_base import CodeMinerBaseDataset  # noqa: E402
from codeminer.web.config import (  # noqa: E402
    CACHE_DIR_NAME,
    RepoEntry,
    load_config,
    save_registry,
)

# Map dataset language_group -> tree-sitter chunker languages.
_LANG_MAP: Dict[str, List[str]] = {
    "python": ["python"],
    "javascript": ["javascript", "typescript"],
    "typescript": ["typescript", "javascript"],
    "go": ["go"],
    "rust": ["rust"],
    "c": ["c"],
    "cpp": ["cpp"],
    "c++": ["cpp"],
    "java": ["java"],
}


def _chunker_languages(language_group: str) -> List[str]:
    return _LANG_MAP.get((language_group or "").lower(), ["python"])


def select_rows(dataset, cfg, per_language: int, instances: List[str]):
    """Return the dataset rows to index."""
    rows = list(dataset)
    by_id = {r["instance_id"]: r for r in rows}

    if instances:
        chosen = [by_id[i] for i in instances if i in by_id]
        missing = [i for i in instances if i not in by_id]
        if missing:
            print(f"  WARNING: instance ids not found: {missing}")
        return chosen

    chosen = []
    for lang in cfg.languages:
        picked = 0
        for r in rows:
            if (r.get("language_group") or "").lower() == lang.lower():
                chosen.append(r)
                picked += 1
                if picked >= per_language:
                    break
    return chosen


def build_one(cfg, row, force: bool) -> RepoEntry:
    instance_id = row["instance_id"]
    repo = row["repo"]
    base_commit = row["base_commit"]
    language = row.get("language_group", "")

    ds = CodeMinerBaseDataset(dataset=cfg.dataset, split=cfg.split)
    repo_root = cfg.repo_dir(instance_id)
    os.makedirs(repo_root, exist_ok=True)

    print(f"[checkout] {instance_id}: {repo} @ {base_commit[:8]} ({language})")
    ds.process_instance(row, repo_root=repo_root)
    repo_path = ds.get_repo_path(row, repo_root=repo_root)

    manifest_path = os.path.join(repo_path, CACHE_DIR_NAME, "repo_manifest.json")
    if os.path.exists(manifest_path) and not force:
        print(f"  [skip] manifest exists at {manifest_path}")
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
    dataset = CodeMinerBaseDataset(dataset=cfg.dataset, split=cfg.split).load()
    rows = select_rows(dataset, cfg, per_language, instances)
    if not rows:
        print("No instances selected. Check qa_config.yaml.", file=sys.stderr)
        sys.exit(1)
    print(f"Selected {len(rows)} instance(s).")

    entries: List[RepoEntry] = []
    for row in rows:
        try:
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
