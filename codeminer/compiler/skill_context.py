# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Skill-aware context construction.

The agent eval scripts used to chunk repos and instantiate ``BM25CodeIndexer``
/ ``CodeVectorStore`` directly. That made the ``agent/`` module the second
home of index lifecycle logic, parallel to ``compiler/``. This module pulls
that responsibility back into ``compiler/``.

Public surface
--------------

- :func:`required_index_types` — given a set of skill ids, return the union
  of ``index_type`` strings declared in their ``config.yaml``. No I/O. Useful
  for query-time skill-set selection (see RFC #133) and for tests.

- :func:`build_skill_contexts` — given a repo path and skill ids, build
  (or reuse) the union of indexes those skills need, then package the
  loaded artifacts into the context dict shape that :class:`SkillLoader`
  consumes (``{"retrieve": RetrieveContext, "expand": ExpandContext, ...}``).
  This is what eval scripts and the agent runtime should call instead of
  rolling their own ``BM25CodeIndexer(chunks=...)`` constructions.

Layering: ``compiler/`` owns build, freshness, load, and the
skill→index resolution. ``agent/`` is a pure consumer.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from ..agent.skills.core import SkillType
from ..agent.skills.registry import SkillRegistry
from .index_builders import IndexBuilderRegistry, register_default_builders
from .index_compiler import IndexCompiler, IndexCompilerConfig
from .manifest import RepoManifest

logger = logging.getLogger(__name__)


_DEFAULT_CACHE_DIR_NAME = ".codeminer_cache"


def required_index_types(
    skill_ids: Iterable[str],
    *,
    skill_registry: Optional[SkillRegistry] = None,
) -> Set[str]:
    """Union of ``index_type`` strings required by the given skills.

    Reads ``index_requirements`` from each skill's ``SkillMetadata`` (loaded
    by ``SkillLoader`` from ``config.yaml``). Skills not present in the
    registry are ignored — callers that need to fail loudly should check
    membership themselves.
    """
    registry = skill_registry or SkillRegistry()
    needed: Set[str] = set()
    for sid in skill_ids:
        meta = registry.get(sid)
        if meta is None:
            continue
        for req in meta.index_requirements or []:
            needed.add(req.index_type)
    return needed


def _skill_types_for(
    skill_ids: Iterable[str],
    skill_registry: SkillRegistry,
) -> Set[SkillType]:
    types: Set[SkillType] = set()
    for sid in skill_ids:
        meta = skill_registry.get(sid)
        if meta is not None:
            types.add(meta.skill_type)
    return types


def _index_dir(cache_dir: str, index_type: str) -> str:
    return os.path.join(cache_dir, index_type)


def _missing_index_types(
    needed: Set[str], cache_dir: str, *, rebuild: bool
) -> List[str]:
    """Index types that need (re)building before we can load artifacts."""
    if rebuild:
        return sorted(needed)
    return sorted(t for t in needed if not _looks_built(cache_dir, t))


def _looks_built(cache_dir: str, index_type: str) -> bool:
    """A loose check that an index dir on disk has *something* in it.

    Each builder writes its own artifact layout; this only filters the
    obvious "directory missing or empty" case so we don't have to call the
    full ``ResourceResolver`` for the v1 of this module. Builders are
    idempotent — a false negative here just triggers a rebuild.
    """
    p = Path(_index_dir(cache_dir, index_type))
    if not p.is_dir():
        return False
    return any(p.iterdir())


def _load_bm25(index_path: str):
    """Load a BM25 index from an arbitrary directory path.

    ``index_path`` is the directory passed to ``BM25CodeIndexer.save_index``
    — typically ``<cache_dir>/bm25`` for ``build_skill_contexts``, or a
    manifest-supplied path for ``load_contexts_from_manifest``.
    """
    from ..index.sparse_idx.bm25_index import BM25CodeIndexer

    indexer = BM25CodeIndexer()
    indexer.load_index(index_path)
    return indexer


def _load_vector(
    index_path: str,
    *,
    embedding_model: str,
    embedding_dimension: int,
    trust_remote_code: bool = False,
    default_batch_size: Optional[int] = None,
):
    """Load a FAISS vector store from an arbitrary directory path."""
    from ..index.embedding.vector_store import CodeVectorStore

    kwargs = {}
    if default_batch_size is not None:
        kwargs["default_batch_size"] = default_batch_size

    store = CodeVectorStore(
        embedding_model=embedding_model,
        embedding_provider="huggingface",
        dimension=embedding_dimension,
        trust_remote_code=trust_remote_code,
        **kwargs,
    )
    store.load(index_path)
    return store


def _load_symbol_graph(index_path: str):
    """Load a symbol graph from ``<index_path>/graph.pkl``."""
    from ..graph.code_graph import CodeGraph

    graph_path = os.path.join(index_path, "graph.pkl")
    return CodeGraph.load_graph(graph_path)


def _run_compiler(
    repo_path: str,
    index_types: Sequence[str],
    cache_dir: str,
    *,
    languages: Sequence[str],
    builder_registry: IndexBuilderRegistry,
) -> None:
    if not index_types:
        return
    cfg = IndexCompilerConfig(
        cache_dir_name=Path(cache_dir).name,
        index_types=list(index_types),
        languages=list(languages),
    )
    compiler = IndexCompiler(builder_registry, cfg)
    compiler.compile_repo(
        repo_path,
        index_types=list(index_types),
        cache_dir=cache_dir,
    )


def build_skill_contexts(
    repo_path: str,
    skill_ids: Iterable[str],
    *,
    languages: Sequence[str] = ("python",),
    cache_dir: Optional[str] = None,
    skill_registry: Optional[SkillRegistry] = None,
    builder_registry: Optional[IndexBuilderRegistry] = None,
    embedding_model: str = "nomic-ai/CodeRankEmbed",
    embedding_dimension: int = 768,
    default_top_k: int = 10,
    default_level: str = "l2",
    rebuild: bool = False,
    trust_remote_code: bool = False,
    embedding_batch_size: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the union of indexes required by *skill_ids* and package them.

    Returns the ``contexts`` dict accepted by ``SkillLoader.load_all`` /
    ``SkillLoader.load_skill``: ``{"retrieve": RetrieveContext, "expand":
    ExpandContext, ...}``. Keys are only present when at least one skill of
    that type was requested AND its index dependencies could be loaded.

    Build behaviour:
      - Already-populated index directories under ``cache_dir/<index_type>``
        are loaded, not rebuilt.
      - Missing types trigger ``IndexCompiler.compile_repo`` for that subset.
      - ``rebuild=True`` forces a fresh build of every requested type.

    The function does no agent-side work; it returns context objects ready
    to hand to ``SkillLoader``. If a requested skill has no index
    requirements (e.g. ``query_transform``), it will not contribute a key
    here — its skill type still gets a context if a *sibling* skill needs
    one (e.g. ``regex_search`` shares the ``retrieve`` key with
    ``bm25_search``).
    """
    skill_ids = list(skill_ids)
    skill_registry = skill_registry or SkillRegistry()

    cache_dir = cache_dir or os.path.join(
        os.path.abspath(repo_path), _DEFAULT_CACHE_DIR_NAME
    )
    os.makedirs(cache_dir, exist_ok=True)

    needed = required_index_types(skill_ids, skill_registry=skill_registry)
    skill_types = _skill_types_for(skill_ids, skill_registry)

    if needed:
        missing = _missing_index_types(needed, cache_dir, rebuild=rebuild)
        if missing:
            registry = builder_registry
            if registry is None:
                registry = IndexBuilderRegistry()
                register_default_builders(
                    registry,
                    languages=list(languages),
                    embedding_model=embedding_model,
                    embedding_dimension=embedding_dimension,
                    trust_remote_code=trust_remote_code,
                )
            logger.info("Building missing indexes %s under %s", missing, cache_dir)
            _run_compiler(
                repo_path,
                missing,
                cache_dir,
                languages=languages,
                builder_registry=registry,
            )

    # Load artifacts for the union of types.
    loaded: Dict[str, Any] = {}
    if "bm25" in needed:
        loaded["bm25"] = _load_bm25(_index_dir(cache_dir, "bm25"))
    if "vector" in needed:
        loaded["vector"] = _load_vector(
            _index_dir(cache_dir, "vector"),
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
            trust_remote_code=trust_remote_code,
            default_batch_size=embedding_batch_size,
        )
    if "symbol_graph" in needed:
        loaded["symbol_graph"] = _load_symbol_graph(
            _index_dir(cache_dir, "symbol_graph")
        )

    return _package_contexts(
        loaded,
        skill_types=skill_types,
        default_top_k=default_top_k,
        default_level=default_level,
    )


def load_contexts_from_manifest(
    manifest: RepoManifest,
    *,
    skill_ids: Iterable[str],
    skill_registry: Optional[SkillRegistry] = None,
    default_top_k: int = 10,
    default_level: str = "l2",
) -> Dict[str, Any]:
    """Load index artifacts directly from a pre-built ``RepoManifest``.

    The AoT counterpart to :func:`build_skill_contexts`: instead of
    deciding what to build from ``skill_ids`` and then building, this
    function consumes a manifest written by a previous
    :class:`~codeminer.compiler.index_compiler.IndexCompiler.compile_repo`
    run, validates that the indexes the requested ``skill_ids`` need
    exist in it, and packages them into the same context dict shape that
    :class:`~codeminer.agent.skills.loader.SkillLoader` consumes.

    Source of truth for paths and configs is the manifest itself —
    ``manifest.indexes[t].path`` is loaded directly, ignoring any
    conventional ``<cache_dir>/<t>`` layout. The vector index's embedding
    model/dimension are read from ``manifest.indexes["vector"].config``
    when present.

    Raises:
        ValueError: if any ``skill_ids`` requires an ``index_type`` that
            isn't present in the manifest or whose ``status != "fresh"``.
            Loud failure here beats the deferred "Skill not available"
            that would otherwise surface at tool-call time.
    """
    skill_ids = list(skill_ids)
    registry = skill_registry or SkillRegistry()

    # Map skill_id → list of required index_types, with up-front validation.
    needed: Set[str] = set()
    for sid in skill_ids:
        meta = registry.get(sid)
        if meta is None:
            continue
        for req in meta.index_requirements or []:
            entry = manifest.indexes.get(req.index_type)
            if entry is None or entry.status != "fresh":
                raise ValueError(
                    f"Manifest is missing required index {req.index_type!r} "
                    f"for skill {sid!r}. Rebuild the manifest with this "
                    f"index_type, or drop {sid!r} from allowed_skills."
                )
            needed.add(req.index_type)

    skill_types = _skill_types_for(skill_ids, registry)

    loaded: Dict[str, Any] = {}
    if "bm25" in needed:
        loaded["bm25"] = _load_bm25(manifest.indexes["bm25"].path)
    if "vector" in needed:
        vec_entry = manifest.indexes["vector"]
        # Prefer the embedding config that was used at build time so the
        # load uses a compatible tokenizer/dimension.
        emb_model = vec_entry.config.get("embedding_model", "nomic-ai/CodeRankEmbed")
        emb_dim = vec_entry.config.get("embedding_dimension", 768)
        loaded["vector"] = _load_vector(
            vec_entry.path,
            embedding_model=emb_model,
            embedding_dimension=emb_dim,
        )
    if "symbol_graph" in needed:
        loaded["symbol_graph"] = _load_symbol_graph(
            manifest.indexes["symbol_graph"].path
        )

    return _package_contexts(
        loaded,
        skill_types=skill_types,
        default_top_k=default_top_k,
        default_level=default_level,
    )


def _package_contexts(
    loaded: Dict[str, Any],
    *,
    skill_types: Set[SkillType],
    default_top_k: int,
    default_level: str,
) -> Dict[str, Any]:
    """Wrap loaded index artifacts into the per-context-type dataclasses.

    Mirrors ``SkillLoader._CONTEXT_KEY_FOR_TYPE``: retrieval/aggregate skills
    share the ``retrieve`` key, expand skills share the ``expand`` key, etc.

    A ``retrieve``/``expand`` context is packaged when *either* a skill of the
    corresponding type was requested *or* the matching index artifacts were
    loaded. The second clause covers ``custom`` composers (e.g.
    ``codeminer_context``) that declare ``bm25``/``vector``/``symbol_graph``
    index_requirements but aren't a RETRIEVAL/EXPAND skill type — without it
    their loaded indexes would be silently dropped and the composer would
    receive ``retrieve=None``/``expand=None`` and return nothing.
    """
    contexts: Dict[str, Any] = {}

    needs_retrieve = bool(skill_types & {SkillType.RETRIEVAL, SkillType.AGGREGATE})
    has_retrieve_index = ("bm25" in loaded) or ("vector" in loaded)
    if needs_retrieve or has_retrieve_index:
        from ..ops.retrieve import RetrieveContext

        contexts["retrieve"] = RetrieveContext(
            bm25=loaded.get("bm25"),
            vector_store=loaded.get("vector"),
            regex_index=None,
            default_top_k=default_top_k,
            default_level=default_level,
        )

    if SkillType.EXPAND in skill_types or "symbol_graph" in loaded:
        from ..ops.expand import ExpandContext

        contexts["expand"] = ExpandContext(code_graph=loaded.get("symbol_graph"))

    return contexts
