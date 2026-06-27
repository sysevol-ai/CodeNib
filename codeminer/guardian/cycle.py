# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Cycle orchestrator: sync → index → observe → investigate → report.

Phase 1 of the Repository Guardian RFC — one cycle, no memory, no graph-diff, no
candidate patches. Reuses the existing CodeMiner index compiler and cascade
retrieval; this module only glues them into a single non-modifying pass.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional, Sequence

from ..log_utils import get_logger
from .investigate import Evidence, investigate_hotspot
from .report import Finding, GuardianReport
from .signals import Hotspot, churn_hotspots, run_test_suite

logger = get_logger(__name__)

# Callable that gathers evidence for one hotspot. Injectable for testing.
Investigator = Callable[[Hotspot], List[Evidence]]

# Callable that produces an LLM narrative for a hotspot + its evidence.
# Returns an empty string to signal "no narrative" (e.g. LLM disabled/unavailable).
Reporter = Callable[[Hotspot, List[Evidence]], str]


@dataclass
class GuardianConfig:
    """Configuration for a single Guardian cycle."""

    repo_path: str
    languages: Sequence[str] = ("python",)
    index_cache_dir: Optional[str] = None
    index_types: Sequence[str] = ("bm25",)
    top_n: int = 10
    since: str = "90 days ago"
    run_tests: bool = False
    investigate: bool = True
    retrieval_top_k: int = 5
    embedding_model: str = "nomic-ai/CodeRankEmbed"
    embedding_dimension: int = 768
    use_llm: bool = False
    llm_model: str = "vertex_ai/gemini-2.5-flash"
    llm_max_tool_rounds: int = 3


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


def _llm_reporter(config: GuardianConfig, retriever: object) -> Reporter:
    """Return a Reporter that runs the LLM agentic loop for each hotspot.

    ``retriever`` is any object with ``.query(str, top_k=int)`` — the same duck
    type accepted by :func:`~.investigate.investigate_hotspot`. It is captured
    once and reused across hotspots.
    """
    from ..llm.litellm_chat import LiteLLMChat
    from .llm_investigator import investigate_with_llm

    llm = LiteLLMChat(model=config.llm_model, temperature=0.0, max_tokens=1024)

    def _report(hotspot: Hotspot, evidence: List[Evidence]) -> str:
        try:
            return investigate_with_llm(
                hotspot,
                retriever,
                repo_path=config.repo_path,
                since=config.since,
                initial_evidence=evidence,
                llm=llm,
                max_tool_rounds=config.llm_max_tool_rounds,
                top_k=config.retrieval_top_k,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Guardian: LLM reporter failed for %s: %s", hotspot.path, exc
            )
            return ""

    return _report


def run_cycle(
    config: GuardianConfig,
    *,
    investigator: Optional[Investigator] = None,
    reporter: Optional[Reporter] = None,
    manifest: object = None,
) -> GuardianReport:
    """Run one Guardian cycle and return a (non-modifying) report.

    Args:
        config: Cycle configuration.
        investigator: Override the evidence-gathering callable (for tests). When
            omitted, a HybridRetrievePipeline-backed investigator is built iff
            ``config.investigate`` is True.
        reporter: Override the LLM narrative callable (for tests). When omitted,
            a LiteLLM-backed reporter is built iff ``config.use_llm`` is True.
        manifest: Pre-built RepoManifest to reuse instead of compiling (tests).

    Returns:
        A :class:`GuardianReport` ready to render.
    """
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

    # 3. Observe — churn hotspots (always) + optional test run.
    hotspots = churn_hotspots(repo_path, since=config.since, top_n=config.top_n)
    logger.info("Guardian: %d churn hotspots", len(hotspots))

    test_result = None
    if config.run_tests:
        logger.info("Guardian: running test suite (gated by --run-tests)")
        test_result = run_test_suite(repo_path)

    # 4. Investigate — build a retriever, then attach evidence to each hotspot.
    #
    # Retriever selection driven by index_types:
    #   - "vector" present  → HybridRetrievePipeline (BM25 + embeddings; needs GPU)
    #   - BM25 only         → BM25CodeIndexer loaded directly from the manifest (no GPU)
    #
    # The retriever is shared with the LLM reporter so the model can issue additional
    # search_code calls without a separate index load.
    _retriever: object = _NullRetriever()
    if investigator is None:
        if config.investigate:
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
                    logger.info(
                        "Guardian: using HybridRetrievePipeline (BM25 + vector)"
                    )
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

            investigator = lambda h: investigate_hotspot(  # noqa: E731
                h, _retriever, top_k=config.retrieval_top_k
            )
        else:
            # investigate=False: skip retrieval entirely (programmatic / test use).
            investigator = lambda _h: []  # noqa: E731

    # 5. Report — optionally run the LLM investigation step.
    #    The LLM always gets the same retriever used for evidence, so search_code
    #    calls return real results (never _NullRetriever when investigate=True).
    if reporter is None and config.use_llm:
        reporter = _llm_reporter(config, _retriever)

    findings: List[Finding] = []
    for hotspot in hotspots:
        evidence = investigator(hotspot)
        narrative = reporter(hotspot, evidence) if reporter is not None else ""
        findings.append(
            Finding(
                kind="churn",
                title=f"High-churn file: {hotspot.path}",
                detail=(
                    f"Changed in **{hotspot.commit_count}** commits over "
                    f"{config.since}."
                ),
                evidence=evidence,
                narrative=narrative,
            )
        )

    if test_result is not None and test_result.ran:
        for failure in test_result.failures:
            findings.append(
                Finding(
                    kind="test_failure",
                    title=f"Failing test: {failure.nodeid}",
                    detail=failure.message,
                )
            )

    return GuardianReport(
        repo=repo_path,
        commit=commit,
        generated_at=generated_at,
        churn_window=config.since,
        file_count=file_count,
        capabilities=capabilities,
        findings=findings,
        tests_ran=bool(test_result and test_result.ran),
        tests_summary=(test_result.summary if test_result else ""),
    )
