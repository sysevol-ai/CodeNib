# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Summarize opt-in experience events from service logs on stdin.

Counts are browser-tab visits, not people. Origins have separate sessions;
disabled telemetry, privacy signals, blocked requests and rate limiting make
these partial observations, not a census or a causal conversion estimate.
"""

import re
import sys
from collections import defaultdict

PATTERN = re.compile(
    r"experience_event event=([a-z_]+) surface=(landing|wiki) visit=([a-f0-9]{32})"
)


def summarize(lines):
    visits = defaultdict(lambda: defaultdict(dict))
    for position, line in enumerate(lines):
        match = PATTERN.search(line)
        if match:
            event, surface, visit = match.groups()
            visits[surface][visit].setdefault(event, position)
    for surface, sessions in sorted(visits.items()):
        print(f"{surface}: {len(sessions)} observed tab visits")
        for event in (
            "page_view",
            "example_view",
            "example_source_open",
            "example_github_open",
            "example_wiki_open",
            "example_share",
            "repository_submit",
            "generation_form_view",
            "generation_start",
            "first_chapter_read",
            "agent_setup_open",
        ):
            print(f"  {event}: {sum(event in seen for seen in sessions.values())}")
        for start, finish in (
            ("example_view", "example_source_open"),
            ("repository_submit", "generation_start"),
            ("generation_start", "first_chapter_read"),
        ):
            cohort = [seen for seen in sessions.values() if start in seen]
            reached = sum(finish in seen for seen in cohort)
            print(f"  {start} -> {finish}: {reached}/{len(cohort)}")
        # A meaningful next step must be observed after evidence inspection,
        # using the first occurrence of either source event in this log stream.
        # This is a partial, same-origin tab observation, not attribution.
        source_events = ("example_source_open", "example_github_open")
        inspected = [
            (seen, min(seen[event] for event in source_events if event in seen))
            for seen in sessions.values()
            if any(event in seen for event in source_events)
        ]
        for finish in ("repository_submit", "agent_setup_open"):
            reached = sum(
                finish in seen and seen[finish] > source_position
                for seen, source_position in inspected
            )
            print(f"  source_inspection -> {finish}: {reached}/{len(inspected)}")


if __name__ == "__main__":
    summarize(sys.stdin)
