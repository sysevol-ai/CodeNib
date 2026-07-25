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
import re
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..agent.runner import AgentRunner
from ..agent.skills.loader import SkillLoader
from ..agent.skills.registry import SkillRegistry
from ..compiler.manifest import RepoManifest
from ..compiler.params import SessionContext
from ..graph.code_graph import CodeGraph
from ..index.embedding.vector_store import CodeVectorStore
from ..index.sparse_idx.bm25_index import BM25CodeIndexer
from ..llm.litellm_chat import LiteLLMChat
from ..log_utils import get_logger
from ..ops.rerank import RerankContext
from ..ops.retrieve import RetrieveContext
from .config import QAConfig, RepoEntry, load_registry
from .schemas import RepoInfo

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
    "you found so the reader can open them. If a search returns nothing useful, "
    "try a different query or tool before concluding."
)


_README_SKIP = re.compile(
    r"\b(install|download|getting started|to get started|usage|build from source"
    r"|clone|npm i\b|pip install|cargo add|see (the )?docs|documentation"
    r"|these steps|version information|for example|e\.g\.)\b",
    re.IGNORECASE,
)
# Lines that are clearly boilerplate prefixes, not a project tagline.
_README_SKIP_PREFIX = ("note:", "warning:", "tip:", "see ", "run ", "$ ")


def _readme_summary(text: str, limit: int = 160) -> str:
    """A descriptive sentence from a README — skipping headings, badges, HTML,
    and install/usage boilerplate (prefers the project tagline)."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("#", ">", "<", "---", "===", "|", "- ", "* ", "```")):
            continue
        if line.startswith(("![", "[![")) or line.startswith("["):
            continue  # badge / image / link-only line
        # Strip markdown links/emphasis, keep the visible text.
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"[*_`]", "", line)
        line = re.sub(r"<[^>]+>", "", line).strip()
        # Require a real sentence: skip labels ("Requirements:"), short lines.
        if line.endswith(":") or len(line.split()) < 6 or _README_SKIP.search(line):
            continue
        if line.lower().startswith(_README_SKIP_PREFIX):
            continue
        return (line[:limit] + "…") if len(line) > limit else line
    return ""


def _fresh_registry() -> SkillRegistry:
    """Create an isolated registry that bypasses the global singleton."""
    reg = object.__new__(SkillRegistry)
    reg._skills = {}
    return reg


@dataclass
class RepoBundle:
    """Everything needed to answer questions about one repo."""

    entry: RepoEntry
    manifest: RepoManifest
    runner: Optional[AgentRunner] = None
    # Read-only handles reused by the wiki builder (index-derived docs).
    vector_store: Optional[CodeVectorStore] = None
    bm25: Optional[BM25CodeIndexer] = None
    runtime_loader: Optional[Callable[["RepoBundle"], None]] = None

    def __post_init__(self) -> None:
        self._runtime_lock = Lock()
        self._runtime_loaded = self.runner is not None

    def ensure_runtime(self) -> None:
        """Load heavy retrieval indexes and the QA runner on first real use."""
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
        capabilities["codemap"] = self._graph_path() is not None
        return RepoInfo(
            id=self.entry.instance_id,
            name=f"{self.entry.repo} @ {self.entry.commit_short}",
            repo=self.entry.repo,
            base_commit=self.entry.base_commit,
            commit_short=self.entry.commit_short,
            language=self.entry.language,
            description=self._description(),
            problem_statement=self.entry.problem_statement,
            languages=self.manifest.languages,
            file_count=self._file_count(),
            capabilities=capabilities,
        )

    def _graph_path(self) -> Optional[str]:
        """Locate this repo's prebuilt symbol-graph pickle, if any.

        The manifest only declares bm25/vector, but the prebuilt tree ships a
        ``graph.pkl`` alongside the vector store (and, when present, via a
        ``symbol_graph`` entry), so probe both. Result is cached.
        """
        cached = getattr(self, "_graph_path_cache", "?")
        if cached != "?":
            return cached
        candidates: List[str] = []
        sg = self.manifest.indexes.get("symbol_graph")
        if sg is not None and getattr(sg, "path", None):
            candidates.append(
                sg.path
                if sg.path.endswith(".pkl")
                else os.path.join(sg.path, "graph.pkl")
            )
        vec = self.manifest.indexes.get("vector")
        if vec is not None and getattr(vec, "path", None):
            candidates.append(os.path.join(vec.path, "graph.pkl"))
        found = next((p for p in candidates if p and os.path.isfile(p)), None)
        self._graph_path_cache = found
        return found

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
            self._code_graph = CodeGraph.load_graph(path)
            logger.info(
                "codemap: loaded symbol graph for %r (%s)", self.entry.instance_id, path
            )
        except Exception as exc:  # noqa: BLE001 - stale/old-format graph: skip codemap
            logger.warning("codemap: graph at %s unusable: %s", path, exc)
            self._code_graph = None
        return self._code_graph

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
        desc = ""
        repo_dir = self.entry.repo_dir
        for name in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
            path = os.path.join(repo_dir, name)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    desc = _readme_summary(fh.read())
            except OSError:
                desc = ""
            break
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
            runtime_loader=self._load_repo_runtime,
        )

    def _load_vector_store(self, vec_entry: Any) -> CodeVectorStore:
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

        vector_store = CodeVectorStore(
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

    def _create_ask_llm(self) -> LiteLLMChat:
        """Create the interactive model without mutating process-wide config."""
        return LiteLLMChat(
            model=self._config.model,
            temperature=0.0,
            max_tokens=self._config.max_tokens,
            api_base=self._config.model_api_base,
            api_key=self._config.model_api_key,
        )

    def _load_repo_runtime(self, bundle: RepoBundle) -> None:
        entry = bundle.entry
        manifest = bundle.manifest

        bm25_index: Optional[BM25CodeIndexer] = None
        vector_store: Optional[CodeVectorStore] = None

        bm25_entry = manifest.indexes.get("bm25")
        if bm25_entry is not None and bm25_entry.status == "fresh":
            bm25_index = BM25CodeIndexer()
            bm25_index.load_index(bm25_entry.path)

        vec_entry = manifest.indexes.get("vector")
        if vec_entry is not None and vec_entry.status == "fresh":
            vector_store = self._load_vector_store(vec_entry)

        retrieve_ctx = RetrieveContext(
            bm25=bm25_index,
            vector_store=vector_store,
            default_top_k=10,
            default_level="l2",
        )
        contexts: Dict[str, object] = {"retrieve": retrieve_ctx}
        if vector_store is not None:
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
        )
        bundle.runner = runner
        bundle.vector_store = vector_store
        bundle.bm25 = bm25_index

    # -- queries --

    def list_infos(self) -> List[RepoInfo]:
        return [b.info() for b in self._bundles.values()]

    def get(self, repo_id: str) -> Optional[RepoBundle]:
        return self._bundles.get(repo_id)
