# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Configuration + repo registry types for the code-QA demo.

The demo answers questions about a fixed set of repositories drawn from the
**codeminer-base-dataset** (each instance = a repo pinned to a ``base_commit``).
``scripts/build_qa_index.py`` selects instances, checks out each repo at its
commit, builds CodeMiner indexes, and writes a ``qa_registry.json`` describing
what was indexed. The server (``codeminer.web.app``) reads that registry.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

DEFAULT_CONFIG_PATH = "qa_config.yaml"
CACHE_DIR_NAME = ".codeminer_cache"
REGISTRY_FILENAME = "qa_registry.json"


@dataclass(slots=True)
class RepoEntry:
    """One indexed dataset instance (repo @ base_commit)."""

    instance_id: str
    repo: str  # e.g. "django/django"
    base_commit: str
    language: str  # language_group from the dataset
    repo_dir: str  # absolute path to the checked-out source
    manifest_path: str  # absolute path to repo_manifest.json
    problem_statement: str = ""

    @property
    def commit_short(self) -> str:
        return (self.base_commit or "")[:8]


@dataclass(slots=True)
class QAConfig:
    """Top-level demo configuration."""

    # litellm model string for the agent. Env ``CODEMINER_DEMO_MODEL`` wins.
    model: str = "gpt-4o"
    # "sparse" (BM25 only) or "hybrid" (BM25 + vector embeddings).
    mode: str = "sparse"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dimension: int = 384
    # Where checked-out repos + indexes + the registry live.
    data_dir: str = ".codeminer_qa"
    # Optional read-only tree of pre-built per-instance artifacts:
    #   <prebuilt_dir>/<instance_id>/repo/           — source @ base_commit
    #   <prebuilt_dir>/<instance_id>/{l0,l2}/...    — hierarchical vector index
    # When set, build_qa_index reuses the checkout (no clone) and points the
    # manifest's ``vector`` entry at the pre-built files. BM25 is still built
    # locally into ``data_dir`` because the pre-built tree has no BM25.
    prebuilt_dir: Optional[str] = None
    max_turns: int = 8
    max_tokens: int = 1024
    # Use the conceptual agent wiki pipeline (outline + per-page generation)
    # instead of the directory-based WikiBuilder.
    wiki_agent: bool = True

    # --- rerank strategy -------------------------------------------------------
    # "embedding"    — dot-product against pre-indexed vectors (default, no extra GPU)
    # "crossencoder" — neural pair scoring (Qwen3-Reranker or mxbai-rerank-*);
    #                  requires crossencoder_model to be on disk under HF_HOME
    rerank_strategy: str = "embedding"
    crossencoder_model: str = "Qwen/Qwen3-Reranker-0.6B"
    crossencoder_batch_size: int = 8
    cors_origins: List[str] = field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    )

    # --- instance selection (used by the build script) ---
    dataset: str = "fishmingyu/codeminer-base-dataset"
    split: str = "test"
    # Explicit instance ids to feature; if empty, sample `per_language` from
    # each of `languages` (a varied set).
    instances: List[str] = field(default_factory=list)
    languages: List[str] = field(
        default_factory=lambda: ["python", "javascript", "typescript", "go", "rust"]
    )
    per_language: int = 1

    def index_types(self) -> List[str]:
        return ["bm25", "vector"] if self.mode == "hybrid" else ["bm25"]

    @property
    def registry_path(self) -> str:
        return os.path.join(os.path.abspath(self.data_dir), REGISTRY_FILENAME)

    def repo_dir(self, instance_id: str) -> str:
        return os.path.join(os.path.abspath(self.data_dir), "repos", instance_id)


def load_config(path: Optional[str] = None) -> QAConfig:
    """Load demo config from YAML, applying env overrides."""
    cfg_path = path or os.environ.get("CODEMINER_DEMO_CONFIG", DEFAULT_CONFIG_PATH)
    data = {}
    if Path(cfg_path).exists():
        with open(cfg_path) as f:
            data = yaml.safe_load(f) or {}

    cfg = QAConfig(
        model=data.get("model", QAConfig.model),
        mode=data.get("mode", QAConfig.mode),
        embedding_model=data.get("embedding_model", QAConfig.embedding_model),
        embedding_dimension=data.get(
            "embedding_dimension", QAConfig.embedding_dimension
        ),
        data_dir=data.get("data_dir", QAConfig.data_dir),
        prebuilt_dir=data.get("prebuilt_dir", QAConfig.prebuilt_dir),
        max_turns=data.get("max_turns", QAConfig.max_turns),
        max_tokens=data.get("max_tokens", QAConfig.max_tokens),
        cors_origins=data.get(
            "cors_origins",
            ["http://localhost:3000", "http://127.0.0.1:3000"],
        ),
        dataset=data.get("dataset", QAConfig.dataset),
        split=data.get("split", QAConfig.split),
        instances=data.get("instances", []),
        languages=data.get(
            "languages",
            ["python", "javascript", "typescript", "go", "rust"],
        ),
        per_language=data.get("per_language", QAConfig.per_language),
        rerank_strategy=data.get("rerank_strategy", QAConfig.rerank_strategy),
        crossencoder_model=data.get("crossencoder_model", QAConfig.crossencoder_model),
        crossencoder_batch_size=data.get("crossencoder_batch_size", QAConfig.crossencoder_batch_size),
    )

    if os.environ.get("CODEMINER_DEMO_MODEL"):
        cfg.model = os.environ["CODEMINER_DEMO_MODEL"]
    if os.environ.get("CODEMINER_DEMO_DATA_DIR"):
        cfg.data_dir = os.environ["CODEMINER_DEMO_DATA_DIR"]
    if os.environ.get("CODEMINER_DEMO_PREBUILT_DIR"):
        cfg.prebuilt_dir = os.environ["CODEMINER_DEMO_PREBUILT_DIR"]

    return cfg


def save_registry(path: str, entries: List[RepoEntry]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump([asdict(e) for e in entries], f, indent=2)


def load_registry(path: str) -> List[RepoEntry]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        rows = json.load(f)
    return [RepoEntry(**{k: r[k] for k in r if k in RepoEntry.__slots__}) for r in rows]
