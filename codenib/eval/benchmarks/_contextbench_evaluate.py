# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Subprocess bridge to the pinned official evaluator (stdlib imports only).

Executed by filename so an independent evaluator environment need not install
CodeNib. The only replaced upstream I/O is checkout: scoring reads the exact
already-audited agent snapshot and cannot fetch or change its revision.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import TemporaryDirectory


def validate_symbol_extraction(extract_defs) -> None:
    """Catch parsers that construct but silently fail the upstream bytes API.

    Upstream catches parse errors and returns an empty definition set. For
    example, language-pack 1.8 expects str while upstream passes bytes; empty
    gold and prediction sets then misleadingly score 100% symbol coverage.
    Probe the actual extractor, without changing the official metric algorithm.
    """
    probes = {
        "py": "def contextbench_probe():\n    return 1\n",
        "js": "function contextbench_probe() { return 1; }\n",
        "ts": "function contextbench_probe(): number { return 1; }\n",
        "go": "package main\nfunc contextbench_probe() int { return 1 }\n",
        "c": "int contextbench_probe(void) { return 1; }\n",
        "cpp": "int contextbench_probe() { return 1; }\n",
        "rs": "fn contextbench_probe() -> i32 { 1 }\n",
        "java": "class Probe { int contextbench_probe() { return 1; } }\n",
    }
    with TemporaryDirectory(prefix="contextbench-parser-") as directory:
        for suffix, content in probes.items():
            source = Path(directory) / f"probe.{suffix}"
            source.write_text(content, encoding="utf-8")
            if not extract_defs(str(source)):
                raise RuntimeError(
                    "official ContextBench symbol extraction failed its known-function "
                    f"probe for .{suffix}; select a compatible environment with "
                    "--evaluator-python (upstream passes bytes to tree-sitter)"
                )


def audit_gold_source_locations(repo: Path, line_spans: dict) -> list[dict]:
    """Describe annotation locations unavailable in the pinned source reader.

    Keep the official normalizer's paths and inclusive intervals unchanged.
    A plausible suffix match must never silently replace an annotation or gain
    retrieval credit. This is evaluation-only metadata, not a scoring filter.
    """
    repo = repo.resolve()
    issues = []
    for file, spans in line_spans.items():
        issue = {"file": file, "unavailable_line_spans": spans}
        try:
            path = (repo / file).resolve()
            if not path.is_relative_to(repo):
                issues.append({**issue, "reason": "outside_source_root"})
                continue
            if not path.is_file():
                issues.append({**issue, "reason": "path_missing"})
                continue
            size = len(path.read_text(errors="replace").splitlines())
        except (OSError, RuntimeError):
            # Diagnostics must not invalidate a score for an unreadable path.
            issues.append({**issue, "reason": "source_unreadable"})
            continue
        unavailable = []
        for start, end in spans:
            if start < 1:
                unavailable.append([start, min(end, 0)])
            if end > size:
                unavailable.append([max(start, size + 1), end])
        if unavailable:
            issues.append(
                {
                    "file": file,
                    "reason": "line_outside_source",
                    "source_line_count": size,
                    "unavailable_line_spans": unavailable,
                }
            )
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.upstream.resolve()))
    from contextbench import evaluate
    from contextbench.extractors import available, extract_defs
    from contextbench.parsers import Gold

    if not available():
        raise RuntimeError("official ContextBench tree-sitter parsers unavailable")
    validate_symbol_extraction(extract_defs)
    if args.input is None:
        packages = {}
        for name in (
            "tree-sitter",
            "tree-sitter-languages",
            "tree-sitter-language-pack",
        ):
            try:
                packages[name] = version(name)
            except PackageNotFoundError:
                continue
        print(
            json.dumps(
                {"evaluator_ready": True, "python": sys.version, "packages": packages}
            )
        )
        return 0
    items = json.loads(args.input.read_text(encoding="utf-8"))
    results = []
    for item in items:
        instance_id = item["prediction"]["instance_id"]
        try:
            repo = Path(item["repo_path"]).resolve()
            expected = item["gold"]["commit"]

            def checked_checkout(_url, commit, _cache, repo=repo, expected=expected):
                observed = subprocess.check_output(
                    ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
                ).strip()
                if commit != expected or observed != expected:
                    raise ValueError("evaluator snapshot revision mismatch")
                return str(repo)

            evaluate.checkout = checked_checkout
            # Never allow evaluate_instance's reference-patch fallback to
            # produce an EditLoc score for a retrieval-only agent.
            gold = dict(item["gold"])
            gold.pop("patch", None)
            gold.pop("test_patch", None)
            prediction = dict(item["prediction"])
            prediction["repo_url"] = str(repo)
            prediction["commit"] = expected
            official_gold = Gold(gold)
            result = evaluate.evaluate_instance(
                instance_id, official_gold, prediction, str(repo)
            )
            # Auxiliary source-overlap audits must use the same path normalization
            # as scoring: raw annotations may contain /workspace/<repo>/ prefixes.
            # Keep this evaluation-only metadata out of model-facing predictions.
            result["gold_line_spans"] = official_gold.line_spans_init()
            result["gold_source_issues"] = audit_gold_source_locations(
                repo, result["gold_line_spans"]
            )
            results.append(result)
        except Exception as exc:
            results.append(
                {
                    "instance_id": instance_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            )
    report = {"cases": results, "summary": evaluate.aggregate_results(results)}
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if all("error" not in row for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
