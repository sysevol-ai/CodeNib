#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Plan a small smoke + rotating holdout slice for agent-runner iteration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a deterministic smoke + holdout plan for small agent sweeps.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--seed", default="codenib-feedback-v1")
    parser.add_argument("--group-field", default="language_group")
    parser.add_argument("--smoke-per-group", type=int, default=1)
    parser.add_argument("--holdout-per-group", type=int, default=2)
    parser.add_argument(
        "--exclude-instance",
        action="append",
        default=[],
        help="Instance id to exclude; repeat for multiple ids.",
    )
    parser.add_argument("--output-json", type=Path, default=None)
    args = parser.parse_args(argv)

    from codenib.eval.agent_runner.feedback import FeedbackPlanSpec, build_feedback_plan
    from codenib.eval.agent_runner.sweep import load_dataset_rows
    from codenib.eval.agent_runner.sweep_config import SweepConfig

    cfg = SweepConfig.from_yaml(args.config)
    rows_by_id, _ = load_dataset_rows(cfg)
    plan = build_feedback_plan(
        rows_by_id,
        FeedbackPlanSpec(
            seed=args.seed,
            group_field=args.group_field,
            smoke_per_group=args.smoke_per_group,
            holdout_per_group=args.holdout_per_group,
            exclude_instances=tuple(args.exclude_instance or ()),
        ),
    )
    payload = plan.to_dict()
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
