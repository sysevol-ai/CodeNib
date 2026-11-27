# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

from codenib.web.journey_areas import place_journey_areas

CITATIONS = [
    {"file": "src/api.py", "start_line": 10},
    {"file": "src/sessions.py", "start_line": 20},
    {"file": "src/unknown.py", "start_line": 30},
]
PLACES = {
    "src/api.py": {"id": "lifecycle", "title": "Request Lifecycle"},
    "src/sessions.py": {"id": "sessions", "title": "Session Orchestration"},
}


def test_names_the_area_of_an_unnamed_step_from_its_cited_file():
    markdown = "1. **`get()`**: Passes the URL on. [E1](#evidence-E1)"
    out = place_journey_areas(markdown, CITATIONS, PLACES)
    assert out == (
        "1. **`get()`** · [Request Lifecycle](?p=lifecycle): Passes the URL on. "
        "[E1](#evidence-E1)"
    )


def test_keeps_an_area_the_writer_named():
    markdown = "2. **`send()`** · [Elsewhere](?p=elsewhere): Sends it. [E2](#evidence-E2)"
    assert place_journey_areas(markdown, CITATIONS, PLACES) == markdown


def test_uses_the_first_cited_file_a_page_cites():
    markdown = "3. **`send()`**: Sends it. [E3](#evidence-E3) [E2](#evidence-E2)"
    out = place_journey_areas(markdown, CITATIONS, PLACES)
    assert "· [Session Orchestration](?p=sessions):" in out


def test_leaves_a_step_whose_sources_no_page_cites():
    markdown = "4. **`main()`**: Starts. [E3](#evidence-E3)"
    assert place_journey_areas(markdown, CITATIONS, PLACES) == markdown


def test_leaves_other_lines_alone():
    markdown = "## From `get()` to `send()`\n\nSome prose citing [E1](#evidence-E1).\n- **`x`**: y"
    assert place_journey_areas(markdown, CITATIONS, PLACES) == markdown


AREAS = [
    {"id": "core", "title": "Core", "files": ["src/core/", "src/lua.c"]},
    {"id": "lifecycle", "title": "Request Lifecycle", "files": ["src/api.py"]},
]


def test_prefers_the_area_whose_outline_holds_the_file():
    citations = [{"file": "src/core/engine.c", "start_line": 3}, {"file": "src/lua.c", "start_line": 1}]
    first = place_journey_areas("1. **`run()`**: Runs. [E1](#evidence-E1)", citations, {}, AREAS)
    assert "· [Core](?p=core):" in first
    second = place_journey_areas("2. **`lua_close()`**: Closes. [E2](#evidence-E2)", citations, PLACES, AREAS)
    assert "· [Core](?p=core):" in second


def test_falls_back_to_the_page_that_cites_the_file():
    out = place_journey_areas("1. **`send()`**: Sends. [E2](#evidence-E2)", CITATIONS, PLACES, AREAS)
    assert "· [Session Orchestration](?p=sessions):" in out
