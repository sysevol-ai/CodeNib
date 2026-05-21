# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Configuration for the DeepWiki-style demo.

The demo serves a *fixed* set of repositories declared in a YAML file
(``demo_repos.yaml`` at the repo root by default). The same config drives both
the offline build step (``scripts/build_demo_index.py``) and the online server
(``codeminer.web.app``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

DEFAULT_CONFIG_PATH = "demo_repos.yaml"
CACHE_DIR_NAME = ".codeminer_cache"


@dataclass(slots=True)
class RepoConfig:
    """A single demo repository."""

    id: str
    name: str
    languages: List[str] = field(default_factory=lambda: ["python"])
    # Source: either a remote ``url`` (cloned at build time) or an existing
    # local ``path``. ``commit`` pins the checkout when ``url`` is used.
    url: Optional[str] = None
    commit: Optional[str] = None
    path: Optional[str] = None
    description: str = ""


@dataclass(slots=True)
class DemoConfig:
    """Top-level demo configuration."""

    # litellm model string used by the agent (e.g. "gpt-4o",
    # "vertex_ai/gemini-2.5-flash"). Env ``CODEMINER_DEMO_MODEL`` wins.
    model: str = "gpt-4o"
    # "sparse" (BM25 only) or "hybrid" (BM25 + vector embeddings).
    mode: str = "sparse"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dimension: int = 384
    # Where cloned repos + indexes live (relative to repo root unless absolute).
    data_dir: str = ".codeminer_demo"
    max_turns: int = 5
    max_tokens: int = 1024
    # CORS origins allowed to call the API (the Next.js dev server).
    cors_origins: List[str] = field(default_factory=lambda: ["http://localhost:3000"])
    repos: List[RepoConfig] = field(default_factory=list)

    def index_types(self) -> List[str]:
        return ["bm25", "vector"] if self.mode == "hybrid" else ["bm25"]

    def repo_dir(self, repo: RepoConfig) -> str:
        """Absolute working directory for a repo's source checkout."""
        if repo.path:
            return os.path.abspath(repo.path)
        return os.path.join(os.path.abspath(self.data_dir), repo.id)

    def manifest_path(self, repo: RepoConfig) -> str:
        return os.path.join(self.repo_dir(repo), CACHE_DIR_NAME, "repo_manifest.json")

    def get_repo(self, repo_id: str) -> Optional[RepoConfig]:
        for r in self.repos:
            if r.id == repo_id:
                return r
        return None


def load_config(path: Optional[str] = None) -> DemoConfig:
    """Load demo config from YAML, applying env overrides.

    Env overrides: ``CODEMINER_DEMO_CONFIG`` (path), ``CODEMINER_DEMO_MODEL``,
    ``CODEMINER_DEMO_DATA_DIR``.
    """
    cfg_path = path or os.environ.get("CODEMINER_DEMO_CONFIG", DEFAULT_CONFIG_PATH)
    data = {}
    if Path(cfg_path).exists():
        with open(cfg_path) as f:
            data = yaml.safe_load(f) or {}

    repos = [RepoConfig(**r) for r in data.get("repos", [])]
    cfg = DemoConfig(
        model=data.get("model", DemoConfig.model),
        mode=data.get("mode", DemoConfig.mode),
        embedding_model=data.get("embedding_model", DemoConfig.embedding_model),
        embedding_dimension=data.get(
            "embedding_dimension", DemoConfig.embedding_dimension
        ),
        data_dir=data.get("data_dir", DemoConfig.data_dir),
        max_turns=data.get("max_turns", DemoConfig.max_turns),
        max_tokens=data.get("max_tokens", DemoConfig.max_tokens),
        cors_origins=data.get("cors_origins", ["http://localhost:3000"]),
        repos=repos,
    )

    if os.environ.get("CODEMINER_DEMO_MODEL"):
        cfg.model = os.environ["CODEMINER_DEMO_MODEL"]
    if os.environ.get("CODEMINER_DEMO_DATA_DIR"):
        cfg.data_dir = os.environ["CODEMINER_DEMO_DATA_DIR"]

    return cfg
