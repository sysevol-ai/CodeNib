# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""OpenAI-compatible tool schemas for Guardian's L2 cycle loop."""

from __future__ import annotations


def _function(name: str, description: str, properties: dict, required=None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required or []),
                "additionalProperties": False,
            },
        },
    }


TOOLS = [
    _function(
        "list_signals",
        "Read deterministic repository measurements. Signals are not findings.",
        {
            "kind": {"type": "string", "description": "Optional signal kind filter."},
            "since": {
                "type": "integer",
                "description": "Optional zero-based signal offset for paging.",
            },
        },
    ),
    _function(
        "recall",
        (
            "Pull prior hypothesis trajectories from repository memory. "
            "The tool is present in every arm; it returns no records in memoryless."
        ),
        {
            "query": {"type": "string"},
            "kind": {
                "type": "string",
                "enum": ["semantic", "trajectory", "recent"],
            },
            "locus": {"type": "string"},
            "k": {"type": "integer", "minimum": 1, "maximum": 50},
        },
    ),
    _function(
        "search_code",
        (
            "Search symbol names and ranked source lines for files, identifiers, "
            "callers, tests, or behavior. Multi-term queries match individual terms."
        ),
        {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        required=["query"],
    ),
    _function(
        "read_commit_diff",
        (
            "Read the bounded patch introduced by the reviewed HEAD commit. "
            "Use it before exploring unrelated repository-wide problems."
        ),
        {},
    ),
    _function(
        "read_code",
        "Read a bounded line range from a repository file.",
        {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        required=["path"],
    ),
    _function(
        "read_observation",
        (
            "Read a bounded slice of a full tool observation that was replaced "
            "in context by a compact observation reference."
        ),
        {
            "ref": {"type": "string"},
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Zero-based character offset. Defaults to 0.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 16000,
                "description": "Maximum characters to return. Defaults to 4000.",
            },
        },
        required=["ref"],
    ),
    _function(
        "write_hypothesis",
        "Create a complete falsifiable and solvable hypothesis.",
        {
            "claim": {"type": "string"},
            "consequence": {"type": "string"},
            "remedy": {"type": "string"},
            "origin": {
                "type": "string",
                "enum": ["signal", "memory", "exploration", "human"],
            },
            "locus": {"type": "array", "items": {"type": "string"}},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "supersedes": {"type": "array", "items": {"type": "string"}},
        },
        required=["claim", "consequence", "remedy", "origin", "locus", "evidence"],
    ),
    _function(
        "update_hypothesis",
        (
            "Update one hypothesis after evidence or resolution work. For a "
            "simple closed-form source claim, source_evidence lets L2 cite exact "
            "spans directly without invoking L3."
        ),
        {
            "id": {"type": "string"},
            "grade": {
                "type": "string",
                "enum": [
                    "conjecture",
                    "supported",
                    "finding",
                    "refuted",
                    "deferred",
                    "resolved",
                ],
            },
            "evidence": {"type": "array", "items": {"type": "string"}},
            "source_evidence": {
                "type": "array",
                "description": (
                    "Exact spans that conclusively establish a simple claim. "
                    "The runtime validates these spans and creates admissible "
                    "source-valid evidence references."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                        "description": {
                            "type": "string",
                            "description": (
                                "Closed-form explanation of what the cited span "
                                "establishes."
                            ),
                        },
                    },
                    "required": [
                        "path",
                        "start_line",
                        "end_line",
                        "description",
                    ],
                    "additionalProperties": False,
                },
            },
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "remedy": {"type": "string"},
            "supersedes": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
        required=["id", "reason"],
    ),
    _function(
        "investigate",
        (
            "Optionally delegate one hypothesis to L3 when source inspection "
            "alone cannot establish or refute it, such as runtime, integration, "
            "historical, or environment-dependent behavior."
        ),
        {
            "hypothesis_id": {"type": "string"},
            "budget_tokens": {
                "type": "integer",
                "minimum": 8000,
                "description": "L3 grants below 8000 cannot fund its tool protocol.",
            },
            "obligation": {
                "type": "string",
                "description": "One narrow falsifiable question for this L3 run.",
            },
        },
        required=["hypothesis_id", "budget_tokens", "obligation"],
    ),
    _function(
        "submit_report",
        (
            "End the cycle after re-grounding the current explanation and "
            "challenging its important weak beliefs."
        ),
        {
            "summary": {"type": "string"},
            "understanding": {
                "type": "string",
                "description": "Current coherent explanation of repository behavior.",
            },
            "open_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Important residual uncertainties, empty when none remain.",
            },
        },
        required=["summary", "understanding", "open_questions"],
    ),
]

TOOL_NAMES = frozenset(item["function"]["name"] for item in TOOLS)

# Only tools whose dispatch is side-effect free and independent of mutable
# Guardian collaborators may share a worker batch. In particular, retrieval,
# memory access, hypothesis state, L3, and submission remain ordering barriers.
PARALLEL_SAFE_TOOL_NAMES = frozenset(
    {
        "list_signals",
        "read_code",
        "read_commit_diff",
        "read_observation",
    }
)

__all__ = ["TOOLS", "TOOL_NAMES", "PARALLEL_SAFE_TOOL_NAMES"]
