# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Prepare a local repository for the CodeNib Wiki runtime."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..compiler.manifest import RepoManifest
from ..compiler.snapshot_store import normalize_repo
from ..llm.options import validate_model_options
from ..source_fingerprint import (
    capture_repository_source,
    is_secure_source_fingerprint_v2,
    lexical_repository_path,
)
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
    embedding_api_key_env: str | None = None,
    model_options: Mapping[str, Any] | None = None,
) -> LocalWiki:
    """Write the registry and config consumed by the existing Wiki service."""
    repo_path = lexical_repository_path(repo_path)
    manifest_path = Path(os.path.abspath(os.fspath(manifest_path.expanduser())))
    manifest = RepoManifest.load(str(manifest_path))
    if not is_secure_source_fingerprint_v2(manifest.source_fingerprint):
        raise ValueError("local wiki source reads require source fingerprint v2")
    from ..artifacts.runtime import (
        SourceBindingCleanupOwner,
        _attach_source_cleanup_owner,
    )

    cleanup_owner = SourceBindingCleanupOwner()
    try:
        source_binding = capture_repository_source(
            repo_path,
            exclude_roots=(manifest_path.parent,),
            _source_owner=cleanup_owner.retain,
        )
        if (
            source_binding.fingerprint != manifest.source_fingerprint
            or source_binding.file_count != manifest.file_count
        ):
            raise ValueError("repository source content does not match the manifest")
    except BaseException as primary:  # noqa: B036 - preserve validation fault
        try:
            cleanup_owner.close()
        except BaseException:  # noqa: B036 - expose retry owner on primary
            _attach_source_cleanup_owner(primary, cleanup_owner)
        raise
    try:
        cleanup_owner.close()
    except BaseException as cleanup_failure:  # noqa: B036 - retryable owner
        _attach_source_cleanup_owner(cleanup_failure, cleanup_owner)
        raise

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

    mode = "hybrid" if manifest.index_is_current("vector") else "sparse"
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
    options = validate_model_options(model_options)
    if options:
        config["model_options"] = options
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, sort_keys=True)

    runtime_env: dict[str, str] = {}
    if model:
        runtime_env["CODENIB_DEMO_MODEL"] = model
    if api_base:
        runtime_env["CODENIB_DEMO_API_BASE"] = api_base
    if options:
        runtime_env["CODENIB_DEMO_MODEL_OPTIONS"] = json.dumps(
            options,
            sort_keys=True,
            separators=(",", ":"),
        )
    if api_key_env:
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ValueError(
                f"API key environment variable is unset or empty: {api_key_env}"
            )
        runtime_env["CODENIB_DEMO_API_KEY"] = api_key
    if embedding_api_key_env:
        embedding_api_key = os.environ.get(embedding_api_key_env)
        if not embedding_api_key:
            raise ValueError(
                "Embedding API key environment variable is unset or empty: "
                f"{embedding_api_key_env}"
            )
        runtime_env["CODENIB_EMBEDDING_API_KEY"] = embedding_api_key

    return LocalWiki(
        repo_path=repo_path,
        manifest_path=manifest_path,
        data_dir=data_dir,
        config_path=config_path,
        repo_id=repo_id,
        runtime_env=runtime_env,
    )


__all__ = ["LocalWiki", "prepare_local_wiki"]
