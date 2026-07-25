# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CLI for validating static-index LSP calls against live JSON-RPC LSP."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .live_lsp_provider import compare_static_to_live_lsp_provider
from .lsp_provider_validation import (
    FingerprintFn,
    FingerprintSelector,
    LSPProviderComparison,
    LSPProviderRequest,
    default_lsp_provider_fingerprint,
    fingerprint_lsp_start_location_set,
    fingerprint_lsp_start_locations,
    render_lsp_provider_validation_markdown,
    summarize_lsp_provider_validation,
)
from .prebuilt import load_code_graph_artifact, load_prebuilt_code_graph, repo_path_for


def load_lsp_provider_requests(path: str | Path) -> list[LSPProviderRequest]:
    """Load provider validation requests from JSON or JSONL."""

    request_path = Path(path)
    text = request_path.read_text(encoding="utf-8")
    if request_path.suffix.lower() == ".jsonl":
        rows = [
            json.loads(line)
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    else:
        data = json.loads(text)
        if isinstance(data, Mapping) and isinstance(data.get("requests"), list):
            rows = data["requests"]
        elif isinstance(data, list):
            rows = data
        elif isinstance(data, Mapping):
            rows = [data]
        else:
            raise ValueError(f"unsupported request JSON shape in {request_path}")
    return [_coerce_request(row) for row in rows]


def run_lsp_provider_validation_from_args(
    args: argparse.Namespace,
) -> list[LSPProviderComparison]:
    """Run static-vs-live validation from parsed CLI arguments."""

    project_root = _project_root_from_args(args)
    graph = _load_graph_from_args(args, project_root=project_root)
    requests = load_lsp_provider_requests(args.requests)
    fingerprint_fn, fingerprint_selector = _fingerprint_config_from_mode(
        args.fingerprint_mode
    )
    return compare_static_to_live_lsp_provider(
        requests,
        graph=graph,
        project_root=project_root,
        language=args.language,
        command=_command_from_arg(args.command),
        skip_probe=args.skip_probe,
        fingerprint_fn=fingerprint_fn,
        fingerprint_selector=fingerprint_selector,
    )


def write_lsp_provider_validation_report(
    comparisons: Sequence[LSPProviderComparison],
    *,
    output_json: str | Path | None = None,
    output_markdown: str | Path | None = None,
) -> dict[str, Any]:
    """Write JSON/markdown reports and return the JSON payload."""

    summary = summarize_lsp_provider_validation(comparisons)
    payload = {
        "summary": summary.to_dict(),
        "comparisons": [comparison.to_dict() for comparison in comparisons],
    }
    if output_json:
        _write_text(output_json, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    markdown = render_lsp_provider_validation_markdown(comparisons)
    if output_markdown:
        _write_text(output_markdown, markdown)
    return payload


def exit_code_for_lsp_provider_summary(
    summary: Mapping[str, Any],
    *,
    require_promotion: bool = False,
    fail_on_fallback: bool = False,
) -> int:
    """Return a process exit code for validation gates."""

    if int(summary.get("mismatch_count") or 0) > 0:
        return 2
    if int(summary.get("error_count") or 0) > 0:
        return 2
    if fail_on_fallback and int(summary.get("fallback_count") or 0) > 0:
        return 3
    if require_promotion and not bool(summary.get("promotion_ready")):
        return 4
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--graph", help="Path to a CodeGraph graph.pkl")
    source.add_argument(
        "--prebuilt-root", help="Prebuilt root containing instance dirs"
    )
    parser.add_argument("--instance-id", help="Required with --prebuilt-root")
    parser.add_argument(
        "--project-root",
        help="Repository root for live LSP. Defaults to prebuilt instance repo.",
    )
    parser.add_argument("--language", required=True, help="Graph/LSP language key")
    parser.add_argument(
        "--requests",
        required=True,
        help="JSON or JSONL file of graph-facing LSP requests",
    )
    parser.add_argument(
        "--command",
        help="Optional live LSP command string, for example 'pyright-langserver --stdio'",
    )
    parser.add_argument(
        "--skip-probe",
        action="store_true",
        help="Pass skip_probe=True when starting the live LSP client",
    )
    parser.add_argument(
        "--fingerprint-mode",
        choices=["auto", "ordered-start", "start-set"],
        default="auto",
        help=(
            "Result equivalence mode. auto uses ordered-start for definitions "
            "and start-set for references; ordered-start preserves provider "
            "order; start-set ignores order for reference-style location sets."
        ),
    )
    parser.add_argument("--output-json", help="Write machine-readable report")
    parser.add_argument("--output-markdown", help="Write markdown report")
    parser.add_argument(
        "--require-promotion",
        action="store_true",
        help="Exit non-zero unless every row is equivalent_static_faster",
    )
    parser.add_argument(
        "--fail-on-fallback",
        action="store_true",
        help="Exit non-zero when any static row requires fallback",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print markdown to stdout when no markdown output path is set",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        comparisons = run_lsp_provider_validation_from_args(args)
        payload = write_lsp_provider_validation_report(
            comparisons,
            output_json=args.output_json,
            output_markdown=args.output_markdown,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should report cleanly.
        parser.exit(1, f"codenib-lsp-provider-validate: {exc}\n")

    if not args.quiet and not args.output_markdown:
        sys.stdout.write(render_lsp_provider_validation_markdown(comparisons))

    return exit_code_for_lsp_provider_summary(
        payload["summary"],
        require_promotion=args.require_promotion,
        fail_on_fallback=args.fail_on_fallback,
    )


def _coerce_request(raw: Any) -> LSPProviderRequest:
    if isinstance(raw, LSPProviderRequest):
        return raw
    if not isinstance(raw, Mapping):
        raise ValueError(f"LSP provider request must be a mapping, got {type(raw)}")
    capability = str(raw.get("capability") or raw.get("lsp_method") or "")
    arguments = raw.get("arguments") or raw.get("resolved_arguments") or {}
    if not capability:
        raise ValueError(f"LSP provider request is missing capability: {raw}")
    if not isinstance(arguments, Mapping):
        raise ValueError(f"LSP provider request arguments must be a mapping: {raw}")
    request_id = raw.get("request_id")
    return LSPProviderRequest(
        capability=capability,
        arguments=dict(arguments),
        request_id=str(request_id) if request_id is not None else None,
    )


def _load_graph_from_args(args: argparse.Namespace, *, project_root: str) -> Any:
    if args.graph:
        return load_code_graph_artifact(str(args.graph), project_root=project_root)
    if not args.instance_id:
        raise ValueError("--instance-id is required with --prebuilt-root")
    graph = load_prebuilt_code_graph(str(args.prebuilt_root), str(args.instance_id))
    graph.project_root = str(Path(project_root).resolve())
    return graph


def _project_root_from_args(args: argparse.Namespace) -> str:
    if args.project_root:
        return str(Path(args.project_root).resolve())
    if args.prebuilt_root and args.instance_id:
        return repo_path_for(str(args.prebuilt_root), str(args.instance_id))
    raise ValueError("--project-root is required when using --graph")


def _command_from_arg(command: str | None) -> list[str] | None:
    if not command:
        return None
    parsed = shlex.split(command)
    if not parsed:
        raise ValueError("--command cannot be empty")
    return parsed


def _fingerprint_config_from_mode(
    mode: str,
) -> tuple[FingerprintFn | None, FingerprintSelector | None]:
    if mode == "auto":
        return None, default_lsp_provider_fingerprint
    if mode == "ordered-start":
        return fingerprint_lsp_start_locations, None
    if mode == "start-set":
        return fingerprint_lsp_start_location_set, None
    raise ValueError(f"unsupported fingerprint mode: {mode}")


def _write_text(path: str | Path, text: str) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_parser",
    "exit_code_for_lsp_provider_summary",
    "load_lsp_provider_requests",
    "main",
    "run_lsp_provider_validation_from_args",
    "write_lsp_provider_validation_report",
]
