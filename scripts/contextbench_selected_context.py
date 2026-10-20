#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Export CodeNib's retained source selection to ContextBench's unified format.

This selection policy retains the frozen search union and adds authenticated
base-source lines delivered through the solver's controlled reads/searches.
It does not infer source from arbitrary shell output or modified-file lines.
It reads no annotations, makes no model calls and imposes no source-body cap.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath

POLICY = "retained-search-plus-confirmed-base-source-v1"


def _path(value):
    if (
        not isinstance(value, str)
        or not value
        or PurePosixPath(value).is_absolute()
        or any(p in {"", ".", ".."} for p in value.split("/"))
        or any(c in value for c in "\r\n\0\\")
    ):
        raise ValueError("Selected source path is not repository-relative")
    return value


def _union(spans):
    intervals = []
    for span in spans:
        start, end = span.get("start"), span.get("end")
        if type(start) is not int or type(end) is not int or not 1 <= start <= end:
            raise ValueError("Selected source spans require positive inclusive lines")
        intervals.append((start, end))
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1]["end"] + 1:
            merged[-1]["end"] = max(merged[-1]["end"], end)
        else:
            merged.append({"start": start, "end": end})
    return merged


def selected_context(search_prediction, observations, model_patch):
    """Compile a selected pool, distinct from exhaustive observation attribution.

    Inputs must already be authenticated by the saved search handoff and recomputed
    model-observation receipts. Preserve every original search span, including the
    disclosed legacy VCS observations. Repair additions require confirmed base-source
    coordinates; stale, elided and unconfirmed bodies receive no added line credit.
    """
    instance = search_prediction["instance_id"]
    if observations["instance_id"] != instance or not isinstance(model_patch, str):
        raise ValueError("Selected context belongs to another solver task")
    attribution = observations["source_attribution"]
    if attribution.get("status") not in {"partial", "missing_receipts"}:
        raise ValueError("Unknown solver source-attribution contract")
    original = search_prediction["traj_data"]
    spans = {
        _path(file): _union(values) for file, values in original["pred_spans"].items()
    }
    files = {_path(file) for file in original["pred_files"]} | spans.keys()
    added = {}
    for file, lines in attribution["base_source_lines"].items():
        file = _path(file)
        if any(type(line) is not int or line <= 0 for line in lines):
            raise ValueError("Repair selection has invalid base-source coordinates")
        if lines:
            added[file] = _union([{"start": line, "end": line} for line in lines])
            spans[file] = _union(spans.get(file, []) + added[file])
            files.add(file)
    steps = copy.deepcopy(original["pred_steps"])
    for event in attribution.get("events", []):
        if (
            event.get("base_version") is True
            and event.get("confirmed_turns")
            and event.get("visible_source_lines")
        ):
            file = _path(event["file"])
            values = _union(
                [{"start": line, "end": line} for line in event["visible_source_lines"]]
            )
            steps.append({"files": [file], "spans": {file: values}})
    prediction = {
        "instance_id": instance,
        "model_patch": model_patch,
        "traj_data": {
            "pred_steps": steps,
            "pred_files": sorted(files),
            "pred_spans": dict(sorted(spans.items())),
        },
    }
    receipt = {
        "policy": POLICY,
        "selection_phase": "retained system source pool after repair",
        "original_search_selection_retained": True,
        "repair_base_source_lines": sum(
            s["end"] - s["start"] + 1 for values in added.values() for s in values
        ),
        "selected_source_lines": sum(
            s["end"] - s["start"] + 1 for values in spans.values() for s in values
        ),
        "repair_observation_coverage": attribution["status"],
        "all_repair_model_inputs_confirmed": observations["all_model_inputs_confirmed"],
        "repair_arbitrary_shell_and_modified_file_lines_selected": False,
        "legacy_search_VCS_observations_preserved": True,
        "annotations_used": False,
        "model_calls": 0,
        "upstream_acceptance_or_population_comparability_established": False,
    }
    return prediction, receipt


def export(args):
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts.benchmark_context_solver import model_observations
    from scripts.contextbench_search_pair_solver import load_pair_handoff

    def load(path):
        return json.loads(path.read_text())

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    config = load(args.solver_run / "config.json")
    row = load(args.solver_run / "solver.json")
    saved = load(args.solver_run / "search-pair-handoff.json")
    if config.get("retrieval_kind") != "search_pair_navigation":
        raise ValueError("Selected context requires a source-search handoff")
    if config.get("initial_context") != "retrieved":
        raise ValueError("Selected context excludes controls without the search stage")
    handoff = load_pair_handoff(args.search_root, row["instance_id"])
    if saved != handoff.receipt or config["source_commit"] != saved["base_commit"]:
        raise ValueError("Saved search/source handoff changed")
    observations = model_observations(row)
    if observations != load(args.solver_run / "model-observations.json"):
        raise ValueError("Saved model-observation receipts changed")
    search_path = (
        args.search_root
        / "live"
        / row["instance_id"]
        / "haiku_scout_pair-prediction.json"
    )
    prediction, receipt = selected_context(
        load(search_path), observations, row["model_patch"]
    )
    receipt.update(
        search_prediction_sha256=sha(search_path),
        solver_sha256=sha(args.solver_run / "solver.json"),
        model_observations_sha256=sha(args.solver_run / "model-observations.json"),
        handoff_sha256=sha(args.solver_run / "search-pair-handoff.json"),
        source_commit=config["source_commit"],
        source_tree=config["source_tree"],
        compiler_sha256=sha(Path(__file__)),
    )
    args.output.mkdir(parents=True, exist_ok=False)
    for name, value in [
        ("selected-context", prediction),
        ("selection-receipt", receipt),
    ]:
        (args.output / (name + ".json")).write_text(json.dumps(value, indent=2) + "\n")
    print(
        json.dumps(
            {
                "instance_id": row["instance_id"],
                "policy": POLICY,
                "selected_source_lines": receipt["selected_source_lines"],
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search-root", type=Path, required=True)
    parser.add_argument("--solver-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    export(parser.parse_args())


if __name__ == "__main__":
    main()
