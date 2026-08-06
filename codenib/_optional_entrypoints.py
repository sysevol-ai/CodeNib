# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Console entry points whose implementations need optional dependencies."""

from __future__ import annotations

import sys
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from shlex import quote
from typing import Callable

_OPTIONAL_IMPORT_ROOTS = {
    "mcp": frozenset({"mcp"}),
    "graph": frozenset({"google", "igraph"}),
    "full": frozenset(
        {
            "datasets",
            "einops",
            "faiss",
            "google",
            "igraph",
            "litellm",
            "numpy",
            "openai",
            "requests",
            "sentence_transformers",
        }
    ),
}


def _installed_requirement(extra: str) -> str:
    try:
        installed_version = version("codenib")
    except PackageNotFoundError:
        return f"codenib[{extra}]"
    return f"codenib[{extra}]=={installed_version}"


def _load_main(module_name: str) -> Callable[[], int | None]:
    return import_module(module_name).main


def _invoked_command(default: str) -> str:
    invoked = Path(sys.argv[0]).name
    return invoked if invoked.startswith("codenib-") else default


def _missing_optional_dependency(
    *,
    command: str,
    extra: str,
    missing: str,
) -> int:
    requirement = _installed_requirement(extra)
    message = (
        f"{command} requires optional CodeNib dependencies (missing: {missing}).\n"
        f"Install them with:\n  python -m pip install {quote(requirement)}"
    )
    if {"-h", "--help"}.intersection(sys.argv[1:]):
        print(f"usage: {command} [options]\n\n{message}")
        return 0
    print(f"{command}: error: {message}", file=sys.stderr)
    return 2


def _run_optional(module_name: str, *, command: str, extra: str) -> int | None:
    command = _invoked_command(command)
    try:
        main = _load_main(module_name)
        return main()
    except ModuleNotFoundError as exc:
        missing = exc.name or "unknown module"
        root = missing.partition(".")[0]
        if root not in _OPTIONAL_IMPORT_ROOTS[extra]:
            raise
        return _missing_optional_dependency(
            command=command,
            extra=extra,
            missing=missing,
        )


def mcp_main() -> int | None:
    return _run_optional(
        "codenib.mcp.server",
        command="codenib-mcp",
        extra="mcp",
    )


def lsp_provider_validate_main() -> int | None:
    return _run_optional(
        "codenib.eval.agent_runner.lsp_provider_cli",
        command="codenib-lsp-provider-validate",
        extra="graph",
    )


def lsp_replay_benchmark_main() -> int | None:
    return _run_optional(
        "codenib.eval.agent_runner.lsp_replay_benchmark",
        command="codenib-lsp-replay-benchmark",
        extra="graph",
    )


def prebuilt_normalize_graphs_main() -> int | None:
    return _run_optional(
        "codenib.eval.agent_runner.prebuilt",
        command="codenib-prebuilt-normalize-graphs",
        extra="graph",
    )


def lsp_agent_ab_main() -> int | None:
    return _run_optional(
        "codenib.eval.agent_runner.lsp_agent_ab",
        command="codenib-lsp-agent-ab",
        extra="full",
    )


def lsp_agent_study_manifest_main() -> int | None:
    return _run_optional(
        "codenib.eval.agent_runner.lsp_agent_study_manifest",
        command="codenib-lsp-agent-study-manifest",
        extra="full",
    )


def lsp_agent_study_artifacts_main() -> int | None:
    return _run_optional(
        "codenib.eval.agent_runner.lsp_agent_study_artifacts",
        command="codenib-lsp-agent-study-artifacts",
        extra="graph",
    )


def lsp_agent_study_run_main() -> int | None:
    return _run_optional(
        "codenib.eval.agent_runner.lsp_agent_study_runner",
        command="codenib-lsp-agent-study-run",
        extra="full",
    )


__all__ = [
    "lsp_agent_ab_main",
    "lsp_agent_study_artifacts_main",
    "lsp_agent_study_manifest_main",
    "lsp_agent_study_run_main",
    "lsp_provider_validate_main",
    "lsp_replay_benchmark_main",
    "mcp_main",
    "prebuilt_normalize_graphs_main",
]
