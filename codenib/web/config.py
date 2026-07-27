# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Configuration + repo registry types for the code-QA demo.

The demo answers questions about a fixed set of repositories drawn from the
**codenib-base-dataset** (each instance = a repo pinned to a ``base_commit``).
``scripts/build_qa_index.py`` selects instances, checks out each repo at its
commit, builds CodeNib indexes, and writes a ``qa_registry.json`` describing
what was indexed. The server (``codenib.web.app``) reads that registry.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..llm.options import (
    merge_model_options,
    parse_model_options_json,
    validate_model_options,
)
from ..paths import QA_DATA_DIRNAME, REPO_INDEX_DIRNAME

DEFAULT_CONFIG_PATH = "qa_config.yaml"
CACHE_DIR_NAME = REPO_INDEX_DIRNAME
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

    # LiteLLM model string for the interactive Ask agent.
    model: str = "gpt-4o"
    # Wiki prose and edge labels may use a separate provider/model. When unset,
    # they use ``model``.
    wiki_model: Optional[str] = None
    wiki_api_base: Optional[str] = None
    wiki_api_key: Optional[str] = field(default=None, repr=False)
    # Optional OpenAI-compatible endpoint for the Ask agent. Provider-native
    # models (for example Vertex or Anthropic) normally leave these unset.
    model_api_base: Optional[str] = None
    model_api_key: Optional[str] = field(default=None, repr=False)
    # Provider-specific LiteLLM completion options. Core fields such as model,
    # endpoint, credentials, token budget, and tools remain managed separately.
    model_options: Dict[str, Any] = field(default_factory=dict)
    # Wiki generation inherits ``model_options`` and may override individual
    # values (including nested ``extra_body`` fields).
    wiki_model_options: Dict[str, Any] = field(default_factory=dict)
    # "sparse" (BM25 only) or "hybrid" (BM25 + vector embeddings).
    mode: str = "sparse"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dimension: int = 384
    # Embeddings can run in-process or through an OpenAI-compatible endpoint.
    embedding_provider: str = "huggingface"
    embedding_base_url: Optional[str] = None
    embedding_api_key: Optional[str] = field(default=None, repr=False)
    # Where checked-out repos + indexes + the registry live.
    data_dir: str = QA_DATA_DIRNAME
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
    # Show short LLM-written dependency phrases on graph edges (hover/click).
    # Off by default: each first-seen edge costs one small LLM call (then cached).
    edge_labels: bool = False
    # Optional cheaper model for the (very short) edge-label calls. None ->
    # falls back to ``model``. Env override: CODENIB_EDGE_MODEL.
    edge_label_model: Optional[str] = None

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

    def __post_init__(self) -> None:
        self.model_options = validate_model_options(
            self.model_options,
            source="model_options",
        )
        self.wiki_model_options = validate_model_options(
            self.wiki_model_options,
            source="wiki_model_options",
        )

    def index_types(self) -> List[str]:
        return ["bm25", "vector"] if self.mode == "hybrid" else ["bm25"]

    @property
    def registry_path(self) -> str:
        return os.path.join(os.path.abspath(self.data_dir), REGISTRY_FILENAME)

    def repo_dir(self, instance_id: str) -> str:
        return os.path.join(os.path.abspath(self.data_dir), "repos", instance_id)

    @property
    def wiki_generation_model(self) -> str:
        """Model used by wiki and edge-label generation."""
        return self.wiki_model or self.model

    @property
    def wiki_generation_api_base(self) -> Optional[str]:
        """Endpoint used by wiki generation, falling back to the Ask endpoint."""

        return self.wiki_api_base or self.model_api_base

    @property
    def wiki_generation_api_key(self) -> Optional[str]:
        """Credential used by wiki generation, falling back to the Ask key."""

        return self.wiki_api_key or self.model_api_key

    @property
    def wiki_generation_options(self) -> Dict[str, Any]:
        """Provider options for Wiki calls, layered over the Ask defaults."""

        return merge_model_options(self.model_options, self.wiki_model_options)


def load_config(path: Optional[str] = None) -> QAConfig:
    """Load demo config from YAML, applying env overrides."""
    cfg_path = path or os.environ.get("CODENIB_DEMO_CONFIG", DEFAULT_CONFIG_PATH)
    data = {}
    if Path(cfg_path).exists():
        with open(cfg_path) as f:
            data = yaml.safe_load(f) or {}

    defaults = QAConfig()
    cfg = QAConfig(
        model=data.get("model", defaults.model),
        wiki_model=data.get("wiki_model"),
        wiki_api_base=data.get("wiki_api_base"),
        wiki_api_key=data.get("wiki_api_key"),
        model_api_base=data.get("model_api_base"),
        model_api_key=data.get("model_api_key"),
        model_options=validate_model_options(
            data.get("model_options"),
            source="model_options",
        ),
        wiki_model_options=validate_model_options(
            data.get("wiki_model_options"),
            source="wiki_model_options",
        ),
        mode=data.get("mode", defaults.mode),
        embedding_model=data.get("embedding_model", defaults.embedding_model),
        embedding_dimension=data.get(
            "embedding_dimension", defaults.embedding_dimension
        ),
        embedding_provider=data.get("embedding_provider", defaults.embedding_provider),
        embedding_base_url=data.get("embedding_base_url"),
        embedding_api_key=data.get("embedding_api_key"),
        data_dir=data.get("data_dir", defaults.data_dir),
        prebuilt_dir=data.get("prebuilt_dir", defaults.prebuilt_dir),
        max_turns=data.get("max_turns", defaults.max_turns),
        max_tokens=data.get("max_tokens", defaults.max_tokens),
        wiki_agent=data.get("wiki_agent", defaults.wiki_agent),
        cors_origins=data.get(
            "cors_origins",
            defaults.cors_origins,
        ),
        dataset=data.get("dataset", defaults.dataset),
        split=data.get("split", defaults.split),
        instances=data.get("instances", []),
        languages=data.get(
            "languages",
            defaults.languages,
        ),
        per_language=data.get("per_language", defaults.per_language),
        edge_labels=data.get("edge_labels", defaults.edge_labels),
        edge_label_model=data.get("edge_label_model", defaults.edge_label_model),
        rerank_strategy=data.get("rerank_strategy", defaults.rerank_strategy),
        crossencoder_model=data.get("crossencoder_model", defaults.crossencoder_model),
        crossencoder_batch_size=data.get(
            "crossencoder_batch_size", defaults.crossencoder_batch_size
        ),
    )

    if os.environ.get("CODENIB_DEMO_MODEL"):
        cfg.model = os.environ["CODENIB_DEMO_MODEL"]
    if os.environ.get("CODENIB_DEMO_WIKI_MODEL"):
        cfg.wiki_model = os.environ["CODENIB_DEMO_WIKI_MODEL"]
    if os.environ.get("CODENIB_DEMO_WIKI_API_BASE"):
        cfg.wiki_api_base = os.environ["CODENIB_DEMO_WIKI_API_BASE"]
    if os.environ.get("CODENIB_DEMO_WIKI_API_KEY"):
        cfg.wiki_api_key = os.environ["CODENIB_DEMO_WIKI_API_KEY"]
    if os.environ.get("CODENIB_DEMO_API_BASE"):
        cfg.model_api_base = os.environ["CODENIB_DEMO_API_BASE"]
    if os.environ.get("CODENIB_DEMO_API_KEY"):
        cfg.model_api_key = os.environ["CODENIB_DEMO_API_KEY"]
    if os.environ.get("CODENIB_DEMO_MODEL_OPTIONS"):
        cfg.model_options = merge_model_options(
            cfg.model_options,
            parse_model_options_json(
                os.environ["CODENIB_DEMO_MODEL_OPTIONS"],
                source="CODENIB_DEMO_MODEL_OPTIONS",
            ),
        )
    if os.environ.get("CODENIB_DEMO_WIKI_MODEL_OPTIONS"):
        cfg.wiki_model_options = merge_model_options(
            cfg.wiki_model_options,
            parse_model_options_json(
                os.environ["CODENIB_DEMO_WIKI_MODEL_OPTIONS"],
                source="CODENIB_DEMO_WIKI_MODEL_OPTIONS",
            ),
        )
    if os.environ.get("CODENIB_DEMO_DATA_DIR"):
        cfg.data_dir = os.environ["CODENIB_DEMO_DATA_DIR"]
    if os.environ.get("CODENIB_DEMO_PREBUILT_DIR"):
        cfg.prebuilt_dir = os.environ["CODENIB_DEMO_PREBUILT_DIR"]
    _env_edge = os.environ.get("CODENIB_EDGE_LABELS")
    if _env_edge is not None:
        cfg.edge_labels = _env_edge.strip().lower() in ("1", "true", "yes", "on")
    if os.environ.get("CODENIB_EDGE_MODEL"):
        cfg.edge_label_model = os.environ["CODENIB_EDGE_MODEL"]

    if os.environ.get("CODENIB_EMBEDDING_PROVIDER"):
        cfg.embedding_provider = os.environ["CODENIB_EMBEDDING_PROVIDER"]
    if os.environ.get("CODENIB_EMBEDDING_MODEL"):
        cfg.embedding_model = os.environ["CODENIB_EMBEDDING_MODEL"]
    if os.environ.get("CODENIB_EMBEDDING_BASE_URL"):
        cfg.embedding_base_url = os.environ["CODENIB_EMBEDDING_BASE_URL"]
    if os.environ.get("CODENIB_EMBEDDING_API_KEY"):
        cfg.embedding_api_key = os.environ["CODENIB_EMBEDDING_API_KEY"]

    cfg.embedding_provider = cfg.embedding_provider.strip().lower()
    if cfg.embedding_provider not in {"huggingface", "openai"}:
        raise ValueError("embedding_provider must be either 'huggingface' or 'openai'")

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
