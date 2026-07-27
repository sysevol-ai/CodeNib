# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Prepare a local repository for the CodeNib Wiki runtime."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..compiler.manifest import RepoManifest
from ..compiler.snapshot_store import normalize_repo
from .config import RepoEntry, save_registry


@dataclass(frozen=True, slots=True)
class LocalWiki:
    repo_path: Path
    manifest_path: Path
    data_dir: Path
    config_path: Path
    repo_id: str
    runtime_env: dict[str, str] = field(default_factory=dict, repr=False)


def _repo_id(name: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-").lower()
    return value or "repository"


def _origin_url(repo_path: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
        capture_output=True,
        check=False,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _repository_slug(repo_path: Path) -> str:
    origin = _origin_url(repo_path)
    if origin:
        try:
            slug = normalize_repo(origin)
            if slug.count("/") == 1:
                return slug
        except ValueError:
            pass
    return repo_path.name


def prepare_local_wiki(
    repo_path: Path,
    manifest_path: Path,
    *,
    frontend_port: int,
    agent_wiki: bool = False,
    model: str | None = None,
    api_base: str | None = None,
    api_key_env: str | None = None,
) -> LocalWiki:
    """Write the registry and config consumed by the existing Wiki service."""
    repo_path = repo_path.expanduser().resolve()
    manifest_path = manifest_path.expanduser().resolve()
    manifest = RepoManifest.load(str(manifest_path))

    data_dir = manifest_path.parent / "wiki"
    data_dir.mkdir(parents=True, exist_ok=True)
    repo_name = _repository_slug(repo_path)
    repo_id = _repo_id(repo_name.rsplit("/", 1)[-1])
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
    if model:
        config["model"] = model
    if api_base:
        config["model_api_base"] = api_base
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, sort_keys=True)

    runtime_env: dict[str, str] = {}
    if api_key_env:
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ValueError(
                f"API key environment variable is unset or empty: {api_key_env}"
            )
        runtime_env["CODENIB_DEMO_API_KEY"] = api_key

    return LocalWiki(
        repo_path=repo_path,
        manifest_path=manifest_path,
        data_dir=data_dir,
        config_path=config_path,
        repo_id=repo_id,
        runtime_env=runtime_env,
    )


__all__ = ["LocalWiki", "prepare_local_wiki"]
