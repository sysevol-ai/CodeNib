#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Replay product candidate generation against the frozen 100-case experiment.

Only the planner and scoring provider boundaries are replaced. Actual product
source authentication, chunking, rg execution, interleaving and bounds run on
immutable Git blobs in a temporary directory. No dataset checkout is changed.
Zero-score scoring fixtures collect candidates without looking at gold labels.
Frozen Jev scores are usable only for byte-identical candidate text afterwards;
they are a sensitivity calculation, never a fresh production quality result.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from statistics import mean
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codenib.agent.runtime import grep_jev as runtime
from codenib.source_fingerprint import capture_repository_source
from codenib.types import NodeInfo
from scripts.analyze_jev_dense import align_cases, read_run
from scripts.audit_jev_visible_ranges import corrected_node
from scripts.benchmark_jev_base import digest, git, load_cases, metrics, write_json

MAX_SNAPSHOT_BYTES = 512 * 1024**2


def materialize_commit(repo: Path, commit: str, tree: str, root: Path) -> dict:
    """Copy raw Git blobs, including symlinks, without checking out or executing."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Expected a pinned Git commit")
    if git(repo, "rev-parse", f"{commit}^{{tree}}").decode().strip() != tree:
        raise ValueError("Source tree differs from the frozen experiment")
    entries, submodules, total_bytes = [], [], 0
    for record in git(repo, "ls-tree", "-rlz", "--full-tree", commit).split(b"\0"):
        if not record:
            continue
        header, raw_path = record.split(b"\t", 1)
        mode, kind, oid, size = header.decode().split()
        path = Path(raw_path.decode("utf-8"))
        if path.is_absolute() or any(p in {"..", ".git"} for p in path.parts):
            raise ValueError("Git source path escapes the isolated snapshot")
        if mode == "160000" and kind == "commit":
            submodules.append(path.as_posix())
            continue
        if kind != "blob" or mode not in {"100644", "100755", "120000"}:
            raise ValueError("Unsupported Git tree entry")
        entries.append((path, mode, oid, int(size)))
        total_bytes += int(size)
    if len(entries) > 100000 or total_bytes > MAX_SNAPSHOT_BYTES:
        raise ValueError("Full Git snapshot exceeds audit resource limits")
    # Batches bound memory and avoid bidirectional pipe deadlocks. Raw bytes,
    # not a normalized research corpus, reach the product's own source reader.
    for offset in range(0, len(entries), 128):
        batch = entries[offset : offset + 128]
        stream = io.BytesIO(
            git(
                repo,
                "cat-file",
                "--batch",
                input_bytes="".join(oid + "\n" for _, _, oid, _ in batch).encode(),
            )
        )
        for path, mode, oid, size in batch:
            actual, kind, observed_size = stream.readline().decode().split()
            data = stream.read(size)
            if (
                (actual, kind, int(observed_size)) != (oid, "blob", size)
                or len(data) != size
                or stream.read(1) != b"\n"
            ):
                raise ValueError("Invalid immutable Git blob stream")
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if mode == "120000":
                target.symlink_to(data.decode("utf-8"))
            else:
                target.write_bytes(data)
    return {
        "files": len(entries),
        "bytes": total_bytes,
        "uninitialized_submodules": submodules,
    }


def text_identity(node: NodeInfo) -> tuple:
    return (
        node.node_id,
        node.file,
        node.start_line,
        hashlib.sha256((node.content or "").encode()).hexdigest(),
    )


def compare_candidates(case: dict, candidates: list[NodeInfo], ranking: list) -> dict:
    """Apply labels/scores only after production candidate selection is finished."""
    frozen = [NodeInfo.model_validate(n) for n in case["candidates"]]
    before = [text_identity(n) for n in frozen]
    after = [text_identity(n) for n in candidates]
    if len(set(before)) != len(before) or len(set(after)) != len(after):
        raise ValueError("Duplicate candidate identity")
    scores = {
        (r["node_id"], r["start_line"], r["end_line"]): r["score"] for r in ranking
    }
    if len(scores) != len(frozen):
        raise ValueError("Frozen scores do not cover the original pool exactly")
    by_text = {
        text_identity(n): scores[(n.node_id, n.start_line, n.end_line)] for n in frozen
    }
    corrected = [corrected_node(n) for n in case["candidates"]]
    matched = all(identity in by_text for identity in after)
    ranked_recall = None
    if matched:
        order = sorted(candidates, key=lambda n: -by_text[text_identity(n)])
        ranked_recall = metrics(case, order)["span_recall@5"]
    return {
        "frozen_candidate_count": len(before),
        "product_candidate_count": len(after),
        "ordered_text_match": before == after,
        "corrected_span_order_match": before == after
        and [n.end_line for n in corrected] == [n.end_line for n in candidates],
        "added_candidate_ids": [n[0] for n in after if n not in set(before)],
        "removed_candidate_ids": [n[0] for n in before if n not in set(after)],
        "all_product_texts_have_frozen_scores": matched,
        "frozen_grep_recall_at_5": metrics(case, corrected)["span_recall@5"],
        "product_grep_recall_at_5": metrics(case, candidates)["span_recall@5"],
        "frozen_score_sensitivity_recall_at_5": ranked_recall,
    }


def replay(case: dict, plan: dict, prebuilt: Path) -> tuple[list[NodeInfo], dict]:
    """Replay only external provider replies, with HTTP requests prohibited."""
    captured, observed_payload = [], {}

    def planner(payload, config, _key, budget):
        observed_payload.update(payload)
        budget.before_model()
        budget.record("planning", config.planner_model, {"cost": 0})
        return runtime.GrepPlan(actions=plan["actions"])

    def scorer(_client, query, batch):
        if query != case["query"]:
            raise ValueError("Product scoring query changed")
        captured.extend(node.model_copy(deep=True) for _, node in batch)
        return SimpleNamespace(
            model=runtime.JEV_MODEL,
            usage={"cost": 0},
            answers={f"node_{i}": SimpleNamespace(score=0) for i, _ in batch},
        )

    with tempfile.TemporaryDirectory(prefix="codenib-product-audit-") as temporary:
        root = Path(temporary) / case["repo"].split("/")[-1]
        root.mkdir()
        snapshot = materialize_commit(
            prebuilt / case["instance_id"] / "repo",
            case["base_commit"],
            case["git_tree"],
            root,
        )
        started = time.monotonic()

        def deadline():
            if time.monotonic() - started >= 90:
                raise runtime.GrepJevError("Product source/candidate deadline exceeded")

        with (
            patch(
                "requests.Session.send", side_effect=AssertionError("HTTP forbidden")
            ),
            patch.object(runtime, "_plan", planner),
            patch.object(runtime, "decide_code_relevance", scorer),
            capture_repository_source(root, check_cancelled=deadline) as source,
        ):
            config = runtime.GrepJevConfig(
                api_key="offline-audit-fixture",
                timeout=max(0.001, 90 - (time.monotonic() - started)),
            )
            result = runtime.GrepJevRetriever(config).search(
                source, case["query"], top_k=100, check_cancelled=deadline
            )
    if len(captured) != result.plan["candidate_count"]:
        raise ValueError("Scoring fixture did not observe every product candidate")
    differences = sorted(
        k
        for k in set(observed_payload) | set(plan["input"])
        if observed_payload.get(k) != plan["input"].get(k)
    )
    return captured, {
        "snapshot": snapshot,
        "planner_input_differences": differences,
        "planner_input": observed_payload,
        "source_and_candidate_seconds": time.monotonic() - started,
        "actions": result.plan["actions"],
        "skipped_files": result.plan["skipped_files"],
    }


def audit(args) -> dict:
    manifest, published = load_cases(args.prepared)
    labels = json.loads(args.labels.read_text())
    cases = align_cases(published, labels["base_aligned_sensitivity"]["label_audit"])
    if len(cases) != 100 or len({c["repo"] for c in cases}) != 25:
        raise ValueError("The complete 100-case, 25-repository split is required")
    config, rankings, _, run_hashes, _ = read_run(
        args.run, args.prepared, published, cases
    )
    if config["backend"] != "jev" or config["model"] != runtime.JEV_MODEL:
        raise ValueError("Expected the frozen Jev run")
    inputs = {
        "prepared_manifest": digest(args.prepared / "manifest.json"),
        "label_report": digest(args.labels),
        **run_hashes,
    }
    prior = json.loads((ROOT / "docs/assets/grep_jev_range_audit.json").read_text())
    if inputs != prior["input_sha256"]:
        raise ValueError(
            "Experiment input hashes differ from the published range audit"
        )
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "cases").mkdir()
    rows = []
    for case in cases:
        iid = case["instance_id"]
        row = {"instance_id": iid, "language_group": case["language_group"]}
        plan_path = args.prepared / "plans" / f"{iid}.json"
        if digest(plan_path) != case["grep"]["plan_sha256"]:
            raise ValueError("Frozen planner artifact changed")
        plan = json.loads(plan_path.read_text())
        try:
            if plan.get("error"):
                raise ValueError("Frozen planner failed")
            candidates, trace = replay(case, plan, args.prebuilt_root)
            row.update(compare_candidates(case, candidates, rankings[iid]["ranking"]))
            row.update(trace)
            write_json(
                args.output / "cases" / f"{iid}.json",
                {**row, "candidates": [n.model_dump() for n in candidates]},
            )
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            write_json(args.output / "cases" / f"{iid}.json", row)
        rows.append(row)
        print(
            json.dumps(
                {
                    "done": len(rows),
                    **{
                        k: v
                        for k, v in row.items()
                        if k
                        in {
                            "instance_id",
                            "error",
                            "ordered_text_match",
                            "product_candidate_count",
                        }
                    },
                }
            ),
            flush=True,
        )
    passed = [r for r in rows if "error" not in r]
    complete = len(passed) == len(cases)
    sensitivity_complete = complete and all(
        r["all_product_texts_have_frozen_scores"] for r in passed
    )
    result = {
        "scope": (
            "Frozen-plan replay of production candidate generation, "
            "not fresh model or agent evaluation"
        ),
        "dataset_revision": manifest["dataset_revision"],
        "cases": len(cases),
        "completed_cases": len(passed),
        "errors": [r for r in rows if "error" in r],
        "ordered_text_matches": sum(r["ordered_text_match"] for r in passed),
        "corrected_span_order_matches": sum(
            r["corrected_span_order_match"] for r in passed
        ),
        "planner_input_difference_fields": dict(
            Counter(k for row in passed for k in row["planner_input_differences"])
        ),
        "product_grep_recall_at_5": (
            mean(r["product_grep_recall_at_5"] for r in passed) if complete else None
        ),
        "frozen_score_sensitivity_recall_at_5": (
            mean(r["frozen_score_sensitivity_recall_at_5"] for r in passed)
            if sensitivity_complete
            else None
        ),
        "score_sensitivity_note": (
            "Reuse only exact candidate-text scores, with product stable tie "
            "ordering; no new Jev calls"
        ),
        "input_sha256": inputs,
        "product_sha256": digest(ROOT / "codenib/agent/runtime/grep_jev.py"),
        "script_sha256": digest(Path(__file__)),
        "billed_api_calls": 0,
        "per_case": rows,
    }
    write_json(args.output / "summary.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--prebuilt-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    result = audit(parser.parse_args())
    if result["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
