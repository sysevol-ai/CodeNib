# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the homepage's replay of a recorded agent session."""

from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.render_agent_replay import RECORD, render_replay, replace_replay

PAGE = Path("landing/index.html")


def _record() -> dict:
    return json.loads(RECORD.read_text(encoding="utf-8"))


def test_homepage_replay_is_current():
    text = PAGE.read_text(encoding="utf-8")

    assert replace_replay(text, render_replay()) == text


def test_replay_shows_the_recorded_output():
    record = _record()
    replay = render_replay(record)

    assert record["setup_command"] in replay
    for line in record["setup_output_excerpt"]:
        assert line in replay
    assert f"{record['seconds']['agent']:.1f} s" in replay


def test_graph_draws_every_recorded_node_and_edge():
    record = _record()
    replay = render_replay(record)
    graph = record["dependency_result"]

    assert len(re.findall(r'class="g-edge', replay)) == len(graph["edges"])
    assert len(re.findall(r'class="g-(?:node|root)', replay)) == len(graph["nodes"]) + 1


def test_every_source_link_is_pinned_to_the_recorded_commit():
    record = _record()
    links = re.findall(r'href="([^"]+)"', render_replay(record))

    assert links
    assert all(f"/blob/{record['source_commit']}/" in link for link in links)
