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
    visits = defaultdict(lambda: defaultdict(set))
    for line in lines:
        match = PATTERN.search(line)
        if match:
            event, surface, visit = match.groups()
            visits[surface][visit].add(event)
    for surface, sessions in sorted(visits.items()):
        print(f"{surface}: {len(sessions)} observed tab visits")
        for event in (
            "page_view",
            "example_view",
            "example_source_open",
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


if __name__ == "__main__":
    summarize(sys.stdin)
