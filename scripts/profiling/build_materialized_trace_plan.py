#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Build the fixed 25-snapshot mixed-operation trace plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codeminer.compiler.snapshot_store import SourceSnapshot  # noqa: E402
from codeminer.languages import get_language_spec  # noqa: E402
from codeminer.languages import normalize_chunker_language


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="sysevol-ai/codeminer-synthesis")
    parser.add_argument(
        "--dataset-revision",
        help="Immutable Hugging Face dataset revision recorded in the plan.",
    )
    parser.add_argument("--split", default="test")
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--lsp-report-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    rows = _load_dataset(args.dataset, args.split, args.dataset_revision)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["repo"]), str(row["base_commit"]))].append(row)
    if len(grouped) != 25 or any(len(group) != 20 for group in grouped.values()):
        raise ValueError("expected 25 exact snapshots with 20 synthesis rows each")

    trace_dir = args.output_dir / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    snapshots = []
    for (repo, commit), group in sorted(grouped.items()):
        instance_id = str(group[0]["instance_id"])
        language_group = str(group[0]["language_group"])
        if any(str(row["instance_id"]) != instance_id for row in group):
            raise ValueError(f"mixed instance ids for {repo}@{commit}")
        snapshot = SourceSnapshot(repo, commit)
        repo_path = args.artifact_root / ".snapshots" / snapshot.snapshot_id / "repo"
        report_path = args.lsp_report_dir / f"{instance_id}.json"
        if not repo_path.is_dir():
            raise FileNotFoundError(f"missing repository snapshot: {repo_path}")
        if not report_path.is_file():
            raise FileNotFoundError(f"missing LSP report: {report_path}")

        lsp_report = json.loads(report_path.read_text(encoding="utf-8"))
        subject = lsp_report.get("subject") or {}
        if subject.get("repository") != repo or subject.get("git_commit") != commit:
            raise ValueError(f"LSP report identity mismatch: {report_path}")
        lsp_requests, lsp_selection = _select_lsp_requests(lsp_report)
        requests = _retrieval_requests(group) + lsp_requests
        trace_path = trace_dir / f"{instance_id}.json"
        trace_path.write_text(json.dumps(requests, indent=2) + "\n", encoding="utf-8")
        snapshots.append(
            {
                "snapshot_id": snapshot.snapshot_id,
                "repository": repo,
                "commit": commit,
                "instance_id": instance_id,
                "language_group": language_group,
                "languages": _chunker_languages(language_group),
                "repo_path": str(repo_path.resolve()),
                "lsp_report": str(report_path.resolve()),
                "lsp_report_sha256": _file_sha256(report_path),
                "trace_path": str(trace_path.resolve()),
                "trace_sha256": _file_sha256(trace_path),
                "synthesis_queries": len(group),
                "lsp_candidate_requests": len(lsp_report["requests"]),
                "lsp_provider_equivalent_requests": lsp_selection[
                    "provider_equivalent_count"
                ],
                "lsp_eligible_definition_requests": lsp_selection[
                    "eligible_definition_count"
                ],
                "lsp_requests": len(lsp_requests),
                "lsp_selected_request_ids": lsp_selection["selected_request_ids"],
                "total_requests": len(requests),
            }
        )

    plan = {
        "schema_version": 5,
        "dataset": {
            "name": args.dataset,
            "revision": args.dataset_revision,
            "split": args.split,
        },
        "protocol": {
            "workload_kind": "controlled mixed-operation trace",
            "per_snapshot": {
                "dense": 20,
                "bm25": 20,
                "definition": 2,
            },
            "claim_boundary": (
                "controlled service workload, not an observed agent call distribution"
            ),
            "identity_guard": "exact repository and base_commit",
            "lsp_coordinate_conversion": (
                "JSON-RPC 0-based line to MCP agent-facing 1-based line"
            ),
            "lsp_admission_guard": (
                "select exactly two deterministic definition requests per snapshot "
                "from requests that were nonempty, stable, and equivalent across "
                "every measured static-index/JSON-RPC comparison"
            ),
            "lsp_replay_guard": (
                "newly materialized output must match the pre-registered ordered "
                "definition file/start-line fingerprint"
            ),
            "lsp_selection": (
                "deterministic hash ranking within provider-equivalent definition "
                "requests; references are outside the lifecycle-admitted subset"
            ),
            "lsp_capability_scope": (
                "definition only; RQ3 reports references separately, but independent "
                "materialization calibration did not establish their rebuild stability"
            ),
        },
        "snapshots": snapshots,
    }
    plan_path = args.output_dir / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "plan": str(plan_path),
                "snapshots": len(snapshots),
                "requests": sum(row["total_requests"] for row in snapshots),
            },
            indent=2,
        )
    )
    return 0


def _load_dataset(
    name: str, split: str, revision: str | None = None
) -> list[dict[str, Any]]:
    from datasets import get_dataset_config_names, load_dataset

    rows = []
    for config in get_dataset_config_names(name, revision=revision):
        rows.extend(
            dict(row)
            for row in load_dataset(
                name,
                config,
                split=split,
                revision=revision,
            )
        )
    return rows


def _retrieval_requests(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    requests = []
    for row in sorted(rows, key=lambda item: str(item["query_id"])):
        query_id = str(row["query_id"])
        query = str(row["query"])
        requests.extend(
            [
                {
                    "request_id": f"dense:{query_id}",
                    "operation": "dense",
                    "params": {"query": query, "top_k": 10, "level": "l2"},
                },
                {
                    "request_id": f"bm25:{query_id}",
                    "operation": "bm25",
                    "params": {"query": query, "top_k": 10},
                },
            ]
        )
    return requests


def _lsp_requests(report: dict[str, Any]) -> list[dict[str, Any]]:
    requests, _ = _select_lsp_requests(report)
    return requests


def _select_lsp_requests(
    report: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    provider_equivalent = _eligible_lsp_requests(report)
    eligible = [
        row
        for row in provider_equivalent
        if str(row["request"]["capability"]) == "definition"
    ]
    if len(eligible) < 2:
        raise ValueError(
            "expected at least two nonempty, stable, provider-equivalent definition "
            f"requests; found {len(eligible)}"
        )

    subject = report.get("subject") or {}
    selection_seed = str(
        subject.get("snapshot_id")
        or f"{subject.get('repository', '')}@{subject.get('git_commit', '')}"
    )
    selected = sorted(eligible, key=lambda row: _lsp_rank(selection_seed, row))[:2]
    selected.sort(key=lambda row: str(row["request"]["request_id"]))

    requests = []
    for selected_row in selected:
        row = selected_row["request"]
        capability = str(row["capability"])
        if capability != "definition":
            raise ValueError(f"unsupported LSP capability {capability!r}")
        arguments = dict(row["arguments"])
        line = arguments.get("line")
        if not isinstance(line, int) or isinstance(line, bool) or line < 0:
            raise ValueError(f"invalid zero-based LSP line {line!r}")
        arguments["line"] = line + 1
        requests.append(
            {
                "request_id": f"lsp:{row['request_id']}",
                "operation": capability,
                "params": arguments,
                "expected_result": {
                    "contract": "ordered_file_start_line",
                    "fingerprint": selected_row["fingerprint"],
                    "result_count": selected_row["result_count"],
                    "coordinate_system": "internal_0_based",
                    "admission": (
                        "nonempty and equivalent in every measured static-index/"
                        "JSON-RPC comparison; definition capability admitted for "
                        "independent rebuild validation"
                    ),
                },
            }
        )
    if len(requests) != 2:
        raise ValueError("expected two admitted definition requests per snapshot")
    return requests, {
        "provider_equivalent_count": len(provider_equivalent),
        "eligible_definition_count": len(eligible),
        "selected_request_ids": [row["request_id"] for row in requests],
    }


def _eligible_lsp_requests(report: dict[str, Any]) -> list[dict[str, Any]]:
    source_requests = {
        str(row["request_id"]): row for row in report.get("requests") or []
    }
    if len(source_requests) != len(report.get("requests") or []):
        raise ValueError("LSP report contains duplicate request ids")
    comparisons: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for comparison in report.get("comparisons") or []:
        request_id = str((comparison.get("request") or {}).get("request_id") or "")
        if request_id:
            comparisons[request_id].append(comparison)

    eligible = []
    for request_id, request in source_requests.items():
        rows = comparisons.get(request_id, [])
        if not rows:
            continue
        if any(
            (row.get("request") or {}).get("arguments") != request["arguments"]
            for row in rows
        ):
            raise ValueError(f"comparison arguments drifted for {request_id!r}")
        if not all(
            row.get("same_result") is True
            and (row.get("static_call") or {}).get("status") == "ok"
            and (row.get("reference_call") or {}).get("status") == "ok"
            and int((row.get("static_call") or {}).get("result_count") or 0) > 0
            for row in rows
        ):
            continue
        fingerprints = {
            str((row.get("static_call") or {}).get("result_fingerprint") or "")
            for row in rows
        }
        result_counts = {
            int((row.get("static_call") or {}).get("result_count") or 0) for row in rows
        }
        if len(fingerprints) != 1 or "" in fingerprints or len(result_counts) != 1:
            continue
        eligible.append(
            {
                "request": request,
                "fingerprint": fingerprints.pop(),
                "result_count": result_counts.pop(),
            }
        )
    return eligible


def _lsp_rank(selection_seed: str, row: dict[str, Any]) -> tuple[str, str]:
    request_id = str(row["request"]["request_id"])
    digest = hashlib.sha256(f"{selection_seed}\0{request_id}".encode()).hexdigest()
    return digest, request_id


def _chunker_languages(language_group: str) -> list[str]:
    languages = []
    for token in language_group.split("/"):
        language = normalize_chunker_language(token)
        if language is None:
            spec = get_language_spec(token)
            language = spec.chunker_language if spec else None
        if language and language not in languages:
            languages.append(language)
    if not languages:
        raise ValueError(f"unsupported language group {language_group!r}")
    return languages


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
