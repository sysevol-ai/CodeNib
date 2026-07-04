# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Reusable evaluation helpers for agent-runner experiments."""

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
from .results import summarize_agent_result
from .scoring import AgentLocalizationEvaluation, evaluate_agent_localization
from .sweep import (
    LANG_GROUP_TO_KEY,
    all_index_skill_ids,
    load_dataset_rows,
    load_full_contexts,
    run_cell,
    scenario_for,
    slug,
)
from .sweep_config import SampleConfig, SweepConfig
from .symbols import (
    build_prebuilt_symbol_span_index,
    build_symbol_span_index,
    symbol_leaf,
)
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
    "FormatArmSummary",
    "FormatDiagnostics",
    "FeedbackGroup",
    "FeedbackPlan",
    "FeedbackPlanSpec",
    "GATE_SYSTEM",
    "GraphNav",
    "LANG_GROUP_TO_KEY",
    "PreloadQueryPlan",
    "SampleConfig",
    "SweepConfig",
    "Verdict",
    "VerifyExpandRun",
    "VERIFY_SYSTEM_PROMPT",
    "assemble_preload",
    "all_index_skill_ids",
    "analyze_pareto",
    "build_prebuilt_symbol_span_index",
    "build_symbol_span_index",
    "build_feedback_plan",
    "candidate_location",
    "converge_query",
    "default_agent_skills_dir",
    "evaluate_agent_localization",
    "expansion_seeds_from_candidates",
    "graph_verify",
    "load_graph_nav",
    "graph_compose",
    "interleave_ranked",
    "load_agent_skill_contexts",
    "load_cell_jsons",
    "load_dataset_rows",
    "load_format_diagnostics",
    "load_full_contexts",
    "load_meaningful_cells",
    "load_pareto_cells",
    "metric_at_k",
    "prepare_preload_query",
    "query_is_specific",
    "render_expansion",
    "retrieve_candidates",
    "run_cell",
    "run_verify_expand",
    "scatter_gather_localize",
    "safe_mean",
    "safe_min",
    "scenario_for",
    "snippet_for_node",
    "slug",
    "summarize_agent_result",
    "summarize_format_cells",
    "symbol_leaf",
    "usable_cells",
    "verdict_is_yes",
    "verify_query",
]
