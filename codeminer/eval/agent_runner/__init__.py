# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Reusable evaluation helpers for agent-runner experiments."""

from .baseline import (
    BaselineLocation,
    BaselineRunResult,
    BaselineTask,
    build_baseline_result_entry,
    location_symbol_ids,
    score_baseline_predictions,
    unique_location_files,
)
from .batch import (
    BaselineBatchSummary,
    BaselineTaskRunner,
    baseline_done_ids,
    run_baseline_batch,
)
from .contexts import (
    AgentSkillContextSpec,
    default_agent_skills_dir,
    load_agent_skill_contexts,
)
from .feedback import FeedbackGroup, FeedbackPlan, FeedbackPlanSpec, build_feedback_plan
from .format_diagnostics import (
    FormatArmSummary,
    FormatDiagnostics,
    load_format_diagnostics,
    load_meaningful_cells,
    summarize_format_cells,
)
from .lsp_baseline import (
    LSPRouteBaselineConfig,
    build_lsp_route_result_entry,
    extract_lsp_symbol_seeds,
    load_prebuilt_lsp_graph,
    locations_from_lsp_nodes,
    run_lsp_route_baseline,
)
from .metrics import load_cell_jsons, metric_at_k, safe_mean, safe_min, usable_cells
from .orchestrator import (
    GATE_SYSTEM,
    VERIFY_SYSTEM_PROMPT,
    candidate_location,
    converge_query,
    query_is_specific,
    scatter_gather_localize,
    verdict_is_yes,
    verify_query,
)
from .pareto import analyze_pareto, load_pareto_cells
from .preload import (
    PreloadQueryPlan,
    assemble_preload,
    graph_compose,
    interleave_ranked,
    prepare_preload_query,
    retrieve_candidates,
    snippet_for_node,
)
from .promotion import (
    FAILURE_CATEGORIES,
    EvidenceSlice,
    PromotionEvidence,
    PromotionGateResult,
    evaluate_promotion_evidence,
)
from .results import summarize_agent_result
from .scoring import AgentLocalizationEvaluation, evaluate_agent_localization
from .sweep import (
    LANG_GROUP_TO_KEY,
    all_index_skill_ids,
    load_dataset_rows,
    load_full_contexts,
    run_cell,
    run_sweep,
    scenario_for,
    slug,
    validate_sweep_harness,
)
from .sweep_config import SampleConfig, SweepConfig
from .symbols import (
    build_prebuilt_symbol_span_index,
    build_symbol_span_index,
    symbol_leaf,
)
from .trace_summary import AgentTraceSummary, summarize_agent_trace
from .verify_expand import (
    GraphNav,
    Verdict,
    VerifyExpandRun,
    expansion_seeds_from_candidates,
    graph_verify,
    load_graph_nav,
    render_expansion,
    run_verify_expand,
)

__all__ = [
    "AgentLocalizationEvaluation",
    "AgentSkillContextSpec",
    "AgentTraceSummary",
    "BaselineLocation",
    "BaselineRunResult",
    "BaselineBatchSummary",
    "BaselineTaskRunner",
    "BaselineTask",
    "EvidenceSlice",
    "FormatArmSummary",
    "FormatDiagnostics",
    "FeedbackGroup",
    "FeedbackPlan",
    "FeedbackPlanSpec",
    "GATE_SYSTEM",
    "GraphNav",
    "LANG_GROUP_TO_KEY",
    "LSPRouteBaselineConfig",
    "FAILURE_CATEGORIES",
    "PreloadQueryPlan",
    "PromotionEvidence",
    "PromotionGateResult",
    "SampleConfig",
    "SweepConfig",
    "Verdict",
    "VerifyExpandRun",
    "VERIFY_SYSTEM_PROMPT",
    "assemble_preload",
    "all_index_skill_ids",
    "analyze_pareto",
    "baseline_done_ids",
    "build_prebuilt_symbol_span_index",
    "build_symbol_span_index",
    "build_feedback_plan",
    "build_baseline_result_entry",
    "build_lsp_route_result_entry",
    "candidate_location",
    "converge_query",
    "default_agent_skills_dir",
    "evaluate_agent_localization",
    "evaluate_promotion_evidence",
    "expansion_seeds_from_candidates",
    "extract_lsp_symbol_seeds",
    "graph_verify",
    "load_graph_nav",
    "graph_compose",
    "interleave_ranked",
    "load_agent_skill_contexts",
    "load_cell_jsons",
    "load_dataset_rows",
    "load_format_diagnostics",
    "load_full_contexts",
    "load_prebuilt_lsp_graph",
    "locations_from_lsp_nodes",
    "location_symbol_ids",
    "load_meaningful_cells",
    "load_pareto_cells",
    "metric_at_k",
    "prepare_preload_query",
    "query_is_specific",
    "render_expansion",
    "retrieve_candidates",
    "run_baseline_batch",
    "run_cell",
    "run_sweep",
    "run_lsp_route_baseline",
    "run_verify_expand",
    "scatter_gather_localize",
    "safe_mean",
    "safe_min",
    "score_baseline_predictions",
    "scenario_for",
    "snippet_for_node",
    "slug",
    "summarize_agent_result",
    "summarize_agent_trace",
    "summarize_format_cells",
    "symbol_leaf",
    "unique_location_files",
    "usable_cells",
    "validate_sweep_harness",
    "verdict_is_yes",
    "verify_query",
]
