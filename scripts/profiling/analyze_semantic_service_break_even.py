#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Estimate when a reusable static graph repays its one-time build cost."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence


def analyze_break_even(
    build_profiles: Sequence[Mapping[str, Any]],
    replay_reports: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    replay_by_snapshot = {
        _snapshot_key(
            report.get("subject") or {},
            repo_field="repository",
            commit_field="git_commit",
        ): report
        for report in replay_reports
    }
    rows = []
    for profile in build_profiles:
        key = _snapshot_key(profile, repo_field="repo", commit_field="base_commit")
        report = replay_by_snapshot.get(key)
        if report is None:
            raise ValueError(f"no replay report for {key[0]}@{key[1]}")
        if profile.get("success") is not True:
            raise ValueError(f"static build did not succeed for {key[0]}@{key[1]}")

        setup = report.get("setup") or {}
        build_seconds = _number(profile, "wall_seconds")
        graph_load_seconds = _milliseconds(setup, "graph_load_ms")
        live_initialize_seconds = _milliseconds(setup, "live_start_ms")
        live_warmup_seconds = _milliseconds(setup, "warmup_wall_ms")
        live_session_seconds = live_initialize_seconds + live_warmup_seconds
        avoided_seconds_per_session = live_session_seconds - graph_load_seconds
        break_even_sessions = (
            math.ceil(build_seconds / avoided_seconds_per_session)
            if avoided_seconds_per_session > 0
            else None
        )
        rows.append(
            {
                "instance_id": profile.get("instance_id"),
                "repository": key[0],
                "commit": key[1],
                "language": profile.get("language"),
                "static_build_seconds": build_seconds,
                "static_graph_load_seconds": graph_load_seconds,
                "live_initialize_seconds": live_initialize_seconds,
                "live_warmup_seconds": live_warmup_seconds,
                "live_session_setup_seconds": live_session_seconds,
                "avoided_seconds_per_session": avoided_seconds_per_session,
                "break_even_sessions": break_even_sessions,
            }
        )

    break_even = [
        int(row["break_even_sessions"])
        for row in rows
        if row["break_even_sessions"] is not None
    ]
    return {
        "schema_version": 1,
        "snapshot_count": len(rows),
        "method": {
            "static_total": "build + sessions * graph_load",
            "live_total": "sessions * (initialize + behavioral_warmup)",
            "request_latency_included": False,
        },
        "rows": rows,
        "summary": {
            "break_even_sessions_median": (
                statistics.median(break_even) if break_even else None
            ),
            "break_even_sessions_max": max(break_even, default=None),
        },
    }


def render_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Semantic service break-even pilot",
        "",
        (
            "Static total = one-time graph build + per-session graph load. "
            "Live total = per-session server initialize + behavioral warmup. "
            "Request latency savings are excluded, making the estimate conservative."
        ),
        "",
        "| language | snapshot | build s | graph load s | live setup s "
        "| avoided/session s | break-even sessions |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in payload.get("rows") or []:
        lines.append(
            "| {language} | {snapshot} | {build:.2f} | {load:.3f} | "
            "{live:.2f} | {avoided:.2f} | {break_even} |".format(
                language=row.get("language") or "unknown",
                snapshot=f"{row['repository']}@{str(row['commit'])[:8]}",
                build=row["static_build_seconds"],
                load=row["static_graph_load_seconds"],
                live=row["live_session_setup_seconds"],
                avoided=row["avoided_seconds_per_session"],
                break_even=row.get("break_even_sessions") or "n/a",
            )
        )
    summary = payload.get("summary") or {}
    lines.extend(
        [
            "",
            f"Median break-even: {summary.get('break_even_sessions_median')} sessions.",
            "",
        ]
    )
    return "\n".join(lines)


def _snapshot_key(
    payload: Mapping[str, Any], *, repo_field: str, commit_field: str
) -> tuple[str, str]:
    repo = str(payload.get(repo_field) or "").strip().lower()
    commit = str(payload.get(commit_field) or "").strip().lower()
    if not repo or not commit:
        raise ValueError(
            f"missing snapshot identity fields {repo_field}/{commit_field}"
        )
    return repo, commit


def _number(payload: Mapping[str, Any], field: str) -> float:
    value = payload.get(field)
    if not isinstance(value, (int, float)):
        raise ValueError(f"missing numeric field {field}")
    return float(value)


def _milliseconds(payload: Mapping[str, Any], field: str) -> float:
    return _number(payload, field) / 1000


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-profile", action="append", type=Path, required=True)
    parser.add_argument("--replay-report", action="append", type=Path, required=True)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    args = parser.parse_args(argv)

    builds = [
        json.loads(path.read_text(encoding="utf-8")) for path in args.build_profile
    ]
    reports = [
        json.loads(path.read_text(encoding="utf-8")) for path in args.replay_report
    ]
    payload = analyze_break_even(builds, reports)
    markdown = render_markdown(payload)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    if args.output_markdown:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
