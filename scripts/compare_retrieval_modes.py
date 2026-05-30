#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Compare hybrid_baseline vs agent A2 retrieval modes.

Loads JSON reports from skill_agent_eval.py and generates comparison tables
for RFC #133 Phase 0/2 analysis.

Usage:
    python scripts/compare_retrieval_modes.py \\
        --baseline results/car_phase0_hybrid_baseline.json \\
        --agent results/car_phase0_agent_a2.json \\
        --output results/comparison.md
"""

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List


def load_report(path: str) -> Dict[str, Any]:
    """Load skill_agent_eval.py JSON report."""
    with open(path) as f:
        return json.load(f)


def extract_metrics(report: Dict[str, Any], mode_name: str) -> Dict[str, Any]:
    """Extract key metrics from a report."""
    agg = report.get("aggregate_metrics", {})
    row = {"mode": mode_name}

    # Accuracy @ K for files and symbols
    for scope in ["files", "symbols"]:
        scope_metrics = agg.get(scope, {})
        for k in ["1", "3", "5", "10"]:
            stats = scope_metrics.get(k, {})
            row[f"{scope}@{k}_acc"] = stats.get("accuracy", 0.0)
            row[f"{scope}@{k}_recall"] = stats.get("recall", 0.0)
            row[f"{scope}@{k}_prec"] = stats.get("precision", 0.0)

    # Usage stats (agent only)
    total_usage = report.get("total_usage")
    if total_usage:
        row["total_tokens"] = total_usage.get("total_tokens", 0)
        row["cost_usd"] = total_usage.get("cost_usd", 0.0)
    else:
        row["total_tokens"] = 0
        row["cost_usd"] = 0.0

    # Average wall time per instance
    results = report.get("results", [])
    if results:
        total_time = sum(
            r.get("elapsed_seconds", 0) for r in results if r.get("success")
        )
        row["avg_time_sec"] = total_time / len(results)
        row["success_rate"] = sum(1 for r in results if r.get("success")) / len(results)
    else:
        row["avg_time_sec"] = 0.0
        row["success_rate"] = 0.0

    # Instance count
    row["instance_count"] = report.get("instance_count", len(results))

    return row


def format_markdown_table(rows: List[Dict[str, Any]]) -> str:
    """Format comparison as markdown table."""
    if not rows:
        return ""

    # Key metrics to display
    headers = [
        "Mode",
        "files@5",
        "symbols@5",
        "Avg Time (s)",
        "Tokens",
        "Cost (USD)",
        "Success Rate",
    ]

    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")

    for row in rows:
        cells = [
            row["mode"],
            f"{row.get('files@5_acc', 0.0):.3f}",
            f"{row.get('symbols@5_acc', 0.0):.3f}",
            f"{row.get('avg_time_sec', 0.0):.1f}",
            f"{row.get('total_tokens', 0):,}",
            f"${row.get('cost_usd', 0.0):.2f}",
            f"{row.get('success_rate', 0.0):.1%}",
        ]
        lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(lines)


def format_detailed_table(rows: List[Dict[str, Any]]) -> str:
    """Detailed accuracy breakdown by k."""
    if not rows:
        return ""

    lines = []
    for row in rows:
        lines.append(f"\n## {row['mode']}\n")
        lines.append("### Files")
        lines.append("| K | Accuracy | Recall | Precision |")
        lines.append("|---|----------|--------|-----------|")
        for k in ["1", "3", "5", "10"]:
            lines.append(
                f"| {k} | {row.get(f'files@{k}_acc', 0.0):.3f} | "
                f"{row.get(f'files@{k}_recall', 0.0):.3f} | "
                f"{row.get(f'files@{k}_prec', 0.0):.3f} |"
            )

        lines.append("\n### Symbols")
        lines.append("| K | Accuracy | Recall | Precision |")
        lines.append("|---|----------|--------|-----------|")
        for k in ["1", "3", "5", "10"]:
            lines.append(
                f"| {k} | {row.get(f'symbols@{k}_acc', 0.0):.3f} | "
                f"{row.get(f'symbols@{k}_recall', 0.0):.3f} | "
                f"{row.get(f'symbols@{k}_prec', 0.0):.3f} |"
            )

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Compare hybrid_baseline vs agent A2 retrieval modes"
    )
    parser.add_argument(
        "--baseline",
        type=str,
        required=True,
        help="Path to hybrid_baseline JSON report",
    )
    parser.add_argument(
        "--agent", type=str, required=True, help="Path to agent A2 JSON report"
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output markdown file (default: print to stdout)",
    )
    args = parser.parse_args()

    # Load reports
    baseline_report = load_report(args.baseline)
    agent_report = load_report(args.agent)

    # Extract metrics
    baseline_metrics = extract_metrics(baseline_report, "hybrid_baseline")
    agent_metrics = extract_metrics(agent_report, "agent_a2")

    # Generate comparison
    output = []
    output.append("# Retrieval Mode Comparison\n")
    output.append(f"Baseline: `{args.baseline}`")
    output.append(f"Agent: `{args.agent}`\n")

    output.append("## Summary\n")
    output.append(format_markdown_table([baseline_metrics, agent_metrics]))

    output.append("\n## Detailed Metrics\n")
    output.append(format_detailed_table([baseline_metrics, agent_metrics]))

    # Analysis
    output.append("\n## Analysis\n")
    files_delta = agent_metrics["files@5_acc"] - baseline_metrics["files@5_acc"]
    symbols_delta = agent_metrics["symbols@5_acc"] - baseline_metrics["symbols@5_acc"]
    time_ratio = (
        agent_metrics["avg_time_sec"] / baseline_metrics["avg_time_sec"]
        if baseline_metrics["avg_time_sec"] > 0
        else 0
    )

    output.append(f"- **Accuracy gain (files@5)**: {files_delta:+.3f}")
    output.append(f"- **Accuracy gain (symbols@5)**: {symbols_delta:+.3f}")
    output.append(f"- **Time overhead**: {time_ratio:.1f}x")
    output.append(
        f"- **Token cost**: {agent_metrics['total_tokens']:,} "
        f"(${agent_metrics['cost_usd']:.2f})"
    )

    if files_delta > 0.03:
        output.append(
            "\n**Conclusion**: Agent A2 shows meaningful accuracy improvement "
            "over heuristic baseline."
        )
    elif files_delta > 0:
        output.append(
            "\n**Conclusion**: Agent A2 shows marginal accuracy improvement. "
            "Cost/benefit trade-off unclear."
        )
    else:
        output.append(
            "\n**Conclusion**: Agent A2 does NOT improve over heuristic baseline. "
            "LLM tool-calling adds cost without accuracy gain."
        )

    result = "\n".join(output)

    # Write or print
    if args.output:
        Path(args.output).write_text(result)
        print(f"Comparison written to {args.output}")
    else:
        print(result)


if __name__ == "__main__":
    main()
