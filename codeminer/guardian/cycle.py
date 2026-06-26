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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional, Sequence

from ..log_utils import get_logger
from .investigate import Evidence, investigate_hotspot
from .report import Finding, GuardianReport
from .signals import Hotspot, churn_hotspots, run_test_suite

logger = get_logger(__name__)

# Callable that gathers evidence for one hotspot. Injectable for testing.
Investigator = Callable[[Hotspot], List[Evidence]]


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


def _default_investigator(config: GuardianConfig) -> Investigator:
    """Build the production investigator backed by HybridRetrievePipeline.

    The pipeline is constructed once (it loads/builds the indexes) and reused
    across hotspots. If it cannot be built, every hotspot degrades to no
    evidence rather than failing the cycle.
    """
    from ..model.hybrid_retrieve_pipeline import HybridRetrievePipeline

    try:
        pipeline = HybridRetrievePipeline(
            repo_path=config.repo_path,
            index_cache_dir=config.index_cache_dir,
            languages=tuple(config.languages),
            embedding_model=config.embedding_model,
            embedding_dimension=config.embedding_dimension,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Guardian: retrieval pipeline unavailable (%s); no evidence", exc)
        return lambda _hotspot: []

    return lambda hotspot: investigate_hotspot(
        hotspot, pipeline, top_k=config.retrieval_top_k
    )


def run_cycle(
    config: GuardianConfig,
    *,
    investigator: Optional[Investigator] = None,
    manifest: object = None,
) -> GuardianReport:
    """Run one Guardian cycle and return a (non-modifying) report.

    Args:
        config: Cycle configuration.
        investigator: Override the evidence-gathering callable (for tests). When
            omitted, a HybridRetrievePipeline-backed investigator is built iff
            ``config.investigate`` is True.
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

    # 4. Investigate — attach evidence to each hotspot.
    if investigator is None:
        investigator = (
            _default_investigator(config)
            if config.investigate
            else (lambda _hotspot: [])
        )

    findings: List[Finding] = []
    for hotspot in hotspots:
        evidence = investigator(hotspot)
        findings.append(
            Finding(
                kind="churn",
                title=f"High-churn file: {hotspot.path}",
                detail=(
                    f"Changed in **{hotspot.commit_count}** commits over "
                    f"{config.since}."
                ),
                evidence=evidence,
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
