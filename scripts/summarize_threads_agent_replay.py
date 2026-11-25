#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Summarize the always-on agent replays into the published rows and summary.

Reads the output directories of ``benchmark_threads_agent.py`` (and of the
first-look sampling study) and writes, to ``--out``:

- ``threads_agent_replay.csv``: one row per pull request, arm and run, with
  numbers only: model runs, wakes read without a run, drafts, cost, reviewer
  points and how many the agent raised before the reviewer did.
- ``threads_agent_first_look.csv``: per pull request, which of four
  independent first-look reviews, and the merge of three, raised each point.
- ``threads_agent_review_points.csv``: what kind of point each reviewer
  comment makes (bug, test, design, idiom, question), as labelled by
  ``benchmark_threads_agent.py label``.
- ``threads_agent_replay_summary.json``: the pooled comparisons with 95%
  bootstrap intervals over pull requests, resampled within each repository.

    summarize_threads_agent_replay.py --october O --november N --repeat R \\
        --repos M/<owner>__<repo>... --first-look E/<owner>__<repo>... \\
        --labels O/point_labels.json M/<owner>__<repo>/point_labels.json... \\
        --out docs/assets
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import random
from pathlib import Path

ROUNDS = 10000


def _runs(path: Path) -> list[dict]:
    records = json.loads(path.read_text())["records"]
    return [r for r in records if r.get("outcome") != "skipped"]


def rows_for(study: str, out: Path, arms: list[str], exclude: set[int]) -> list[dict]:
    selection = json.loads((out / "selection.json").read_text())
    repo = selection["repo"]
    rows = []
    for number in selection["order"]:
        if number in exclude:
            continue
        for arm in arms:
            result = out / "results" / arm / f"{number}.json"
            if not result.exists():
                continue
            data = json.loads(result.read_text())
            runs = _runs(result)
            verdicts = json.loads((out / "judge" / arm / f"{number}.json").read_text())
            base, _, rep = arm.partition(".r")
            rows.append(
                {
                    "study": study,
                    "repo": repo,
                    "pull": number,
                    "arm": base,
                    "run": int(rep or 1),
                    "passes_with_a_change": data["passes_with_any_change"],
                    "passes_with_news_for_the_agent": data[
                        "passes_with_news_for_the_agent"
                    ],
                    "model_runs": len(runs),
                    "drafts": sum(r.get("outcome") == "reply" for r in runs),
                    "cost_usd": round(sum(r.get("cost_usd") or 0 for r in runs), 5),
                    "agent_input_tokens": sum(
                        r.get("agent_input_tokens") or 0 for r in runs
                    ),
                    "reviewer_points": len(verdicts),
                    "raised_first": sum(v["raised"] for v in verdicts),
                }
            )
    return rows


def judged(out: Path, arm: str, exclude: set[int]) -> dict:
    """Comment address -> whether ``arm`` raised it first, for every point."""
    selection = json.loads((out / "selection.json").read_text())
    found = {}
    for number in selection["order"]:
        if number in exclude:
            continue
        path = out / "judge" / arm / f"{number}.json"
        if path.exists():
            for v in json.loads(path.read_text()):
                found[v["comment"]] = (selection["repo"], number, v["raised"])
    return found


def first_look_rows(dirs: list[Path]) -> list[dict]:
    rows = []
    for out in dirs:
        repo = json.loads((out / "selection.json").read_text())["repo"]
        for path in sorted((out / "firstlook").glob("*.json")):
            data = json.loads(path.read_text())
            for comment, got in data["verdicts"].items():
                row = {
                    "repo": repo,
                    "pull": data["number"],
                    "point": comment.split("#", 1)[1],
                }
                for i in range(len(data["samples"])):
                    row[f"review_{i + 1}"] = int(bool(got.get(f"s{i}")))
                row["merged_of_1_to_3"] = int(bool(got.get("merged")))
                rows.append(row)
            if not data["verdicts"]:
                rows.append({"repo": repo, "pull": data["number"], "point": ""})
    return rows


def _boot(groups: dict, stat) -> list[float]:
    rng = random.Random(20261119)
    values = []
    for _ in range(ROUNDS):
        sample = []
        for members in groups.values():
            sample += [members[rng.randrange(len(members))] for _ in members]
        values.append(stat(sample))
    values.sort()
    return [
        round(values[int(0.025 * ROUNDS)], 3),
        round(values[int(0.975 * ROUNDS)], 3),
    ]


def _per_pull(rows, arm, key):
    """Mean over runs of ``key`` for each (repo, pull) of ``arm``."""
    found = {}
    for r in rows:
        if r["arm"] == arm:
            found.setdefault((r["repo"], r["pull"]), []).append(r[key])
    return {k: sum(v) / len(v) for k, v in found.items()}


def compare(rows, a, b, keys=("cost_usd", "drafts", "raised_first")) -> dict:
    pulls = sorted(
        set(_per_pull(rows, a, "drafts")) & set(_per_pull(rows, b, "drafts"))
    )
    groups = {}
    for p in pulls:
        groups.setdefault(p[0], []).append(p)
    result = {"pull_requests": len(pulls), "repositories": len(groups)}
    for key in keys:
        x, y = _per_pull(rows, a, key), _per_pull(rows, b, key)
        tx, ty = sum(x[p] for p in pulls), sum(y[p] for p in pulls)
        result[key] = {
            a: round(tx, 3),
            b: round(ty, 3),
            "ratio": round(tx / ty, 3) if ty else None,
            "ratio_ci95": _boot(
                groups,
                lambda s, x=x, y=y: sum(x[p] for p in s)
                / max(1e-9, sum(y[p] for p in s)),
            ),
            "difference": round(tx - ty, 3),
            "difference_ci95": _boot(
                groups,
                lambda s, x=x, y=y: sum(x[p] for p in s) - sum(y[p] for p in s),
            ),
        }
    return result


def first_look_summary(rows) -> dict:
    by_pull = {}
    for r in rows:
        by_pull.setdefault((r["repo"], r["pull"]), []).append(r)
    points = [r for r in rows if r["point"]]
    curve = {}
    for k in range(1, 5):
        subsets = list(itertools.combinations(range(1, 5), k))
        curve[str(k)] = round(
            sum(sum(any(r[f"review_{i}"] for i in s) for r in points) for s in subsets)
            / len(subsets),
            2,
        )
    union3 = sum(any(r[f"review_{i}"] for i in (1, 2, 3)) for r in points)
    merged = sum(r["merged_of_1_to_3"] for r in points)
    groups = {}
    for key in by_pull:
        groups.setdefault(key[0], []).append(key)

    def gain(sample):
        total = 0.0
        for key in sample:
            pts = [r for r in by_pull[key] if r["point"]]
            one = sum(sum(r[f"review_{i}"] for r in pts) for i in range(1, 5)) / 4
            total += sum(r["merged_of_1_to_3"] for r in pts) - one
        return total

    return {
        "pull_requests": len(by_pull),
        "reviewer_points": len(points),
        "raised_by_any_of_k_reviews": curve,
        "raised_by_the_union_of_reviews_1_to_3": union3,
        "raised_by_their_merge": merged,
        "merge_minus_one_review": round(merged - curve["1"], 2),
        "merge_minus_one_review_ci95": _boot(groups, gain),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--october", type=Path, required=True)
    parser.add_argument("--november", type=Path, required=True)
    parser.add_argument("--repeat", type=Path, required=True)
    parser.add_argument("--repos", type=Path, nargs="+", required=True)
    parser.add_argument("--first-look", type=Path, nargs="+", required=True)
    parser.add_argument("--labels", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    october = json.loads((args.october / "selection.json").read_text())
    pilot = set(october["pilot"])
    rows = rows_for(
        "ruff-october", args.october, ["codenib", "stateless", "naive"], pilot
    )
    rows += rows_for(
        "ruff-november", args.november, ["codenib", "codenib-commits"], set()
    )
    for row in rows_for("ruff-november", args.repeat, ["codenib"], set()):
        row["run"] = 2
        rows.append(row)
    for out in args.repos:
        rows += rows_for(
            "four-repositories",
            out,
            ["codenib", "codenib.r2", "codenib-broad", "codenib-broad.r2", "stateless"],
            set(),
        )
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "threads_agent_replay.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    looks = first_look_rows(args.first_look)
    fields = ["repo", "pull", "point", "review_1", "review_2", "review_3", "review_4",
              "merged_of_1_to_3"]  # fmt: skip
    with (args.out / "threads_agent_first_look.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=fields, restval="", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(looks)

    labels = {}
    for path in args.labels:
        labels.update(json.loads(path.read_text()))
    # The pooled points: October ruff without the pilot, and the four
    # repositories, as judged for each arm (the first run of each).
    arms = {"codenib": {}, "stateless": {}}
    for arm in arms:
        arms[arm].update(judged(args.october, arm, pilot))
        for out in args.repos:
            arms[arm].update(judged(out, arm, set()))
    points = []
    for address, (repo, number, raised) in sorted(arms["codenib"].items()):
        kind = labels.get(address, {})
        points.append(
            {
                "repo": repo,
                "pull": number,
                "point": address.split("#", 1)[1],
                "kind": kind.get("category", ""),
                "found_by_running_code": int(bool(kind.get("needs_running"))),
                "raised_first_codenib": int(raised),
                "raised_first_stateless": int(arms["stateless"][address][2]),
            }
        )
    with (args.out / "threads_agent_review_points.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(points[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(points)
    kinds = {}
    for p in points:
        k = kinds.setdefault(p["kind"], {"points": 0, "codenib": 0, "stateless": 0})
        k["points"] += 1
        k["codenib"] += p["raised_first_codenib"]
        k["stateless"] += p["raised_first_stateless"]

    def study(name):
        return [r for r in rows if r["study"] == name]

    pooled = study("ruff-october") + study("four-repositories")
    passes = [r for r in pooled if r["arm"] == "codenib" and r["run"] == 1]
    summary = {
        "model": "Claude Sonnet 5.5 (claude-sonnet-5-5) through Claude Code",
        "judge": "the same model, blind to the arm",
        "codenib_vs_stateless_five_repositories": compare(
            pooled, "codenib", "stateless"
        ),
        "codenib_vs_naive_ruff_october": compare(
            study("ruff-october"), "codenib", "naive"
        ),
        "passes_read_without_waking_the_agent": {
            "passes_with_a_change": sum(r["passes_with_a_change"] for r in passes),
            "passes_with_news_for_the_agent": sum(
                r["passes_with_news_for_the_agent"] for r in passes
            ),
        },
        "new_commit_rule_ruff_november": compare(
            [r for r in study("ruff-november") if r["run"] == 1],
            "codenib-commits",
            "codenib",
        ),
        "broader_instructions_four_repositories": compare(
            study("four-repositories"), "codenib-broad", "codenib"
        ),
        "same_arm_twice": {
            name: {
                "raised_first": [
                    sum(
                        r["raised_first"]
                        for r in rs
                        if r["arm"] == "codenib" and r["run"] == k
                    )
                    for k in (1, 2)
                ],
                "drafts": [
                    sum(
                        r["drafts"]
                        for r in rs
                        if r["arm"] == "codenib" and r["run"] == k
                    )
                    for k in (1, 2)
                ],
                "cost_usd": [
                    round(
                        sum(
                            r["cost_usd"]
                            for r in rs
                            if r["arm"] == "codenib" and r["run"] == k
                        ),
                        2,
                    )
                    for k in (1, 2)
                ],
            }
            for name, rs in (
                ("ruff-november", study("ruff-november")),
                ("four-repositories", study("four-repositories")),
            )
        },
        "first_look_sampling": first_look_summary(looks),
        "reviewer_points_by_kind": {
            "points": len(points),
            "found_by_running_code": sum(p["found_by_running_code"] for p in points),
            "kinds": kinds,
            "note": "raised first by the first run of each arm",
        },
    }
    p = summary["passes_read_without_waking_the_agent"]
    p["share_read_without_a_run"] = round(
        1 - p["passes_with_news_for_the_agent"] / p["passes_with_a_change"], 3
    )
    (args.out / "threads_agent_replay_summary.json").write_text(
        json.dumps(summary, indent=1) + "\n"
    )
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
