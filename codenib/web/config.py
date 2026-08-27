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
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..dataset_ids import CODENIB_BASE_DATASET
from ..llm.options import (
    merge_model_options,
    parse_model_options_json,
    validate_model_options,
)
from ..paths import QA_DATA_DIRNAME, REPO_INDEX_DIRNAME
from ..provider_routes import normalize_provider

DEFAULT_CONFIG_PATH = "qa_config.yaml"
CACHE_DIR_NAME = REPO_INDEX_DIRNAME
REGISTRY_FILENAME = "qa_registry.json"
CONFIG_EXTENDS_KEY = "extends"
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_ENV_VALUES = frozenset({"", "0", "false", "no", "off"})
_LOCAL_INDEX_REPOSITORY_RE = re.compile(
    r"[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*\Z",
    re.ASCII,
)
_LOCAL_INDEX_MAX_REPOSITORIES = 4_096
_LOCAL_INDEX_MAX_DELAY_MS = 86_400_000
_LOCAL_INDEX_MAX_LEASE_MS = 2_147_483_647
_LOCAL_INDEX_MAX_SCAN_LIMIT = 256
# Keep the read-only default config lightweight; storage models are imported
# only after this explicit opt-in is present.
_DEFAULT_LOCAL_INDEX_NAMESPACE = "default"


def _exact_config_text(value: Any, *, source: str, max_length: int) -> str:
    if type(value) is not str:
        raise TypeError(f"{source} must be exact text")
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or len(normalized) > max_length
        or "\x00" in normalized
    ):
        raise ValueError(f"{source} is invalid")
    return normalized


def _exact_config_integer(
    value: Any,
    *,
    source: str,
    minimum: int = 1,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(
            f"{source} must be an exact integer between {minimum} and {maximum}"
        )
    return value


def _absolute_config_path(value: Any, *, source: str) -> Path:
    text = _exact_config_text(value, source=source, max_length=32_768)
    path = Path(text)
    if not path.is_absolute() or path == path.parent or str(path) != text:
        raise ValueError(f"{source} must be a canonical absolute non-root path")
    return path


def _config_paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _config_mapping(value: Any, *, source: str) -> Dict[str, Any]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise TypeError(f"{source} must be a mapping with exact text keys")
    return value


def _reject_unknown_config_keys(
    value: Dict[str, Any],
    *,
    source: str,
    allowed: frozenset[str],
) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{source} contains unsupported key {sorted(unknown)[0]!r}")


@dataclass(frozen=True, slots=True)
class LocalIndexStorageRepository:
    """One explicit Web repository to durable storage ref binding."""

    repo_id: str
    repository_key: str
    namespace_name: str = _DEFAULT_LOCAL_INDEX_NAMESPACE
    ref_name: str = "main"
    _repository_id: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        from ..compiler.snapshot_store import normalize_repo
        from ..storage.models import NamespaceIdentity, RepositoryIdentity

        if type(self) is not LocalIndexStorageRepository:
            raise TypeError("local index repository binding must use the exact type")
        repo_id = _exact_config_text(
            self.repo_id,
            source="index_storage repository ID",
            max_length=512,
        )
        repository_key = _exact_config_text(
            self.repository_key,
            source="index_storage repository key",
            max_length=32_768,
        )
        if _LOCAL_INDEX_REPOSITORY_RE.fullmatch(repository_key) is None:
            raise ValueError("index_storage repository key is not canonical")
        try:
            normalized_repository_key = normalize_repo(repository_key)
        except ValueError as exc:
            raise ValueError("index_storage repository key is not canonical") from exc
        if normalized_repository_key != repository_key:
            raise ValueError("index_storage repository key is not canonical")
        namespace_name = _exact_config_text(
            self.namespace_name,
            source="index_storage namespace",
            max_length=32_768,
        )
        namespace = NamespaceIdentity(namespace_name)
        if namespace.name != namespace_name:
            raise ValueError("index_storage namespace is not canonical")
        ref_name = _exact_config_text(
            self.ref_name,
            source="index_storage ref name",
            max_length=512,
        )
        repository = RepositoryIdentity(
            namespace_id=namespace.namespace_id,
            repository_key=repository_key,
        )
        if repository.repository_key != repository_key:
            raise ValueError("index_storage repository key is not canonical")
        object.__setattr__(self, "repo_id", repo_id)
        object.__setattr__(self, "repository_key", repository_key)
        object.__setattr__(self, "namespace_name", namespace.name)
        object.__setattr__(self, "ref_name", ref_name)
        object.__setattr__(self, "_repository_id", repository.repository_id)

    @property
    def repository_id(self) -> str:
        return self._repository_id


@dataclass(frozen=True, slots=True)
class LocalIndexWorkerConfig:
    """Bounded worker and scheduler settings for the local Web service."""

    lease_duration_ms: int = 30_000
    heartbeat_interval_ms: int = 5_000
    scan_limit: int = 64
    initial_idle_delay_ms: int = 250
    max_idle_delay_ms: int = 5_000
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if type(self) is not LocalIndexWorkerConfig:
            raise TypeError("local index worker config must use the exact type")
        lease = _exact_config_integer(
            self.lease_duration_ms,
            source="index_storage worker lease_duration_ms",
            maximum=_LOCAL_INDEX_MAX_LEASE_MS,
        )
        heartbeat = _exact_config_integer(
            self.heartbeat_interval_ms,
            source="index_storage worker heartbeat_interval_ms",
            maximum=_LOCAL_INDEX_MAX_LEASE_MS,
        )
        if heartbeat * 3 >= lease:
            raise ValueError(
                "index_storage worker heartbeat interval must be less than one third "
                "of its lease"
            )
        _exact_config_integer(
            self.scan_limit,
            source="index_storage worker scan_limit",
            maximum=_LOCAL_INDEX_MAX_SCAN_LIMIT,
        )
        initial = _exact_config_integer(
            self.initial_idle_delay_ms,
            source="index_storage worker initial_idle_delay_ms",
            maximum=_LOCAL_INDEX_MAX_DELAY_MS,
        )
        maximum = _exact_config_integer(
            self.max_idle_delay_ms,
            source="index_storage worker max_idle_delay_ms",
            maximum=_LOCAL_INDEX_MAX_DELAY_MS,
        )
        if maximum < initial:
            raise ValueError(
                "index_storage worker maximum idle delay cannot be below its initial "
                "delay"
            )
        _exact_config_integer(
            self.max_attempts,
            source="index_storage worker max_attempts",
            maximum=1_000,
        )


@dataclass(frozen=True, slots=True)
class LocalIndexRuntimeConfig:
    """Bounded current-result polling settings for the local Web service."""

    poll_interval_ms: int = 1_000

    def __post_init__(self) -> None:
        if type(self) is not LocalIndexRuntimeConfig:
            raise TypeError("local index runtime config must use the exact type")
        _exact_config_integer(
            self.poll_interval_ms,
            source="index_storage runtime poll_interval_ms",
            maximum=_LOCAL_INDEX_MAX_DELAY_MS,
        )


@dataclass(frozen=True, slots=True)
class LocalIndexStorageConfig:
    """Explicit existing local durable storage selection for the Web service."""

    catalog_path: Path
    cas_root: Path
    worker_workspace_root: Path
    runtime_workspace_root: Path
    repositories: tuple[LocalIndexStorageRepository, ...]
    namespace_name: str = _DEFAULT_LOCAL_INDEX_NAMESPACE
    catalog_busy_timeout_ms: int = 5_000
    worker: LocalIndexWorkerConfig = field(default_factory=LocalIndexWorkerConfig)
    runtime: LocalIndexRuntimeConfig = field(default_factory=LocalIndexRuntimeConfig)

    def __post_init__(self) -> None:
        from ..storage.models import NamespaceIdentity

        if type(self) is not LocalIndexStorageConfig:
            raise TypeError("local index storage config must use the exact type")
        path_values = (
            (self.catalog_path, "catalog_path"),
            (self.cas_root, "cas_root"),
            (self.worker_workspace_root, "worker_workspace_root"),
            (self.runtime_workspace_root, "runtime_workspace_root"),
        )
        for path, name in path_values:
            if type(path) is not type(Path()):
                raise TypeError(f"index_storage {name} must be an exact Path")
            if (
                not path.is_absolute()
                or path == path.parent
                or Path(os.path.abspath(os.fspath(path))) != path
            ):
                raise ValueError(
                    f"index_storage {name} must be a canonical absolute non-root path"
                )
        for index, (first, first_name) in enumerate(path_values):
            for second, second_name in path_values[index + 1 :]:
                if _config_paths_overlap(first, second):
                    raise ValueError(
                        f"index_storage {first_name} must not overlap {second_name}"
                    )
        namespace = NamespaceIdentity(self.namespace_name)
        if namespace.name != self.namespace_name:
            raise ValueError("index_storage namespace is not canonical")
        if (
            type(self.repositories) is not tuple
            or not 1 <= len(self.repositories) <= _LOCAL_INDEX_MAX_REPOSITORIES
        ):
            raise ValueError("index_storage requires 1 to 4096 repository bindings")
        if any(
            type(repository) is not LocalIndexStorageRepository
            for repository in self.repositories
        ):
            raise TypeError("index_storage repository bindings must use exact values")
        by_repo: set[str] = set()
        by_storage: set[tuple[str, str]] = set()
        for repository in self.repositories:
            if repository.namespace_name != namespace.name:
                raise ValueError("index_storage repository namespace differs")
            storage_key = (repository.repository_id, repository.ref_name)
            if repository.repo_id in by_repo or storage_key in by_storage:
                raise ValueError("index_storage repository bindings must be unique")
            by_repo.add(repository.repo_id)
            by_storage.add(storage_key)
        _exact_config_integer(
            self.catalog_busy_timeout_ms,
            source="index_storage catalog_busy_timeout_ms",
            minimum=0,
            maximum=_LOCAL_INDEX_MAX_DELAY_MS,
        )
        if type(self.worker) is not LocalIndexWorkerConfig:
            raise TypeError("index_storage worker config must use the exact type")
        if type(self.runtime) is not LocalIndexRuntimeConfig:
            raise TypeError("index_storage runtime config must use the exact type")


def _parse_local_index_worker(value: Any) -> LocalIndexWorkerConfig:
    data = _config_mapping(value, source="index_storage.worker")
    _reject_unknown_config_keys(
        data,
        source="index_storage.worker",
        allowed=frozenset(
            {
                "lease_duration_ms",
                "heartbeat_interval_ms",
                "scan_limit",
                "initial_idle_delay_ms",
                "max_idle_delay_ms",
                "max_attempts",
            }
        ),
    )
    defaults = LocalIndexWorkerConfig()
    return LocalIndexWorkerConfig(
        lease_duration_ms=data.get("lease_duration_ms", defaults.lease_duration_ms),
        heartbeat_interval_ms=data.get(
            "heartbeat_interval_ms",
            defaults.heartbeat_interval_ms,
        ),
        scan_limit=data.get("scan_limit", defaults.scan_limit),
        initial_idle_delay_ms=data.get(
            "initial_idle_delay_ms",
            defaults.initial_idle_delay_ms,
        ),
        max_idle_delay_ms=data.get(
            "max_idle_delay_ms",
            defaults.max_idle_delay_ms,
        ),
        max_attempts=data.get("max_attempts", defaults.max_attempts),
    )


def _parse_local_index_runtime(value: Any) -> LocalIndexRuntimeConfig:
    data = _config_mapping(value, source="index_storage.runtime")
    _reject_unknown_config_keys(
        data,
        source="index_storage.runtime",
        allowed=frozenset({"poll_interval_ms"}),
    )
    defaults = LocalIndexRuntimeConfig()
    return LocalIndexRuntimeConfig(
        poll_interval_ms=data.get("poll_interval_ms", defaults.poll_interval_ms),
    )


def _parse_local_index_storage(value: Any) -> LocalIndexStorageConfig | None:
    if value is None:
        return None
    data = _config_mapping(value, source="index_storage")
    _reject_unknown_config_keys(
        data,
        source="index_storage",
        allowed=frozenset(
            {
                "catalog_path",
                "cas_root",
                "worker_workspace_root",
                "runtime_workspace_root",
                "namespace",
                "catalog_busy_timeout_ms",
                "repositories",
                "worker",
                "runtime",
            }
        ),
    )
    required = (
        "catalog_path",
        "cas_root",
        "worker_workspace_root",
        "runtime_workspace_root",
        "repositories",
    )
    missing = [name for name in required if name not in data]
    if missing:
        raise ValueError(f"index_storage requires {missing[0]}")
    namespace = _exact_config_text(
        data.get("namespace", _DEFAULT_LOCAL_INDEX_NAMESPACE),
        source="index_storage namespace",
        max_length=32_768,
    )
    raw_repositories = _config_mapping(
        data["repositories"],
        source="index_storage.repositories",
    )
    if not 1 <= len(raw_repositories) <= _LOCAL_INDEX_MAX_REPOSITORIES:
        raise ValueError("index_storage requires 1 to 4096 repository bindings")
    repositories = []
    for repo_id in sorted(raw_repositories):
        repository_data = _config_mapping(
            raw_repositories[repo_id],
            source=f"index_storage.repositories[{repo_id!r}]",
        )
        _reject_unknown_config_keys(
            repository_data,
            source=f"index_storage.repositories[{repo_id!r}]",
            allowed=frozenset({"repository_key", "ref_name"}),
        )
        if "repository_key" not in repository_data:
            raise ValueError(
                f"index_storage repository {repo_id!r} requires repository_key"
            )
        repositories.append(
            LocalIndexStorageRepository(
                repo_id=repo_id,
                repository_key=repository_data["repository_key"],
                namespace_name=namespace,
                ref_name=repository_data.get("ref_name", "main"),
            )
        )
    return LocalIndexStorageConfig(
        catalog_path=_absolute_config_path(
            data["catalog_path"],
            source="index_storage catalog_path",
        ),
        cas_root=_absolute_config_path(
            data["cas_root"],
            source="index_storage cas_root",
        ),
        worker_workspace_root=_absolute_config_path(
            data["worker_workspace_root"],
            source="index_storage worker_workspace_root",
        ),
        runtime_workspace_root=_absolute_config_path(
            data["runtime_workspace_root"],
            source="index_storage runtime_workspace_root",
        ),
        repositories=tuple(repositories),
        namespace_name=namespace,
        catalog_busy_timeout_ms=data.get("catalog_busy_timeout_ms", 5_000),
        worker=_parse_local_index_worker(data.get("worker", {})),
        runtime=_parse_local_index_runtime(data.get("runtime", {})),
    )


def _validated_bool(value: Any, *, source: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{source} must be a boolean")
    return value


def _parse_env_bool(value: str, *, source: str) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_ENV_VALUES:
        return True
    if normalized in _FALSE_ENV_VALUES:
        return False
    raise ValueError(f"{source} must be a boolean")


def _model_provider(model: str) -> Optional[str]:
    """Return the explicit LiteLLM provider prefix, if one is present."""

    provider, separator, _ = str(model or "").strip().partition("/")
    return provider.lower() if separator else None


def _merge_config_data(
    base: Dict[str, Any],
    overlay: Dict[str, Any],
) -> Dict[str, Any]:
    """Return a recursive config merge where the overlay always wins.

    Mappings are merged so a profile can override one nested model option
    without copying the rest of the base profile. Scalars, lists, and explicit
    ``null`` values replace the inherited value.
    """

    merged = dict(base)
    for key, value in overlay.items():
        if key == CONFIG_EXTENDS_KEY:
            continue
        inherited = merged.get(key)
        if isinstance(inherited, dict) and isinstance(value, dict):
            merged[key] = _merge_config_data(inherited, value)
        else:
            merged[key] = value
    return merged


def _config_parents(value: Any, *, source: Path) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parents = [value]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        parents = value
    else:
        raise ValueError(
            f"{source}: {CONFIG_EXTENDS_KEY} must be a path or list of paths"
        )
    if any(not parent.strip() for parent in parents):
        raise ValueError(f"{source}: {CONFIG_EXTENDS_KEY} paths cannot be empty")
    return parents


def _load_config_data(path: Path, *, chain: tuple[Path, ...] = ()) -> Dict[str, Any]:
    """Load one YAML profile and recursively merge its relative parents."""

    resolved = path.expanduser().resolve()
    if resolved in chain:
        cycle = " -> ".join(str(item) for item in (*chain, resolved))
        raise ValueError(f"demo config extends cycle: {cycle}")
    if not resolved.is_file():
        if chain:
            raise FileNotFoundError(f"demo config parent does not exist: {resolved}")
        return {}

    with resolved.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"demo config must contain a YAML mapping: {resolved}")

    merged: Dict[str, Any] = {}
    next_chain = (*chain, resolved)
    for parent in _config_parents(loaded.get(CONFIG_EXTENDS_KEY), source=resolved):
        parent_path = Path(parent).expanduser()
        if not parent_path.is_absolute():
            parent_path = resolved.parent / parent_path
        merged = _merge_config_data(
            merged,
            _load_config_data(parent_path, chain=next_chain),
        )
    return _merge_config_data(merged, loaded)


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
    # Optional OpenAI-compatible image/VLM endpoint for materializing planned
    # Wiki media slots. When unset, pages still expose deterministic slots but
    # no image/video request is made.
    wiki_media_model: Optional[str] = None
    wiki_media_api_base: Optional[str] = None
    wiki_media_api_key: Optional[str] = field(default=None, repr=False)
    wiki_media_options: Dict[str, Any] = field(default_factory=dict)
    # Optional OpenAI-compatible VLM endpoint for extracting structured visual
    # facts from repository-owned images/diagrams before grounding them to code.
    # Disabled by default so local/offline builds keep using deterministic
    # metadata extraction.
    wiki_visual_facts_enabled: bool = False
    wiki_visual_facts_model: Optional[str] = None
    wiki_visual_facts_api_base: Optional[str] = None
    wiki_visual_facts_api_key: Optional[str] = field(default=None, repr=False)
    wiki_visual_facts_options: Dict[str, Any] = field(default_factory=dict)
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
    dataset: str = CODENIB_BASE_DATASET
    split: str = "test"
    # Explicit instance ids to feature; if empty, sample `per_language` from
    # each of `languages` (a varied set).
    instances: List[str] = field(default_factory=list)
    languages: List[str] = field(
        default_factory=lambda: ["python", "javascript", "typescript", "go", "rust"]
    )
    per_language: int = 1
    # Optional existing-only local durable storage. Presence is the opt-in;
    # default demo servers retain their read-only registry behavior. Keep this
    # additive field last so existing positional QAConfig callers remain stable.
    index_storage: Optional[LocalIndexStorageConfig] = None

    def __post_init__(self) -> None:
        if self.index_storage is not None and (
            type(self.index_storage) is not LocalIndexStorageConfig
        ):
            raise TypeError("index_storage must use the exact local config type")
        self.wiki_visual_facts_enabled = _validated_bool(
            self.wiki_visual_facts_enabled,
            source="wiki_visual_facts_enabled",
        )
        self.model_options = validate_model_options(
            self.model_options,
            source="model_options",
        )
        self.wiki_model_options = validate_model_options(
            self.wiki_model_options,
            source="wiki_model_options",
        )
        self.wiki_media_options = validate_model_options(
            self.wiki_media_options,
            source="wiki_media_options",
        )
        self.wiki_visual_facts_options = validate_model_options(
            self.wiki_visual_facts_options,
            source="wiki_visual_facts_options",
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
    def _wiki_shares_ask_backend(self) -> bool:
        """Whether an implicit Ask endpoint is safe for the selected Wiki model."""

        if not self.wiki_model:
            return True
        return _model_provider(self.wiki_model) == _model_provider(self.model)

    @property
    def wiki_generation_api_base(self) -> Optional[str]:
        """Endpoint for wiki generation.

        A Wiki model with the same provider route as Ask may reuse Ask's
        endpoint. A different explicit provider must use ``wiki_api_base`` (or
        its provider-native defaults), otherwise a hosted model such as Vertex
        could be aimed at Ask's local OpenAI-compatible server.
        """

        if self.wiki_api_base:
            return self.wiki_api_base
        if self._wiki_shares_ask_backend:
            return self.model_api_base
        return None

    @property
    def wiki_generation_api_key(self) -> Optional[str]:
        """Credential for wiki generation (see ``wiki_generation_api_base``)."""

        if self.wiki_api_key:
            return self.wiki_api_key
        if self._wiki_shares_ask_backend:
            return self.model_api_key
        return None

    @property
    def wiki_generation_options(self) -> Dict[str, Any]:
        """Provider options for Wiki calls, layered over the Ask defaults.

        Generic knobs (``timeout``, …) carry over; wiki-specific keys win. Keep
        backend-specific knobs out of ``model_options`` — a local vLLM's
        ``chat_template_kwargs`` layered onto a hosted wiki model would be sent
        to a provider that rejects it. ``_no_thinking_kwargs`` injects those
        per-backend knobs by model name, so they need not be configured here.
        """

        return merge_model_options(self.model_options, self.wiki_model_options)

    @property
    def wiki_media_generation_enabled(self) -> bool:
        """Whether Wiki media slots can be materialized through a provider."""

        model = str(self.wiki_media_model or "").strip().lower()
        provider = str((self.wiki_media_options or {}).get("provider") or "").lower()
        if model in {"local/svg", "local-svg"} or provider in {"local", "local-svg"}:
            return True
        return bool(self.wiki_media_model and self.wiki_media_api_base)

    @property
    def wiki_visual_fact_extraction_enabled(self) -> bool:
        """Whether repository media should be sent to a configured VLM."""

        return bool(
            self.wiki_visual_facts_enabled
            and self.wiki_visual_facts_model
            and self.wiki_visual_facts_api_base
        )


def load_config(path: Optional[str] = None) -> QAConfig:
    """Load a layered demo config from YAML, then apply env overrides."""
    cfg_path = path or os.environ.get("CODENIB_DEMO_CONFIG", DEFAULT_CONFIG_PATH)
    data = _load_config_data(Path(cfg_path))

    defaults = QAConfig()
    cfg = QAConfig(
        model=data.get("model", defaults.model),
        wiki_model=data.get("wiki_model"),
        wiki_api_base=data.get("wiki_api_base"),
        wiki_api_key=data.get("wiki_api_key"),
        wiki_media_model=data.get("wiki_media_model"),
        wiki_media_api_base=data.get("wiki_media_api_base"),
        wiki_media_api_key=data.get("wiki_media_api_key"),
        wiki_media_options=validate_model_options(
            data.get("wiki_media_options"),
            source="wiki_media_options",
        ),
        wiki_visual_facts_enabled=_validated_bool(
            data.get(
                "wiki_visual_facts_enabled",
                defaults.wiki_visual_facts_enabled,
            ),
            source="wiki_visual_facts_enabled",
        ),
        wiki_visual_facts_model=data.get("wiki_visual_facts_model"),
        wiki_visual_facts_api_base=data.get("wiki_visual_facts_api_base"),
        wiki_visual_facts_api_key=data.get("wiki_visual_facts_api_key"),
        wiki_visual_facts_options=validate_model_options(
            data.get("wiki_visual_facts_options"),
            source="wiki_visual_facts_options",
        ),
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
        index_storage=_parse_local_index_storage(data.get("index_storage")),
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
    if os.environ.get("CODENIB_WIKI_MEDIA_MODEL"):
        cfg.wiki_media_model = os.environ["CODENIB_WIKI_MEDIA_MODEL"]
    if os.environ.get("CODENIB_WIKI_MEDIA_API_BASE"):
        cfg.wiki_media_api_base = os.environ["CODENIB_WIKI_MEDIA_API_BASE"]
    if os.environ.get("CODENIB_WIKI_MEDIA_API_KEY"):
        cfg.wiki_media_api_key = os.environ["CODENIB_WIKI_MEDIA_API_KEY"]
    if os.environ.get("CODENIB_WIKI_VISUAL_FACTS_ENABLED") is not None:
        cfg.wiki_visual_facts_enabled = _parse_env_bool(
            os.environ["CODENIB_WIKI_VISUAL_FACTS_ENABLED"],
            source="CODENIB_WIKI_VISUAL_FACTS_ENABLED",
        )
    if os.environ.get("CODENIB_WIKI_VISUAL_FACTS_MODEL"):
        cfg.wiki_visual_facts_model = os.environ["CODENIB_WIKI_VISUAL_FACTS_MODEL"]
    if os.environ.get("CODENIB_WIKI_VISUAL_FACTS_API_BASE"):
        cfg.wiki_visual_facts_api_base = os.environ[
            "CODENIB_WIKI_VISUAL_FACTS_API_BASE"
        ]
    if os.environ.get("CODENIB_WIKI_VISUAL_FACTS_API_KEY"):
        cfg.wiki_visual_facts_api_key = os.environ["CODENIB_WIKI_VISUAL_FACTS_API_KEY"]
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
    if os.environ.get("CODENIB_WIKI_MEDIA_OPTIONS"):
        cfg.wiki_media_options = merge_model_options(
            cfg.wiki_media_options,
            parse_model_options_json(
                os.environ["CODENIB_WIKI_MEDIA_OPTIONS"],
                source="CODENIB_WIKI_MEDIA_OPTIONS",
            ),
        )
    if os.environ.get("CODENIB_WIKI_VISUAL_FACTS_OPTIONS"):
        cfg.wiki_visual_facts_options = merge_model_options(
            cfg.wiki_visual_facts_options,
            parse_model_options_json(
                os.environ["CODENIB_WIKI_VISUAL_FACTS_OPTIONS"],
                source="CODENIB_WIKI_VISUAL_FACTS_OPTIONS",
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

    cfg.embedding_provider = normalize_provider(cfg.embedding_provider)
    if cfg.embedding_provider not in {"huggingface", "openai"}:
        raise ValueError("embedding_provider must be huggingface or openai")

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
