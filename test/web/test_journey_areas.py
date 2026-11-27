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
    markdown = (
        "2. **`send()`** · [Elsewhere](?p=elsewhere): Sends it. [E2](#evidence-E2)"
    )
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
    citations = [
        {"file": "src/core/engine.c", "start_line": 3},
        {"file": "src/lua.c", "start_line": 1},
    ]
    first = place_journey_areas(
        "1. **`run()`**: Runs. [E1](#evidence-E1)", citations, {}, AREAS
    )
    assert "· [Core](?p=core):" in first
    second = place_journey_areas(
        "2. **`lua_close()`**: Closes. [E2](#evidence-E2)", citations, PLACES, AREAS
    )
    assert "· [Core](?p=core):" in second


def test_falls_back_to_the_page_that_cites_the_file():
    out = place_journey_areas(
        "1. **`send()`**: Sends. [E2](#evidence-E2)", CITATIONS, PLACES, AREAS
    )
    assert "· [Session Orchestration](?p=sessions):" in out


def test_a_file_no_area_lists_goes_with_its_folder():
    areas = [
        {"id": "runtime", "title": "Runtime", "files": ["tokio/src/runtime/handle.rs"]},
        {
            "id": "tasks",
            "title": "Task Lifecycle",
            "files": ["tokio/src/task/join_set.rs", "tokio/src/task/local.rs"],
        },
    ]
    citations = [
        {"file": "tokio/src/task/spawn.rs", "start_line": 1},
        {"file": "src/main.rs", "start_line": 1},
    ]
    out = place_journey_areas(
        "1. **`spawn()`**: Spawns. [E1](#evidence-E1)", citations, {}, areas
    )
    assert "· [Task Lifecycle](?p=tasks):" in out
    # A folder no area touches stays outside.
    main = "2. **`main()`**: Starts. [E2](#evidence-E2)"
    assert place_journey_areas(main, citations, {}, areas) == main


def test_lists_the_top_level_areas_the_path_enters_in_order():
    from codenib.web.journey_areas import journey_area_ids

    markdown = "\n".join(
        [
            "1. **`main()`**: Starts.",
            "2. **`run()`** · [CLI](?p=cli-run): Runs.",
            "3. **`check()`** · [Linter](?p=linter): Checks.",
            "4. **`lint()`** · [Rules](?p=linter-rules): Lints.",
            "5. **`exit()`** · [CLI](?p=cli): Exits.",
        ]
    )
    top_of = {
        "cli": "cli",
        "cli-run": "cli",
        "linter": "linter",
        "linter-rules": "linter",
    }
    assert journey_area_ids(markdown, top_of) == ["cli", "linter"]


def test_gives_each_architecture_role_the_area_of_its_source():
    from codenib.web.journey_areas import place_architecture_areas

    slots = [
        {"id": "x", "render_contract": {"adapter": "flow", "data": {}}},
        {
            "id": "arch",
            "render_contract": {
                "adapter": "architecture",
                "data": {
                    "primary_path": ["user", "engine"],
                    "nodes": [
                        {"id": "user", "label": "User code", "evidence": ["E3"]},
                        {"id": "engine", "label": "Engine", "evidence": ["E9", "E1"]},
                    ],
                },
            },
        },
    ]
    citations = [
        {"file": "src/core/engine.c"},
        {"file": "src/api.py"},
        {"file": "README.md"},
    ]
    out = place_architecture_areas(slots, citations, {}, AREAS)
    assert out[0] is slots[0]
    nodes = out[1]["render_contract"]["data"]["nodes"]
    assert "area" not in nodes[0]
    # The citation that placed it is recorded, so the step shows it.
    assert nodes[1]["area"] == {"id": "core", "title": "Core", "evidence": "E1"}
    # The slot it was given is left as it was.
    assert "area" not in slots[1]["render_contract"]["data"]["nodes"][1]


ROLE_AREAS = [
    {"id": "plotting", "title": "Plotting Methods", "files": ["lib/lines.py"]},
    {"id": "backends", "title": "Rendering Backends", "files": ["lib/backends/agg.py"]},
    {"id": "tsdb", "title": "Time Series Database", "files": ["tsdb/head.go"]},
    {
        "id": "trees",
        "title": "Trees, Forests, and Gradient Boosting",
        "files": ["ensemble/gb.py"],
    },
    {
        "id": "apply",
        "title": "Apply Engine",
        "files": ["internal/terraform/context_apply.go"],
    },
]


def _roles(nodes, citations):
    from codenib.web.journey_areas import place_architecture_areas

    slot = {
        "render_contract": {
            "adapter": "architecture",
            "data": {"primary_path": [], "nodes": nodes},
        }
    }
    return place_architecture_areas([slot], citations, {}, ROLE_AREAS)[0][
        "render_contract"
    ]["data"]["nodes"]


def test_a_role_named_after_an_area_takes_it_and_shows_a_source_there():
    citations = [
        {"file": "lib/lines.py"},
        {"file": "lib/backends/agg.py"},
        {"file": "README.md"},
    ]
    renderer, storage = _roles(
        [
            {"id": "r", "label": "Renderer", "evidence": ["E1", "E2"]},
            {"id": "s", "label": "Local Time Series Storage", "evidence": ["E3"]},
        ],
        citations,
    )
    assert renderer["area"] == {
        "id": "backends",
        "title": "Rendering Backends",
        "evidence": "E2",
    }
    # Placed by its name alone: no citation claims it.
    assert storage["area"] == {"id": "tsdb", "title": "Time Series Database"}


def test_one_shared_word_or_an_external_part_is_not_a_name_match():
    citations = [{"file": "README.md"}]
    loss, user = _roles(
        [
            {"id": "l", "label": "Loss and Gradient Computation", "evidence": ["E1"]},
            {
                "id": "u",
                "label": "User plotting code",
                "kind": "external",
                "evidence": ["E1"],
            },
        ],
        citations,
    )
    assert "area" not in loss and "area" not in user


def test_a_test_file_never_decides_a_role_area():
    citations = [
        {"file": "internal/terraform/context_apply2_test.go"},
        {"file": "README.md"},
    ]
    (providers,) = _roles(
        [{"id": "p", "label": "Cloud Plugins", "evidence": ["E2", "E1"]}], citations
    )
    assert "area" not in providers


def test_the_path_title_names_the_first_and_last_steps_it_heads():
    from codenib.web.journey_areas import sync_journey_title

    markdown = (
        "## From `run()` to `prepare()`\n\n1. **`run()`**: Runs. [E1](#evidence-E1)\n"
        "2. **`generate()`**: Generates. [E2](#evidence-E2)\n\n## Next\n\n3. **`x()`**: no."
    )
    assert sync_journey_title(markdown).startswith("## From `run()` to `generate()`\n")
    assert (
        sync_journey_title("## From `a()` to `b()`\n\nNo steps.")
        == "## From `a()` to `b()`\n\nNo steps."
    )


def test_a_role_named_after_a_page_inside_an_area_takes_that_area():
    from codenib.web.journey_areas import place_architecture_areas

    areas = [
        {
            "id": "sd",
            "title": "Service Discovery",
            "files": [],
            "titles": ["Service Discovery", "Discovery Manager and Target Groups"],
        },
        {
            "id": "scrape",
            "title": "Scrape Pipeline",
            "files": [],
            "titles": ["Scrape Pipeline", "Target Label Population"],
        },
    ]
    slot = {
        "render_contract": {
            "adapter": "architecture",
            "data": {
                "primary_path": [],
                "nodes": [{"id": "t", "label": "Target Discovery", "evidence": []}],
            },
        }
    }
    (node,) = place_architecture_areas([slot], [], {}, areas)[0]["render_contract"][
        "data"
    ]["nodes"]
    assert node["area"]["id"] == "sd"
