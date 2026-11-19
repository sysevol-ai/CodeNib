# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Reusable evaluation helpers exposed without eager optional imports."""

from __future__ import annotations

from typing import Any

from codenib._lazy import exported_dir, load_export


def _module_exports(module: str, *names: str) -> dict[str, tuple[str, str]]:
    qualified = f"{__name__}.{module}"
    return {name: (qualified, name) for name in names}


_EXPORTS = {
    **_module_exports(
        "scoring",
        "AgentLocalizationEvaluation",
        "evaluate_agent_localization",
    ),
    **_module_exports(
        "contexts",
        "AgentSkillContextSpec",
        "default_agent_skills_dir",
        "load_agent_skill_contexts",
    ),
    **_module_exports(
        "trace_summary",
        "AgentTraceSummary",
        "summarize_agent_trace",
    ),
    **_module_exports(
        "baseline",
        "BaselineLocation",
        "BaselineRunResult",
        "BaselineTask",
        "build_baseline_result_entry",
        "location_symbol_ids",
        "score_baseline_predictions",
        "unique_location_files",
    ),
    **_module_exports(
        "batch",
        "BaselineBatchSummary",
        "BaselineTaskRunner",
        "baseline_done_ids",
        "run_baseline_batch",
    ),
    **_module_exports(
        "orchestrator",
        "GATE_SYSTEM",
        "VERIFY_SYSTEM_PROMPT",
        "candidate_location",
        "converge_query",
        "query_is_specific",
        "scatter_gather_localize",
        "verdict_is_yes",
        "verify_query",
    ),
    **_module_exports(
        "verify_expand",
        "GraphNav",
        "Verdict",
        "VerifyExpandRun",
        "expansion_seeds_from_candidates",
        "graph_verify",
        "load_graph_nav",
        "render_expansion",
        "run_verify_expand",
    ),
    **_module_exports(
        "sweep",
        "LANG_GROUP_TO_KEY",
        "all_index_skill_ids",
        "load_dataset_rows",
        "load_full_contexts",
        "run_cell",
        "run_sweep",
        "scenario_for",
        "slug",
        "validate_sweep_harness",
    ),
    **_module_exports(
        "lsp_agent_study",
        "CODENIB_LSP_ARM",
        "DEFAULT_ARMS",
        "FILESYSTEM_ARM",
        "LIVE_LSP_ARM",
        "LSPAgentStudySpec",
        "LSPAgentStudyTask",
        "run_lsp_agent_study_arm",
        "study_arm_order",
    ),
    **_module_exports(
        "lsp_provider_validation",
        "LSPProviderCall",
        "LSPProviderComparison",
        "LSPProviderRequest",
        "LSPProviderValidationSummary",
        "compare_static_lsp_provider",
        "default_lsp_provider_fingerprint",
        "fingerprint_lsp_start_location_set",
        "fingerprint_lsp_start_locations",
        "render_lsp_provider_validation_markdown",
        "summarize_lsp_provider_validation",
    ),
    **_module_exports(
        "live_lsp_provider",
        "LiveLSPReferenceProvider",
        "compare_static_to_live_lsp_provider",
        "lsp_locations_to_nodes",
    ),
    **_module_exports(
        "preload",
        "PreloadQueryPlan",
        "assemble_preload",
        "graph_compose",
        "interleave_ranked",
        "prepare_preload_query",
        "retrieve_candidates",
        "snippet_for_node",
    ),
    **_module_exports(
        "sweep_config",
        "SampleConfig",
        "SweepConfig",
    ),
    **_module_exports(
        "lsp_agent_study_analysis",
        "analyze_lsp_agent_noninferiority",
        "collect_live_lsp_replay_bundles",
        "summarize_lsp_agent_study",
    ),
    **_module_exports(
        "symbols",
        "build_prebuilt_symbol_span_index",
        "build_symbol_span_index",
        "symbol_leaf",
    ),
    **_module_exports(
        "lsp_agent_study_manifest",
        "build_base_lsp_agent_manifest",
        "task_from_manifest_subject",
    ),
    **_module_exports(
        "lsp_replay_benchmark",
        "exit_code_for_lsp_replay_benchmark",
        "generate_lsp_replay_requests",
        "render_lsp_replay_benchmark_markdown",
        "summarize_lsp_replay_benchmark",
        "run_lsp_replay_benchmark",
    ),
    **_module_exports(
        "metrics",
        "load_cell_jsons",
        "metric_at_k",
        "safe_mean",
        "safe_min",
        "usable_cells",
    ),
    **_module_exports(
        "results",
        "summarize_agent_result",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    return load_export(globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return exported_dir(globals(), _EXPORTS)
