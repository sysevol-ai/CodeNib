# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Cycle orchestrator: sync → index → observe → investigate → report.

Phase 1 of the Repository Guardian RFC — one cycle, no memory, no graph-diff, no
candidate patches. Reuses the existing CodeMiner index compiler and cascade
retrieval; this module only glues them into a single non-modifying pass.
"""

from __future__ import annotations

import contextlib
import logging as _logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Generator, List, Optional, Sequence

from ..log_utils import get_logger
from .report import Finding, GuardianReport
from .signals import Hotspot, TestFailure, churn_hotspots, run_test_suite

logger = get_logger(__name__)

# Callable that produces an LLM narrative for a hotspot.
# Returns an empty string to signal "no narrative" (e.g. LLM disabled/unavailable).
Reporter = Callable[[Hotspot], str]


@dataclass
class GuardianConfig:
    """Configuration for a single Guardian cycle."""

    repo_path: str
    languages: Sequence[str] = field(default_factory=lambda: ("python",))
    index_cache_dir: Optional[str] = None
    index_types: Sequence[str] = field(default_factory=lambda: ("bm25",))
    top_n: int = 10
    since: str = "90 days ago"
    run_tests: bool = False
    retrieval_top_k: int = 5
    embedding_model: str = "nomic-ai/CodeRankEmbed"
    embedding_dimension: int = 768
    use_llm: bool = False
    llm_model: str = "vertex_ai/gemini-2.5-flash"
    llm_max_tool_rounds: int = 3
    episode_dir: Optional[str] = None
    # Cross-cycle memory
    memory_dir: Optional[str] = None   # path to the memory store dir; None disables
    arm: str = "memory"                # "memory" | "memoryless" ablation toggle
    graph_snapshot: bool = True        # save CodeGraph snapshot each cycle for next diff


def _current_commit(repo_path: str) -> str:
    """Return HEAD SHA, or '' if not a git repo (mirrors scripts/index_repo.py)."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _guardian_loggers() -> List[_logging.Logger]:
    """Return all live Logger instances under the ``codeminer.guardian`` namespace.

    ``get_logger`` sets ``propagate=False`` on every logger it creates, so we
    cannot rely on the parent logger to fan-out to a FileHandler.  Instead we
    enumerate the standard logging manager's dict and attach directly to each
    guardian logger.
    """
    return [
        obj
        for name, obj in _logging.Logger.manager.loggerDict.items()
        if name.startswith("codeminer.guardian")
        and isinstance(obj, _logging.Logger)
    ]


@contextlib.contextmanager
def _episode_log_handler(
    episode_dir: Optional[str],
) -> Generator[None, None, None]:
    """Attach a DEBUG FileHandler to every guardian logger for one cycle.

    All ``logger.debug/info/warning`` calls inside ``codeminer.guardian.*``
    are written to ``{episode_dir}/guardian.log`` for the duration of the
    ``with`` block.
    """
    if not episode_dir:
        yield
        return

    os.makedirs(episode_dir, exist_ok=True)
    log_path = os.path.join(episode_dir, "guardian.log")
    handler = _logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(_logging.DEBUG)
    handler.setFormatter(
        _logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")
    )
    loggers = _guardian_loggers()
    for lg in loggers:
        lg.addHandler(handler)
    try:
        yield
    finally:
        for lg in loggers:
            lg.removeHandler(handler)
        handler.close()


def _compile_index(config: GuardianConfig):
    """Build/load the index for the checkout and return its RepoManifest.

    Mirrors scripts/index_repo.py: register default builders, then compile. The
    compiler caches under ``.codeminer_cache`` so re-runs and the retrieval
    pipeline share artifacts.
    """
    from ..compiler import IndexCompiler, IndexCompilerConfig
    from ..compiler.index_builders import (
        IndexBuilderRegistry,
        register_default_builders,
    )

    languages = list(config.languages)
    registry = IndexBuilderRegistry()
    register_default_builders(
        registry,
        languages=languages,
        embedding_model=config.embedding_model,
        embedding_dimension=config.embedding_dimension,
    )
    compiler = IndexCompiler(
        registry,
        IndexCompilerConfig(
            index_types=list(config.index_types),
            languages=languages,
        ),
    )
    return compiler.compile_repo(
        config.repo_path,
        cache_dir=config.index_cache_dir,
    )


class _NullRetriever:
    """Last-resort retriever that always returns no results (should rarely be reached)."""

    def query(self, query: str, top_k: Optional[int] = None) -> list:
        return []


class _BM25Adapter:
    """Adapt BM25CodeIndexer.search() to the _Retriever .query() protocol."""

    def __init__(self, idx: object) -> None:
        self._idx = idx

    def query(self, query: str, top_k: Optional[int] = None) -> list:
        return self._idx.search(query, top_k=top_k)  # type: ignore[union-attr]


def _load_bm25_from_manifest(manifest: object) -> Optional[object]:
    """Load a BM25CodeIndexer from a compiled manifest and wrap it as a _Retriever.

    Returns ``None`` if the manifest has no fresh BM25 entry or loading fails.
    """
    try:
        from ..index.sparse_idx.bm25_index import BM25CodeIndexer

        indexes = getattr(manifest, "indexes", None) or {}
        entry = indexes.get("bm25")
        if entry is None:
            logger.warning("Guardian: manifest has no BM25 index entry")
            return None
        if getattr(entry, "status", None) != "fresh":
            logger.warning("Guardian: BM25 index status is not fresh: %s", entry.status)
            return None
        idx = BM25CodeIndexer()
        idx.load_index(entry.path)
        return _BM25Adapter(idx)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Guardian: failed to load BM25 index from manifest: %s", exc)
        return None


def _load_current_graph(manifest: object) -> Optional[object]:
    """Load the CodeGraph from the manifest's symbol_graph artifact, or None.

    Requires 'symbol_graph' to be a fresh index entry with a graph.pkl inside
    its artifact directory.  Returns None silently if unavailable.
    """
    try:
        indexes = getattr(manifest, "indexes", None) or {}
        entry = indexes.get("symbol_graph")
        if entry is None or getattr(entry, "status", None) != "fresh":
            return None
        graph_pkl = os.path.join(entry.path, "graph.pkl")
        if not os.path.exists(graph_pkl):
            return None
        from ..graph.code_graph import CodeGraph
        return CodeGraph.load_graph(graph_pkl)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Guardian: failed to load current graph from manifest: %s", exc)
        return None


def _make_llm(config: GuardianConfig) -> object:
    """Instantiate a LiteLLMChat for the cycle."""
    from ..llm.litellm_chat import LiteLLMChat

    return LiteLLMChat(model=config.llm_model, temperature=0.0, max_tokens=1024)


def _llm_reporter(
    config: GuardianConfig,
    retriever: object,
    llm: object,
    usage_acc: object,
) -> Reporter:
    """Return a Reporter that runs the LLM agentic loop for each churn hotspot."""
    from .llm_investigator import investigate_with_llm

    def _report(hotspot: Hotspot) -> str:
        try:
            return investigate_with_llm(
                hotspot,
                retriever,
                repo_path=config.repo_path,
                since=config.since,
                llm=llm,  # type: ignore[arg-type]
                max_tool_rounds=config.llm_max_tool_rounds,
                top_k=config.retrieval_top_k,
                usage_acc=usage_acc,  # type: ignore[arg-type]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Guardian: LLM reporter failed for %s: %s", hotspot.path, exc
            )
            return ""

    return _report


def _infer_source_from_nodeid(nodeid: str, repo_path: str) -> str:
    """Heuristic: map test/pkg/test_module.py::test_foo → codeminer/pkg/module.py."""
    import re

    test_file = nodeid.split("::")[0]
    rel = re.sub(r"^test/", "", test_file)
    parts = rel.rsplit("/", 1)
    basename = re.sub(r"^test_", "", parts[-1])
    candidates = []
    if len(parts) > 1:
        pkg = parts[0]
        candidates = [
            os.path.join("codeminer", pkg, basename),
            os.path.join(pkg, basename),
        ]
    else:
        candidates = [os.path.join("codeminer", basename), basename]
    for c in candidates:
        if os.path.isfile(os.path.join(repo_path, c)):
            return c
    return ""


def _investigate_test_failure(
    failure: TestFailure,
    retriever: object,
    llm: object,
    config: "GuardianConfig",
    usage_acc: object = None,
) -> str:
    """Run the LLM agentic loop for a single failing test."""
    from .llm_investigator import build_test_failure_context, investigate_signal, read_file

    repo_path = config.repo_path
    test_file = failure.nodeid.split("::")[0]
    test_content = read_file(os.path.join(repo_path, test_file))
    source_path = _infer_source_from_nodeid(failure.nodeid, repo_path)
    source_content = read_file(os.path.join(repo_path, source_path)) if source_path else ""

    ctx = build_test_failure_context(
        failure.nodeid,
        failure.message,
        test_content=test_content,
        source_content=source_content,
        source_path=source_path,
    )
    try:
        return investigate_signal(
            ctx,
            retriever,
            llm=llm,  # type: ignore[arg-type]
            max_tool_rounds=config.llm_max_tool_rounds,
            top_k=config.retrieval_top_k,
            usage_acc=usage_acc,  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Guardian: LLM reporter failed for %s: %s", failure.nodeid, exc)
        return ""


def run_cycle(
    config: GuardianConfig,
    *,
    reporter: Optional[Reporter] = None,
    manifest: object = None,
) -> GuardianReport:
    """Run one Guardian cycle and return a (non-modifying) report.

    Args:
        config: Cycle configuration.
        reporter: Override the LLM narrative callable (for tests). When omitted,
            a LiteLLM-backed reporter is built iff ``config.use_llm`` is True.
        manifest: Pre-built RepoManifest to reuse instead of compiling (tests).

    Returns:
        A :class:`GuardianReport` ready to render.
    """
    with _episode_log_handler(config.episode_dir):
        return _run_cycle_inner(config, reporter=reporter, manifest=manifest)


def _run_cycle_inner(
    config: GuardianConfig,
    *,
    reporter: Optional[Reporter] = None,
    manifest: object = None,
) -> GuardianReport:
    repo_path = config.repo_path
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # 1. Sync — record the commit under inspection.
    commit = _current_commit(repo_path)

    # 2. Index — build/load via the existing compiler (skipped if manifest given).
    if manifest is None:
        logger.info("Guardian: compiling index for %s", repo_path)
        manifest = _compile_index(config)
    commit = getattr(manifest, "commit", "") or commit
    file_count = int(getattr(manifest, "file_count", 0) or 0)
    capabilities = dict(getattr(manifest, "capabilities", {}) or {})

    # 0. Memory — load cross-cycle state (prior graph + recent findings).
    #    Numbered 0 because it logically precedes observe but depends on the
    #    commit resolved in step 1.
    _store: Optional[object] = None
    _prior_graph: Optional[object] = None
    _prior_findings: List[dict] = []
    if config.memory_dir:
        from .memory import MemoryStore
        _store = MemoryStore(
            config.memory_dir,
            readonly=(config.arm == "memoryless"),
        )
        _prior_graph = _store.load_prior_graph(config.memory_dir)  # type: ignore[attr-defined]
        _prior_findings = _store.recent_findings(k=5)  # type: ignore[attr-defined]
        logger.info(
            "Guardian: memory loaded — %d prior finding(s), prior graph: %s",
            len(_prior_findings),
            "yes" if _prior_graph is not None else "none",
        )

    # 3. Observe — churn hotspots + graph-diff drift signals + optional test run.
    hotspots = churn_hotspots(repo_path, since=config.since, top_n=config.top_n)
    logger.info("Guardian: %d churn hotspot(s)", len(hotspots))

    # Graph-diff drift: compare current CodeGraph against prior cycle's snapshot.
    # Requires 'symbol_graph' in index_types so the artifact exists in the manifest.
    _current_graph: Optional[object] = None
    _edge_changes: list = []
    _drift_signals: list = []
    if _prior_graph is not None:
        _current_graph = _load_current_graph(manifest)
        if _current_graph is not None:
            from .graph_diff import compute_drift_signals, diff_graphs
            _edge_changes = diff_graphs(_prior_graph, _current_graph)  # type: ignore[arg-type]
            _drift_signals = compute_drift_signals(
                _edge_changes, _current_graph, _prior_graph  # type: ignore[arg-type]
            )
            logger.info("Guardian: %d drift signal(s) from graph diff", len(_drift_signals))
        else:
            logger.debug(
                "Guardian: symbol_graph artifact unavailable — skipping graph diff "
                "(add 'symbol_graph' to index_types to enable)"
            )
    elif config.graph_snapshot and config.memory_dir:
        # No prior graph yet (first cycle): load the current graph so we can
        # snapshot it in step 7 and give the next cycle something to diff against.
        _current_graph = _load_current_graph(manifest)

    test_result = None
    if config.run_tests:
        logger.info("Guardian: running test suite (gated by --run-tests)")
        test_result = run_test_suite(repo_path)

    # 4. Build retriever — used by the LLM's search_code tool calls.
    #
    # Retriever selection driven by index_types:
    #   - "vector" present  → HybridRetrievePipeline (BM25 + embeddings; needs GPU)
    #   - BM25 only         → BM25CodeIndexer loaded directly from the manifest (no GPU)
    _retriever: object = _NullRetriever()
    use_hybrid = "vector" in config.index_types
    if use_hybrid:
        try:
            from ..model.hybrid_retrieve_pipeline import HybridRetrievePipeline

            _retriever = HybridRetrievePipeline(
                repo_path=config.repo_path,
                index_cache_dir=config.index_cache_dir,
                languages=tuple(config.languages),
                embedding_model=config.embedding_model,
                embedding_dimension=config.embedding_dimension,
            )
            logger.info("Guardian: using HybridRetrievePipeline (BM25 + vector)")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Guardian: HybridRetrievePipeline unavailable (%s); "
                "falling back to BM25-only",
                exc,
            )
            _retriever = _load_bm25_from_manifest(manifest) or _NullRetriever()
    else:
        logger.info("Guardian: using BM25-only retriever (no GPU required)")
        _retriever = _load_bm25_from_manifest(manifest) or _NullRetriever()

    # 5. Report — optionally run the LLM investigation step.
    #    A single LLM instance is shared across all signal types so we don't
    #    pay the connection-setup cost multiple times per cycle.
    _llm: Optional[object] = None
    _usage_acc: Optional[object] = None
    if config.use_llm:
        from .llm_investigator import LLMUsage

        _llm = _make_llm(config)
        _usage_acc = LLMUsage()

    if reporter is None and _llm is not None:
        reporter = _llm_reporter(config, _retriever, _llm, _usage_acc)

    findings: List[Finding] = []
    for hotspot in hotspots:
        narrative = reporter(hotspot) if reporter is not None else ""
        findings.append(
            Finding(
                kind="churn",
                title=f"High-churn file: {hotspot.path}",
                detail=(
                    f"Changed in **{hotspot.commit_count}** commits over "
                    f"{config.since}."
                ),
                evidence=[],
                narrative=narrative,
            )
        )

    if test_result is not None and test_result.ran:
        for failure in test_result.failures:
            narrative = (
                _investigate_test_failure(failure, _retriever, _llm, config, _usage_acc)
                if _llm is not None
                else ""
            )
            findings.append(
                Finding(
                    kind="test_failure",
                    title=f"Failing test: {failure.nodeid}",
                    detail=failure.message,
                    narrative=narrative,
                )
            )

    # Append drift findings after churn/test findings.
    if _drift_signals:
        from .graph_diff import drift_findings
        findings.extend(drift_findings(_drift_signals))

    # 6. Build report.
    report = GuardianReport(
        repo=repo_path,
        commit=commit,
        generated_at=generated_at,
        churn_window=config.since,
        file_count=file_count,
        capabilities=capabilities,
        findings=findings,
        tests_ran=bool(test_result and test_result.ran),
        tests_summary=(test_result.summary if test_result else ""),
        llm_usage=_usage_acc,  # type: ignore[arg-type]
    )

    # 7. Persist — write cycle state to memory store (no-op when memory_dir unset
    #    or arm == "memoryless").
    if _store is not None:
        graph_to_save = _current_graph if config.graph_snapshot else None
        cycle_no = _store.persist_cycle(  # type: ignore[attr-defined]
            report,
            graph=graph_to_save,
            memory_dir=config.memory_dir,
        )
        if _edge_changes and cycle_no > 0:
            _store.persist_edge_changes(cycle_no, _edge_changes)  # type: ignore[attr-defined]

    return report
