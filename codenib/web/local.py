# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Prepare a local repository for the CodeNib Wiki runtime."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..compiler.manifest import RepoManifest
from .config import RepoEntry, save_registry


@dataclass(frozen=True, slots=True)
class LocalWiki:
    repo_path: Path
    manifest_path: Path
    data_dir: Path
    config_path: Path
    repo_id: str


def _repo_id(name: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-").lower()
    return value or "repository"


def prepare_local_wiki(
    repo_path: Path,
    manifest_path: Path,
    *,
    frontend_port: int,
    agent_wiki: bool = False,
) -> LocalWiki:
    """Write the registry and config consumed by the existing Wiki service."""
    repo_path = repo_path.expanduser().resolve()
    manifest_path = manifest_path.expanduser().resolve()
    manifest = RepoManifest.load(str(manifest_path))

    data_dir = repo_path / ".codenib_cache" / "wiki"
    data_dir.mkdir(parents=True, exist_ok=True)
    repo_name = repo_path.name
    repo_id = _repo_id(repo_name)
    language = manifest.languages[0] if manifest.languages else "unknown"
    save_registry(
        str(data_dir / "qa_registry.json"),
        [
            RepoEntry(
                instance_id=repo_id,
                repo=repo_name,
                base_commit=manifest.commit,
                language=language,
                repo_dir=str(repo_path),
                manifest_path=str(manifest_path),
            )
        ],
    )

    vector_entry = manifest.indexes.get("vector")
    mode = (
        "hybrid"
        if vector_entry is not None and vector_entry.status == "fresh"
        else "sparse"
    )
    config_path = data_dir / "config.yaml"
    config = {
        "data_dir": str(data_dir),
        "mode": mode,
        "wiki_agent": agent_wiki,
        "cors_origins": [
            f"http://localhost:{frontend_port}",
            f"http://127.0.0.1:{frontend_port}",
        ],
    }
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, sort_keys=True)

    return LocalWiki(
        repo_path=repo_path,
        manifest_path=manifest_path,
        data_dir=data_dir,
        config_path=config_path,
        repo_id=repo_id,
    )


__all__ = ["LocalWiki", "prepare_local_wiki"]
