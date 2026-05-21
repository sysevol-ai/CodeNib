#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Build CodeMiner indexes for the DeepWiki-style demo repos.

Reads ``demo_repos.yaml`` (or ``--config``), clones each remote repo at its
pinned commit into the demo data dir, and runs ``IndexCompiler.compile_repo``
to produce ``repo_manifest.json`` + index artifacts. Idempotent: a repo whose
manifest already exists is skipped unless ``--force`` is given.

Usage::

    python scripts/build_demo_index.py
    python scripts/build_demo_index.py --config demo_repos.yaml --force
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

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
from codeminer.web.config import CACHE_DIR_NAME, RepoConfig, load_config  # noqa: E402


def _ensure_checkout(repo: RepoConfig, repo_dir: str) -> None:
    """Clone ``repo`` into ``repo_dir`` (and check out ``commit``) if needed."""
    if repo.path:
        if not os.path.isdir(repo_dir):
            raise FileNotFoundError(f"Local repo path does not exist: {repo_dir}")
        return
    if not repo.url:
        raise ValueError(f"Repo {repo.id!r} has neither 'url' nor 'path'")

    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        os.makedirs(os.path.dirname(repo_dir), exist_ok=True)
        print(f"  Cloning {repo.url} -> {repo_dir}")
        subprocess.run(["git", "clone", repo.url, repo_dir], check=True)

    if repo.commit:
        print(f"  Checking out {repo.commit}")
        subprocess.run(["git", "-C", repo_dir, "checkout", repo.commit], check=True)


def build_repo(cfg, repo: RepoConfig, force: bool) -> bool:
    repo_dir = cfg.repo_dir(repo)
    manifest_path = cfg.manifest_path(repo)
    if os.path.exists(manifest_path) and not force:
        print(f"[skip] {repo.id}: manifest exists ({manifest_path})")
        return False

    print(f"[build] {repo.id} ({repo.name})")
    _ensure_checkout(repo, repo_dir)

    index_types = cfg.index_types()
    builder_registry = IndexBuilderRegistry()
    builder_registry.register("bm25", BM25IndexBuilder(languages=repo.languages))
    if "vector" in index_types:
        builder_registry.register(
            "vector",
            VectorIndexBuilder(
                languages=repo.languages,
                embedding_model=cfg.embedding_model,
                embedding_dimension=cfg.embedding_dimension,
            ),
        )

    compiler = IndexCompiler(
        builder_registry,
        IndexCompilerConfig(index_types=index_types, languages=repo.languages),
    )
    cache_dir = os.path.join(repo_dir, CACHE_DIR_NAME)
    manifest = compiler.compile_repo(repo_dir, cache_dir=cache_dir)
    print(
        f"  Done: {manifest.file_count} files, "
        f"indexes={list(manifest.indexes)}, caps={manifest.capabilities}"
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Build demo repo indexes")
    parser.add_argument("--config", default=None, help="Path to demo_repos.yaml")
    parser.add_argument(
        "--force", action="store_true", help="Rebuild even if a manifest exists"
    )
    parser.add_argument("--only", default=None, help="Build only this repo id")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if not cfg.repos:
        print("No repos configured. Add some to demo_repos.yaml.", file=sys.stderr)
        sys.exit(1)

    built = 0
    for repo in cfg.repos:
        if args.only and repo.id != args.only:
            continue
        if build_repo(cfg, repo, args.force):
            built += 1
    print(f"\nBuilt {built} repo(s). Data dir: {os.path.abspath(cfg.data_dir)}")


if __name__ == "__main__":
    main()
