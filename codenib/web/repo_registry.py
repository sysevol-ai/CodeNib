# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Load pre-indexed dataset repos and expose one agent per repo.

Each repo gets its **own** ``SkillRegistry`` instance (not the global
singleton) so that skill executors stay bound to that repo's indexes. This
gives clean per-repo isolation and lets us build a ready-to-run ``AgentRunner``
once at startup and reuse it for every request — retrieval is read-only, so
concurrent queries are safe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib.util import find_spec
from threading import Lock
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from ..compiler.manifest import RepoManifest
from ..log_utils import get_logger
from ..repository_summary import read_repository_summary
from .config import QAConfig, RepoEntry, load_registry
from .schemas import GraphCoverage, RepoInfo

if TYPE_CHECKING:
    from ..agent.runner import AgentRunner
    from ..graph.code_graph import CodeGraph
    from ..index.embedding.vector_store import CodeVectorStore
    from ..index.sparse_idx.bm25_index import BM25CodeIndexer
    from ..llm.litellm_chat import LiteLLMChat

logger = get_logger(__name__)

_SKILLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "agent",
    "skills",
)

# Demo agent prompt: steer the model to search the indexes and ground its
# answer in retrieved code (so the answer carries citations the code pane uses).
_DEMO_SYSTEM_PROMPT = (
    "You answer questions about a code repository for a documentation explorer. "
    "Use the search tools (hybrid_search / embedding_search / bm25_search) to "
    "find the relevant code, then write a clear, well-structured explanation. "
    "Ground every claim in the retrieved code and name the key files and symbols "
    "you found so the reader can open them. Follow identifiers discovered in a "
    "call site with a targeted search when the question asks for a definition, "
    "owner, implementation, or control flow; do not claim that a symbol is "
    "missing while an unresolved candidate identifier remains in the evidence. "
    "When a call site names several candidates, distinguish registries and "
    "configuration objects from the component whose method performs the "
    "requested operation, then resolve that component's definition. "
    "Answer definition questions with the exact identifier and defining file, "
    "and use call sites only to explain how it is reached. If a search returns "
    "nothing useful, try a different query or tool before concluding."
)


def _fresh_registry():
    """Create an isolated registry that bypasses the global singleton."""
    from ..agent.skills.registry import SkillRegistry

    reg = object.__new__(SkillRegistry)
    reg._skills = {}
    return reg


def _vector_store_type():
    from ..index.embedding.vector_store import CodeVectorStore

    return CodeVectorStore


def _ask_llm_type():
    from ..llm.litellm_chat import LiteLLMChat

    return LiteLLMChat


@dataclass
class RepoBundle:
    """Everything needed to answer questions about one repo."""

    entry: RepoEntry
    manifest: RepoManifest
    runner: Optional["AgentRunner"] = None
    # Read-only handles reused by the wiki builder (index-derived docs).
    vector_store: Optional["CodeVectorStore"] = None
    bm25: Optional["BM25CodeIndexer"] = None
    chat_available: bool = False
    view_loader: Optional[Callable[["RepoBundle"], None]] = None
    runtime_loader: Optional[Callable[["RepoBundle"], None]] = None

    def __post_init__(self) -> None:
        self._views_lock = Lock()
        self._runtime_lock = Lock()
        self._views_loaded = self.view_loader is None
        self._runtime_loaded = self.runner is not None

    def ensure_views(self) -> None:
        """Load retrieval views without constructing the interactive agent."""
        if self._views_loaded:
            return
        with self._views_lock:
            if self._views_loaded:
                return
            if self.view_loader is None:
                raise RuntimeError("repo view loader is not configured")
            self.view_loader(self)
            self._views_loaded = True

    def ensure_runtime(self) -> None:
        """Load retrieval views and the QA runner on first chat request."""
        self.ensure_views()
        if self._runtime_loaded:
            return
        with self._runtime_lock:
            if self._runtime_loaded:
                return
            if self.runtime_loader is None:
                raise RuntimeError("repo runtime loader is not configured")
            self.runtime_loader(self)
            self._runtime_loaded = True

    def info(self) -> RepoInfo:
        capabilities = dict(self.manifest.capabilities)
        # The prebuilt indexes ship a symbol graph that the manifest doesn't
        # declare; surface a "codemap" capability so the UI can offer the mode.
        capabilities["codemap"] = (
            self._graph_path() is not None and find_spec("igraph") is not None
        )
        capabilities["chat"] = self.chat_available
        display_name = self.entry.repo
        if self.entry.commit_short:
            display_name += f" @ {self.entry.commit_short}"
        return RepoInfo(
            id=self.entry.instance_id,
            name=display_name,
            repo=self.entry.repo,
            base_commit=self.entry.base_commit,
            commit_short=self.entry.commit_short,
            language=self.entry.language,
            description=self._description(),
            problem_statement=self.entry.problem_statement,
            languages=self.manifest.languages,
            file_count=self._file_count(),
            capabilities=capabilities,
            graph_coverage=self.graph_coverage(),
        )

    def graph_coverage(self) -> GraphCoverage | None:
        """Describe partial multi-language graph coverage when metadata exists."""

        entry = self.manifest.indexes.get("symbol_graph")
        if entry is None:
            return None
        metadata = entry.metadata or {}
        available = [
            str(language)
            for language in metadata.get("available_languages") or ()
            if str(language).strip()
        ]
        failures = metadata.get("failed_languages") or {}
        unavailable = (
            [str(language) for language in failures if str(language).strip()]
            if isinstance(failures, dict)
            else []
        )
        if not available and not unavailable:
            return None
        return GraphCoverage(
            available_languages=available,
            unavailable_languages=unavailable,
            partial=bool(metadata.get("partial") or unavailable),
        )

    def graph_setup(self):
        """Return repository-aware setup diagnostics for Dependency Map."""

        from ..graph.setup import diagnose_graph_setup

        languages = list(self.manifest.languages)
        if not languages and self.entry.language:
            languages = [self.entry.language]
        return diagnose_graph_setup(
            self.entry.repo_dir,
            languages,
        )

    def _graph_path(self) -> Optional[str]:
        """Locate this repo's prebuilt symbol-graph pickle, if any.

        The manifest only declares bm25/vector, but the prebuilt tree ships a
        ``graph.pkl`` alongside the vector store (and, when present, via a
        ``symbol_graph`` entry), so probe the legacy vector location only when
        there is no explicit graph entry. Result is cached.
        """
        cached = getattr(self, "_graph_path_cache", "?")
        if cached != "?":
            return cached
        candidates: List[str] = []
        sg = self.manifest.indexes.get("symbol_graph")
        if sg is not None:
            if self._view_is_current(sg) and getattr(sg, "path", None):
                candidates.append(
                    sg.path
                    if sg.path.endswith(".pkl")
                    else os.path.join(sg.path, "graph.pkl")
                )
        else:
            vec = self.manifest.indexes.get("vector")
            if (
                vec is not None
                and self._view_is_current(vec)
                and getattr(vec, "path", None)
            ):
                candidates.append(os.path.join(vec.path, "graph.pkl"))
        found = next((p for p in candidates if p and os.path.isfile(p)), None)
        self._graph_path_cache = found
        return found

    def _view_is_current(self, entry: Any) -> bool:
        """Whether a persisted view is eligible for the manifest's snapshot."""

        if getattr(entry, "status", None) != "fresh":
            return False
        view_commit = str(getattr(entry, "commit", "") or "")
        manifest_commit = str(getattr(self.manifest, "commit", "") or "")
        return not view_commit or not manifest_commit or view_commit == manifest_commit

    def code_graph(self) -> Optional[CodeGraph]:
        """Lazily load + cache the repo's symbol graph (None if unavailable)."""
        if getattr(self, "_code_graph_loaded", False):
            return self._code_graph
        self._code_graph_loaded = True
        self._code_graph = None
        path = self._graph_path()
        if path is None:
            return None
        try:
            from ..graph.code_graph import CodeGraph

            self._code_graph = CodeGraph.load_graph(path)
            logger.info(
                "codemap: loaded symbol graph for %r (%s)", self.entry.instance_id, path
            )
        except Exception as exc:  # noqa: BLE001 - stale/old-format graph: skip codemap
            logger.warning("codemap: graph at %s unusable: %s", path, exc)
            self._code_graph = None
        return self._code_graph

    def graph_unavailable_note(self) -> str:
        """Return an actionable reason when the dependency graph cannot load."""

        entry = self.manifest.indexes.get("symbol_graph")
        if entry is None:
            return (
                "Dependency graph is not built. Use the graph profile to add "
                "a language-aware symbol graph for this repository."
            )
        if entry.status == "stale":
            return "Dependency graph is stale for this commit and must be updated."
        if entry.status == "failed":
            detail = str(entry.metadata.get("error") or "").strip()
            if detail:
                return f"Dependency graph build failed: {detail[:240]}"
            return "Dependency graph build failed; inspect the index build output."
        return "Dependency graph artifact could not be loaded; rebuild symbol_graph."

    def hierarchical_graph(self):
        """Lazily build + cache the repo-level compound graph for CodeGraph UI."""
        if getattr(self, "_hierarchical_graph_loaded", False):
            return self._hierarchical_graph
        self._hierarchical_graph_loaded = True
        self._hierarchical_graph = None
        graph = self.code_graph()
        if graph is None:
            return None
        try:
            from ..graph.hierarchy import build_hierarchical_code_graph

            self._hierarchical_graph = build_hierarchical_code_graph(
                graph, repo_dir=self.entry.repo_dir
            )
            logger.info(
                "codemap: built hierarchical graph for %r "
                "(%d containment nodes, %d dependencies)",
                self.entry.instance_id,
                len(self._hierarchical_graph.containment),
                len(self._hierarchical_graph.dependencies),
            )
        except Exception as exc:  # noqa: BLE001 - hierarchy is a UI enhancement
            logger.warning(
                "codemap: failed to build hierarchy for %r: %s",
                self.entry.instance_id,
                exc,
                exc_info=True,
            )
            self._hierarchical_graph = None
        return self._hierarchical_graph

    def _file_count(self) -> int:
        cached = getattr(self, "_file_count_cache", None)
        if cached is not None:
            return cached
        indexes = getattr(self.manifest, "indexes", {}) or {}
        bm25 = indexes.get("bm25")
        metadata = getattr(bm25, "metadata", {}) or {}
        n = int(metadata.get("source_file_count") or 0)
        if not n:
            n = self.manifest.file_count or 0
        vs = self.vector_store
        if vs is not None:
            docs = getattr(vs, "l0_documents", None)
            if docs:
                n = len(docs)
        self._file_count_cache = n
        return n

    def _description(self) -> str:
        cached = getattr(self, "_description_cache", None)
        if cached is not None:
            return cached
        desc = read_repository_summary(self.entry.repo_dir)
        self._description_cache = desc
        return desc


class RepoRegistry:
    """Holds the loaded :class:`RepoBundle` objects, keyed by instance id."""

    def __init__(self, config: QAConfig) -> None:
        self._config = config
        self._bundles: Dict[str, RepoBundle] = {}
        # One embedding model per model name, shared across repos: the GPU model
        # is loaded once instead of once per CodeVectorStore (one per repo).
        self._embeddings: Dict[Tuple[str, str, Optional[str]], object] = {}

    def load_all(self) -> None:
        """Load every dataset repo in the registry whose manifest exists."""
        entries = load_registry(self._config.registry_path)
        if not entries:
            logger.warning(
                "No QA registry at %s — run scripts/build_qa_index.py first.",
                self._config.registry_path,
            )
            return
        for entry in entries:
            if not os.path.exists(entry.manifest_path):
                logger.warning(
                    "Skipping %r: manifest not found at %s",
                    entry.instance_id,
                    entry.manifest_path,
                )
                continue
            try:
                self._bundles[entry.instance_id] = self._load_repo_metadata(entry)
                logger.info("Registered %r (%s)", entry.instance_id, entry.repo)
            except Exception as exc:  # noqa: BLE001 - keep other repos alive
                logger.error(
                    "Failed to load %r: %s", entry.instance_id, exc, exc_info=True
                )

    def _load_repo_metadata(self, entry: RepoEntry) -> RepoBundle:
        manifest = RepoManifest.load(entry.manifest_path)
        return RepoBundle(
            entry=entry,
            manifest=manifest,
            chat_available=find_spec("litellm") is not None,
            view_loader=self._load_repo_views,
            runtime_loader=self._load_repo_runtime,
        )

    def _load_vector_store(self, vec_entry: Any) -> "CodeVectorStore":
        """Load a manifest vector view with the configured embedding backend."""
        emb_model = vec_entry.config.get(
            "embedding_model", self._config.embedding_model
        )
        emb_dim = vec_entry.config.get(
            "embedding_dimension", self._config.embedding_dimension
        )
        provider = self._config.embedding_provider
        cache_key = (provider, emb_model, self._config.embedding_base_url)
        client_kwargs: Dict[str, object] = {}
        if self._config.embedding_base_url:
            client_kwargs["base_url"] = self._config.embedding_base_url
        if self._config.embedding_api_key:
            client_kwargs["api_key"] = self._config.embedding_api_key

        vector_store = _vector_store_type()(
            embedding_model=emb_model,
            embedding_provider=provider,
            dimension=emb_dim,
            store_path=vec_entry.path,
            embedding=self._embeddings.get(cache_key),
            **client_kwargs,
        )
        self._embeddings[cache_key] = vector_store.embedding
        vector_store.load(vec_entry.path)
        return vector_store

    def _create_ask_llm(self) -> "LiteLLMChat":
        """Create the interactive model without mutating process-wide config."""
        return _ask_llm_type()(
            model=self._config.model,
            temperature=0.0,
            max_tokens=self._config.max_tokens,
            api_base=self._config.model_api_base,
            api_key=self._config.model_api_key,
            extra_kwargs=self._config.model_options,
        )

    def _load_repo_views(self, bundle: RepoBundle) -> None:
        """Load only the persisted retrieval views needed by static Wiki pages."""
        from ..index.sparse_idx.bm25_index import BM25CodeIndexer

        manifest = bundle.manifest

        bm25_index: Optional["BM25CodeIndexer"] = None
        vector_store: Optional["CodeVectorStore"] = None

        bm25_entry = manifest.indexes.get("bm25")
        if bm25_entry is not None and bm25_entry.status == "fresh":
            bm25_index = BM25CodeIndexer()
            bm25_index.load_index(bm25_entry.path)

        vec_entry = manifest.indexes.get("vector")
        if vec_entry is not None and vec_entry.status == "fresh":
            vector_store = self._load_vector_store(vec_entry)

        bundle.vector_store = vector_store
        bundle.bm25 = bm25_index

    def _load_repo_runtime(self, bundle: RepoBundle) -> None:
        from ..agent.runner import AgentRunner
        from ..agent.skills.loader import SkillLoader
        from ..compiler.params import SessionContext
        from ..ops.retrieve import RetrieveContext

        entry = bundle.entry
        manifest = bundle.manifest
        bm25_index = bundle.bm25
        vector_store = bundle.vector_store

        retrieve_ctx = RetrieveContext(
            bm25=bm25_index,
            vector_store=vector_store,
            default_top_k=10,
            default_level="l2",
        )
        contexts: Dict[str, object] = {"retrieve": retrieve_ctx}
        if vector_store is not None:
            from ..ops.rerank import RerankContext

            contexts["rerank"] = RerankContext(embedding_store=vector_store)

        registry = _fresh_registry()
        loader = SkillLoader()
        loader.load_all(_SKILLS_DIR, contexts=contexts, registry=registry)

        session_ctx = SessionContext(
            repo_path=manifest.repo_path,
            repo_size=manifest.file_count,
            primary_language=(
                manifest.languages[0] if manifest.languages else entry.language
            ),
        )

        llm = self._create_ask_llm()
        runner = AgentRunner(
            llm=llm,
            registry=registry,
            max_turns=self._config.max_turns,
            manifest=manifest,
            session_ctx=session_ctx,
            system_prompt=_DEMO_SYSTEM_PROMPT,
            # The demo answers from the retrieval indexes (BM25 + embeddings),
            # which return citable nodes that feed the answer's code pane. The
            # default read/grep/glob/bash tools return plain text (no
            # citations) and let the model grep-and-give-up, so withhold them.
            include_default_tools=False,
            # Keep prose answers: the Files:/Symbols:/Locations: schema turn is
            # for the localization eval and would replace the explanation.
            force_localization_contract=False,
            # A capped exploration should still end in a usable answer (one
            # extra tool-free turn) instead of mid-search chatter.
            force_final_answer=True,
        )
        bundle.runner = runner

    # -- queries --

    def list_infos(self) -> List[RepoInfo]:
        return [b.info() for b in self._bundles.values()]

    def get(self, repo_id: str) -> Optional[RepoBundle]:
        return self._bundles.get(repo_id)
