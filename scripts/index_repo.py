#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0
"""Build BM25 index for a repo and register it in qa_registry.json.

Usage:
    python scripts/index_repo.py /path/to/repo
    python scripts/index_repo.py /path/to/repo --language go
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(levelname)-8s %(message)s")

from codeminer.compiler import IndexCompiler, IndexCompilerConfig
from codeminer.compiler.index_builders import IndexBuilderRegistry, register_default_builders


def get_base_commit(repo_dir: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir, capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def detect_language(repo_dir: str) -> str:
    counts: dict = {}
    ext_map = {
        ".py": "python", ".go": "go", ".rs": "rust",
        ".ts": "javascript", ".tsx": "javascript", ".js": "javascript",
        ".cpp": "cpp", ".cc": "cpp", ".c": "cpp",
    }
    for root, _, files in os.walk(repo_dir):
        if "/.git" in root or "/.codeminer" in root:
            continue
        for f in files:
            lang = ext_map.get(os.path.splitext(f)[1].lower())
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
    return max(counts, key=counts.get) if counts else "python"


def update_registry(registry_path: str, entry: dict) -> None:
    entries = []
    if os.path.isfile(registry_path):
        with open(registry_path) as f:
            entries = json.load(f)
    entries = [e for e in entries if e.get("instance_id") != entry["instance_id"]]
    entries.append(entry)
    os.makedirs(os.path.dirname(os.path.abspath(registry_path)), exist_ok=True)
    with open(registry_path, "w") as f:
        json.dump(entries, f, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Index a repo for CodeMiner web demo")
    parser.add_argument("repo_dir", help="Absolute path to the repo to index")
    parser.add_argument("--language", help="Language override (auto-detected if omitted)")
    parser.add_argument(
        "--registry",
        default=".codeminer_qa/qa_registry.json",
        help="Path to qa_registry.json (default: .codeminer_qa/qa_registry.json)",
    )
    args = parser.parse_args()

    repo_dir = os.path.abspath(args.repo_dir)
    if not os.path.isdir(repo_dir):
        print(f"ERROR: Directory not found: {repo_dir}")
        sys.exit(1)

    language = args.language or detect_language(repo_dir)
    print(f"Detected language: {language}")

    # Build BM25 index
    reg = IndexBuilderRegistry()
    register_default_builders(reg, languages=[language])
    manifest = IndexCompiler(reg, IndexCompilerConfig(index_types=["bm25"])).compile_repo(repo_dir)

    print("\nIndexes built:")
    for name, idx in manifest.indexes.items():
        print(f"  {name}: {idx.status}  ({idx.path})")

    # Derive owner/repo from git remote
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_dir, capture_output=True, text=True,
        ).stdout.strip()
        m = re.search(r"[:/]([^/]+)/([^/.]+?)(?:\.git)?$", remote)
        owner, repo = (m.group(1), m.group(2)) if m else ("local", os.path.basename(repo_dir))
    except Exception:
        owner, repo = "local", os.path.basename(repo_dir)

    github_repo = f"{owner}/{repo}"
    instance_id = f"{owner}__{repo}"
    base_commit = get_base_commit(repo_dir)
    manifest_path = os.path.join(repo_dir, ".codeminer_cache", "repo_manifest.json")

    entry = {
        "instance_id": instance_id,
        "repo": github_repo,
        "base_commit": base_commit,
        "language": language,
        "repo_dir": repo_dir,
        "manifest_path": manifest_path,
        "problem_statement": "",
    }

    update_registry(args.registry, entry)
    print(f"\nRegistered '{instance_id}' in {args.registry}")
    print(f"  repo:    {github_repo}")
    print(f"  commit:  {base_commit[:12] if base_commit else '(unknown)'}")
    print(f"\nDone! Restart the backend to pick up the new repo.")


if __name__ == "__main__":
    main()
