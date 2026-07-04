#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Materialize a frozen feedback-suite execution plan.

The suite file is a small manifest that names the synthesis sweep config,
language/category slice, promotion profile, and expected cell budget. This
keeps agent-runner iteration from drifting into ad hoc probe selection while
remaining cheap enough for local Haiku feedback loops.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.agent_compile.aggregate_feedback_probe import (  # noqa: E402
    PROMOTION_PROFILES,
)
from scripts.agent_compile.lib.config import SweepConfig  # noqa: E402

PROMOTE_EXIT = 0
INVALID_EXIT = 2
PLAN_FILENAME = "feedback_suite_plan.json"
_SUITE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


def _load_yaml_object(path: Path, *, label: str) -> Dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} missing: {path}") from exc
    except yaml.YAMLError as exc:
        raise RuntimeError(f"{label} is not valid YAML: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must be a YAML mapping: {path}")
    return payload


def _project_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(_PROJECT_ROOT))
    except ValueError:
        return str(path)


def _resolve_path(value: Any, *, base_dir: Path, field: str) -> Path:
    if value is None or str(value).strip() == "":
        raise RuntimeError(f"{field} is required")
    path = Path(str(value))
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def _string_list(value: Any, *, field: str) -> List[str]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        items = [str(part).strip() for part in value]
    else:
        raise RuntimeError(f"{field} must be a string or list")
    out = [item for item in items if item]
    if not out:
        raise RuntimeError(f"{field} must not be empty")
    return out


def _positive_int(value: Any, *, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise RuntimeError(f"{field} must be a positive integer")
    return parsed


def _command(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def _command_entry(name: str, argv: Sequence[str]) -> Dict[str, Any]:
    argv_list = [str(part) for part in argv]
    return {
        "name": name,
        "argv": argv_list,
        "command": _command(argv_list),
    }


def _manifest_output_dir(
    *,
    manifest: Dict[str, Any],
    base_dir: Path,
    override: Optional[Path],
) -> Path:
    if override is not None:
        return override
    return _resolve_path(
        manifest.get("output_dir"), base_dir=base_dir, field="output_dir"
    )


def build_plan(
    *,
    suite_file: Path,
    output_dir_override: Optional[Path] = None,
    python: str = "python",
) -> Dict[str, Any]:
    manifest = _load_yaml_object(suite_file, label="feedback suite")
    suite_id = str(manifest.get("suite_id") or "").strip()
    if not suite_id or not _SUITE_ID_RE.match(suite_id):
        raise RuntimeError(
            "suite_id is required and may contain only lowercase letters, "
            "digits, '.', '_', and '-'"
        )

    base_dir = suite_file.resolve().parent
    config_path = _resolve_path(
        manifest.get("config"),
        base_dir=base_dir,
        field="config",
    )
    if not config_path.exists():
        raise RuntimeError(f"config does not exist: {config_path}")
    cfg = SweepConfig.from_yaml(config_path)

    output_dir = _manifest_output_dir(
        manifest=manifest,
        base_dir=base_dir,
        override=output_dir_override,
    )
    synthesis_configs = _string_list(
        manifest.get("synthesis_configs"),
        field="synthesis_configs",
    )
    categories = _string_list(manifest.get("categories"), field="categories")
    profiles = _string_list(
        manifest.get("promotion_profiles") or manifest.get("promotion_profile"),
        field="promotion_profiles",
    )
    unknown_profiles = [name for name in profiles if name not in PROMOTION_PROFILES]
    if unknown_profiles:
        raise RuntimeError(
            "unknown promotion profile(s): "
            + ", ".join(unknown_profiles)
            + "; available: "
            + ", ".join(sorted(PROMOTION_PROFILES))
        )

    max_queries_per_category = _positive_int(
        manifest.get("max_queries_per_category"),
        field="max_queries_per_category",
    )
    reps = _positive_int(manifest.get("reps", cfg.reps), field="reps")
    expected_queries = _positive_int(
        manifest.get("expected_queries"),
        field="expected_queries",
    )
    expected_cells = expected_queries * len(cfg.subsets) * reps
    declared_expected_cells = manifest.get("expected_cells")
    if declared_expected_cells is not None:
        declared = _positive_int(declared_expected_cells, field="expected_cells")
        if declared != expected_cells:
            raise RuntimeError(
                "expected_cells does not match expected_queries * arms * reps: "
                f"{declared} != {expected_queries} * {len(cfg.subsets)} * {reps}"
            )
    max_cells = manifest.get("max_cells")
    if max_cells is not None:
        max_cells_int = _positive_int(max_cells, field="max_cells")
        if expected_cells > max_cells_int:
            raise RuntimeError(
                f"expected cell budget {expected_cells} exceeds max_cells {max_cells_int}"
            )
    else:
        max_cells_int = None

    config_arg = _project_path(config_path)
    output_arg = _project_path(output_dir)
    synthesis_arg = ",".join(synthesis_configs)
    category_arg = ",".join(categories)
    profile_args: List[str] = []
    for profile in profiles:
        profile_args.extend(["--promotion-profile", profile])

    preflight_argv = [
        python,
        "scripts/agent_compile/aggregate_feedback_probe.py",
        "--preflight-route-lifecycle-config",
        config_arg,
        "--route-gate-synthesis-configs",
        synthesis_arg,
        "--route-gate-categories",
        category_arg,
        "--route-gate-max-queries-per-category",
        str(max_queries_per_category),
        "--output-dir",
        output_arg,
        *profile_args,
    ]
    sweep_argv = [
        python,
        "scripts/agent_compile/run_synthesis_sweep.py",
        "--config",
        config_arg,
        "--output-dir",
        output_arg,
        "--synthesis-configs",
        synthesis_arg,
        "--categories",
        category_arg,
        "--max-queries-per-category",
        str(max_queries_per_category),
        "--reps",
        str(reps),
    ]
    aggregate_argv = [
        python,
        "scripts/agent_compile/aggregate_feedback_probe.py",
        "--cells-dir",
        f"{output_arg}/cells",
        "--output-dir",
        output_arg,
        *profile_args,
    ]

    coverage = manifest.get("coverage") or []
    if not isinstance(coverage, list):
        raise RuntimeError("coverage must be a list when provided")

    return {
        "feedback_suite_plan_version": 1,
        "suite_id": suite_id,
        "milestone": manifest.get("milestone"),
        "goal": manifest.get("goal"),
        "suite_file": _project_path(suite_file),
        "config": config_arg,
        "output_dir": output_arg,
        "promotion_profiles": profiles,
        "synthesis_configs": synthesis_configs,
        "categories": categories,
        "max_queries_per_category": max_queries_per_category,
        "reps": reps,
        "arms": sorted(cfg.subsets),
        "expected_queries": expected_queries,
        "expected_cells": expected_cells,
        "max_cells": max_cells_int,
        "coverage": coverage,
        "commands": [
            _command_entry("preflight", preflight_argv),
            _command_entry("sweep", sweep_argv),
            _command_entry("aggregate", aggregate_argv),
        ],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-file", required=True, type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Override the suite manifest output_dir.",
    )
    parser.add_argument(
        "--python",
        default="python",
        help="Python executable token to put in generated commands.",
    )
    parser.add_argument(
        "--write-plan",
        action="store_true",
        help="Write feedback_suite_plan.json under the resolved output directory.",
    )
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="Print the JSON plan. The default is to print command lines.",
    )
    args = parser.parse_args(argv)

    try:
        plan = build_plan(
            suite_file=args.suite_file,
            output_dir_override=args.output_dir,
            python=args.python,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return INVALID_EXIT

    if args.write_plan:
        output_dir = Path(str(plan["output_dir"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / PLAN_FILENAME).write_text(
            json.dumps(plan, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    if args.print_json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        for command in plan["commands"]:
            print(command["command"])
    return PROMOTE_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
