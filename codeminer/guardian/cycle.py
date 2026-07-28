# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Host boundary around Repository Guardian's stateful L2 agent loop."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional, Sequence

from ..log_utils import get_logger
from .loop import CycleState, Signal, StateInconsistent
from .loop.agent import LoopContext, run_cycle_agent
from .report import GuardianReport, report_views
from .signals import churn_hotspots, run_test_suite

logger = get_logger(__name__)


def _analysis_status(exit_reason: Optional[str], *, degraded: bool) -> str:
    """Map a typed loop exit to the public review-completion contract."""

    if degraded:
        return "degraded"
    if exit_reason == "ReportSubmitted":
        return "complete"
    if exit_reason == "StateInconsistent":
        return "failed"
    return "incomplete"


@dataclass
class GuardianConfig:
    """Configuration for one commit-scoped L2 cycle."""

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
    memory_dir: Optional[str] = None
    arm: str = "memory"
    graph_snapshot: bool = True
    budget_tokens: Optional[int] = 100_000
    max_investigator_rounds: int = 8
    hypotheses_only: bool = False
    max_outer_turns: int = 40
    wall_clock_seconds: int = 1_800
    max_context_tokens: int = 200_000
    context_reserve_tokens: int = 20_000
    max_context_chars: Optional[int] = None
    checkpoint_path: Optional[str] = None
    message_inbox_path: Optional[str] = None
    max_external_messages: int = 20
    resume: bool = False


def _current_commit(repo_path: str) -> str:
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


@contextlib.contextmanager
def _episode_log_handler(
    episode_dir: Optional[str],
) -> Generator[None, None, None]:
    if not episode_dir:
        yield
        return
    from ..log_utils import logging_manager

    os.makedirs(episode_dir, exist_ok=True)
    previous_mode = logging_manager.mode
    previous_dir = logging_manager.current_log_dir
    previous_filename = logging_manager.unified_log_filename
    logging_manager.set_unified_log_filename("guardian.log")
    logging_manager.set_log_dir(episode_dir)
    logging_manager.switch_to_both()
    try:
        yield
    finally:
        logging_manager.unified_log_filename = previous_filename
        logging_manager.current_log_dir = previous_dir
        if previous_mode == "stdout":
            logging_manager.switch_to_stdout()
        elif previous_mode in ("file", "both") and previous_dir:
            logging_manager.set_log_dir(previous_dir)
            if previous_mode == "file":
                logging_manager.switch_to_file()
            else:
                logging_manager.switch_to_both()
        else:
            logging_manager.switch_to_stdout()


def _compile_index(config: GuardianConfig):
    from ..compiler import IndexCompiler, IndexCompilerConfig
    from ..compiler.index_builders import (
        IndexBuilderRegistry,
        register_default_builders,
    )

    registry = IndexBuilderRegistry()
    register_default_builders(
        registry,
        languages=list(config.languages),
        embedding_model=config.embedding_model,
        embedding_dimension=config.embedding_dimension,
    )
    return IndexCompiler(
        registry,
        IndexCompilerConfig(
            index_types=list(config.index_types),
            languages=list(config.languages),
        ),
    ).compile_repo(config.repo_path, cache_dir=config.index_cache_dir)


class _NullRetriever:
    def query(self, query: str, top_k: Optional[int] = None) -> list:
        del query, top_k
        return []


class _BM25Adapter:
    def __init__(self, index: object) -> None:
        self._index = index

    def query(self, query: str, top_k: Optional[int] = None) -> list:
        return self._index.search(query, top_k=top_k)  # type: ignore[union-attr]


def _load_bm25_from_manifest(manifest: object) -> Optional[object]:
    try:
        from ..index.sparse_idx.bm25_index import BM25CodeIndexer

        entry = (getattr(manifest, "indexes", None) or {}).get("bm25")
        if entry is None or getattr(entry, "status", None) != "fresh":
            return None
        artifact_path = getattr(entry, "artifact_path", None)
        if not artifact_path:
            return None
        return _BM25Adapter(BM25CodeIndexer.load(artifact_path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Guardian: failed to load BM25 artifact: %s", exc)
        return None


def _load_current_graph(manifest: object) -> Optional[object]:
    try:
        from ..graph.code_graph import CodeGraph

        entry = (getattr(manifest, "indexes", None) or {}).get("symbol_graph")
        artifact_path = getattr(entry, "artifact_path", None) if entry else None
        if not artifact_path:
            return None
        return CodeGraph.load(artifact_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Guardian: failed to load symbol graph: %s", exc)
        return None


def _make_llm(config: GuardianConfig) -> object:
    if config.llm_model.startswith("codex:"):
        import importlib.util

        model = config.llm_model.split(":", 1)[1].strip()
        if not model:
            raise ValueError("codex: Guardian model requires a Codex model name")
        transport = os.environ.get("CODEMINER_CODEX_TRANSPORT", "").strip().lower()
        if (
            transport not in {"cli", "codex-cli"}
            and importlib.util.find_spec("openai_codex") is not None
        ):
            from .llm.codex_cli_chat import CodexSdkChat

            return CodexSdkChat(model=model, cwd=config.repo_path)
        from .llm.codex_cli_chat import CodexCliChat

        return CodexCliChat(model=model, cwd=config.repo_path)
    from ..llm.litellm_chat import LiteLLMChat

    return LiteLLMChat(model=config.llm_model, temperature=0.0, max_tokens=1024)


def _llm_report_metadata(config: GuardianConfig, llm: object) -> dict:
    if llm is None:
        return {"llm_model": "", "llm_backend": "", "llm_transport_history": []}
    return {
        "llm_model": config.llm_model,
        "llm_backend": getattr(llm, "backend_name", "") or "litellm",
        "llm_transport_history": list(
            getattr(llm, "transport_history", None)
            or [getattr(llm, "backend_name", "") or "litellm"]
        ),
    }


def _merge_llm_usage(total: object, *parts: object) -> None:
    """Merge LLM usage accumulators for compatibility consumers."""
    total.prompt_tokens = sum(getattr(part, "prompt_tokens", 0) or 0 for part in parts)
    total.cached_input_tokens = sum(
        getattr(part, "cached_input_tokens", 0) or 0 for part in parts
    )
    total.completion_tokens = sum(
        getattr(part, "completion_tokens", 0) or 0 for part in parts
    )
    # Keep the rendered accounting identity exact even when a provider reports
    # a legacy total that omits separately reported reasoning tokens.
    total.total_tokens = total.prompt_tokens + total.completion_tokens


def _build_retriever(config: GuardianConfig, manifest: object) -> object:
    if "vector" in config.index_types:
        try:
            from ..model.hybrid_retrieve_pipeline import HybridRetrievePipeline

            return HybridRetrievePipeline(
                repo_path=config.repo_path,
                index_cache_dir=config.index_cache_dir,
                languages=tuple(config.languages),
                embedding_model=config.embedding_model,
                embedding_dimension=config.embedding_dimension,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Guardian: hybrid retriever unavailable: %s", exc)
    return _load_bm25_from_manifest(manifest) or _NullRetriever()


def _collect_signals(
    config: GuardianConfig,
    *,
    prior_graph: object,
    current_graph: object,
) -> tuple[list[Signal], list]:
    signals: list[Signal] = []
    edge_changes: list = []
    signals.extend(
        churn_hotspots(config.repo_path, since=config.since, top_n=config.top_n)
    )
    if prior_graph is not None and current_graph is not None:
        from .signals.graph_diff import compute_drift_signals, diff_graphs

        edge_changes = diff_graphs(prior_graph, current_graph)
        signals.extend(compute_drift_signals(edge_changes, current_graph, prior_graph))
    return signals, edge_changes


def run_cycle(
    config: GuardianConfig,
    *,
    manifest: object = None,
) -> GuardianReport:
    """Run one cycle; code owns boundaries and the model owns decisions."""
    with _episode_log_handler(config.episode_dir):
        return _run_cycle_inner(config, manifest=manifest)


def _run_cycle_inner(
    config: GuardianConfig,
    *,
    manifest: object = None,
) -> GuardianReport:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    # Freeze external input before indexing or model work. Messages arriving
    # after this boundary are deliberately deferred to the next commit cycle.
    external_messages: list[dict] = []
    if config.message_inbox_path:
        from .interaction import MessageInbox

        external_messages = [
            item.to_dict()
            for item in MessageInbox(
                config.message_inbox_path, repo_path=config.repo_path
            ).read_recent(limit=config.max_external_messages)
        ]
    if manifest is None:
        manifest = _compile_index(config)
    commit = getattr(manifest, "commit", "") or _current_commit(config.repo_path)
    file_count = int(getattr(manifest, "file_count", 0) or 0)
    capabilities = dict(getattr(manifest, "capabilities", {}) or {})

    store = None
    prior_graph = None
    cycle_no = 1
    carried_hypotheses = []
    prior_understanding = ""
    prior_open_questions: list[str] = []
    if config.memory_dir:
        from .memory import MemoryStore

        store = MemoryStore(config.memory_dir, readonly=(config.arm == "memoryless"))
        if config.arm != "memoryless":
            cycle_no = store.next_cycle_no()
            carried_hypotheses = store.load_hypotheses()
            prior_graph = store.load_prior_graph(config.memory_dir)
            prior_understanding, prior_open_questions = (
                store.load_review_understanding()
            )

    current_graph = _load_current_graph(manifest)
    signals, edge_changes = _collect_signals(
        config, prior_graph=prior_graph, current_graph=current_graph
    )
    test_result = run_test_suite(config.repo_path) if config.run_tests else None
    if test_result is not None and test_result.ran:
        signals.extend(test_result.failures)

    state = CycleState(
        cycle_no=cycle_no,
        commit=commit,
        hypotheses=carried_hypotheses,
        signals=signals,
        current=None,
        budget_total=(
            None if config.budget_tokens is None else max(0, config.budget_tokens)
        ),
        budget_spent=0,
        decision_log=[],
        exit_reason=None,
        carried_from=(cycle_no - 1 if cycle_no > 1 else None),
        understanding=prior_understanding,
        open_questions=prior_open_questions,
        external_messages=external_messages,
    )

    from .investigator import CurrentSnapshotSandbox, LLMUsage, PriorSnapshotSandbox

    outer_usage = LLMUsage()
    inner_usage = LLMUsage()
    llm = _make_llm(config) if config.use_llm else None
    output_root = Path(
        config.episode_dir
        or (
            Path(config.memory_dir) / "out"
            if config.memory_dir
            else Path(tempfile.gettempdir()) / "codeminer-guardian" / commit[:12]
        )
    )
    if config.episode_dir:
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "external_messages.json").write_text(
            json.dumps(external_messages, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    checkpoint = Path(config.checkpoint_path or output_root / "cycle_state.json")

    if config.resume and checkpoint.exists():
        from .loop import load_checkpoint

        resumed, messages = load_checkpoint(checkpoint)
        if resumed.commit != commit:
            raise StateInconsistent(
                "checkpoint commit does not match current repository commit"
            )
        state = resumed
    else:
        messages = None

    if llm is None:
        state.degraded = True
        state.exit_reason = "Degraded"
        state.report_summary = "No model configured; no agent decisions were scored."
        state.decision_log.append(
            {
                "event": "exit",
                "exit_type": "Degraded",
                "reason": state.report_summary,
            }
        )
        from .loop import save_checkpoint

        save_checkpoint(checkpoint, state, [])
    else:
        prior_sandbox = None
        current_sandbox = None
        try:
            prior_sandbox = PriorSnapshotSandbox(config.repo_path)
        except ValueError:
            logger.info(
                "Guardian: no prior git snapshot available for differential probes"
            )
        try:
            current_sandbox = CurrentSnapshotSandbox(config.repo_path)
            state = run_cycle_agent(
                state,
                LoopContext(
                    repo_path=config.repo_path,
                    arm=config.arm,
                    llm=llm,
                    retriever=_build_retriever(config, manifest),
                    memory_store=store,
                    sandbox=current_sandbox,
                    prior_sandbox=prior_sandbox,
                    checkpoint_path=checkpoint,
                    observation_dir=output_root / "observations",
                    max_context_tokens=config.max_context_tokens,
                    context_reserve_tokens=config.context_reserve_tokens,
                    max_context_chars=config.max_context_chars,
                    max_turns=(1 if config.hypotheses_only else config.max_outer_turns),
                    wall_clock_seconds=config.wall_clock_seconds,
                    max_investigator_rounds=config.max_investigator_rounds,
                    inner_usage=inner_usage,
                    outer_usage=outer_usage,
                ),
                messages=messages,
            )
        finally:
            if prior_sandbox is not None:
                prior_sandbox.close()
            if current_sandbox is not None:
                current_sandbox.close()

    # Model response usage is carried in the state ledger. Keep the established
    # report fields for dashboards while the decision log remains authoritative.
    total_usage = LLMUsage()
    _merge_llm_usage(total_usage, outer_usage, inner_usage)
    findings, backlog, retractions = report_views(state.hypotheses)
    tool_calls = [row for row in state.decision_log if row.get("event") == "tool_call"]
    investigation_budgets = [
        int(row.get("arguments", {}).get("budget_tokens", 0) or 0)
        for row in tool_calls
        if row.get("tool") == "investigate"
    ]
    no_signal = [
        hypothesis
        for hypothesis in state.hypotheses
        if not any(ref.startswith("signal:") for ref in hypothesis.evidence)
    ]
    trace_metrics = {
        "budget_spent": state.budget_spent,
        "environment_failures": sum(
            "environment_unavailable" in str(row.get("observation", ""))
            for row in tool_calls
            if row.get("tool") == "investigate"
        ),
        "degraded_investigations": sum(
            '"degraded": true' in str(row.get("observation", "")).lower()
            for row in tool_calls
            if row.get("tool") == "investigate"
        ),
        "tool_calls": len(tool_calls),
        "recall_calls": sum(row.get("tool") == "recall" for row in tool_calls),
        "resolved_hypotheses": sum(
            hypothesis.grade == "resolved" for hypothesis in state.hypotheses
        ),
        "investigation_budgets": investigation_budgets,
        "no_signal_hypothesis_fraction": (
            len(no_signal) / len(state.hypotheses) if state.hypotheses else 0.0
        ),
        "proactive_findings": sum(
            hypothesis.grade == "finding"
            and hypothesis.origin in {"memory", "exploration"}
            and not any(ref.startswith("signal:") for ref in hypothesis.evidence)
            for hypothesis in state.hypotheses
        ),
    }
    report = GuardianReport(
        repo=config.repo_path,
        commit=commit,
        generated_at=generated_at,
        churn_window=config.since,
        file_count=file_count,
        capabilities=capabilities,
        findings=findings,
        backlog=backlog,
        retractions=retractions,
        tests_ran=bool(test_result and test_result.ran),
        tests_summary=(test_result.summary if test_result else ""),
        llm_usage=total_usage,
        outer_llm_usage=outer_usage,
        inner_llm_usage=inner_usage,
        cycle_no=state.cycle_no,
        exit_reason=state.exit_reason or "",
        report_summary=state.report_summary,
        understanding=state.understanding,
        open_questions=state.open_questions,
        external_message_ids=[
            str(item.get("id", "")) for item in state.external_messages
        ],
        degraded=state.degraded,
        analysis_status=_analysis_status(
            state.exit_reason,
            degraded=state.degraded,
        ),
        decision_log=state.decision_log,
        compaction_events=state.compaction_events,
        trace_metrics=trace_metrics,
        retriever=("hybrid" if "vector" in config.index_types else "bm25"),
        **_llm_report_metadata(config, llm),
    )

    # A corrupt state is never persisted. Every other typed exit is useful
    # trajectory data, including Degraded and NoProgress.
    if store is not None and state.exit_reason != "StateInconsistent":
        persisted_cycle = store.persist_state(
            state,
            report,
            graph=(current_graph if config.graph_snapshot else None),
            memory_dir=config.memory_dir,
        )
        if edge_changes and persisted_cycle > 0:
            store.persist_edge_changes(persisted_cycle, edge_changes)
    return report
