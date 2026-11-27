# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import igraph

from codenib.types import EDGE_TYPE_CONTAIN, EDGE_TYPE_REFERENCE
from codenib.wiki.agent_wiki import AgentWiki
from codenib.wiki.importance import (
    MAX_PRIMARY_PAGES,
    TIER_DETAIL,
    TIER_KEY,
    TIER_MORE,
    TIER_PRIMARY,
    file_weights,
    is_peripheral_file,
    main_path_start,
    own_entry_files,
    page_tiers,
    path_stops,
)


def _graph(vertices, edges):
    graph = igraph.Graph(directed=True)
    graph.add_vertices(len(vertices))
    graph.vs["unified_name"] = [name for name, _file, _kind in vertices]
    graph.vs["file"] = [file for _name, file, _kind in vertices]
    graph.vs["type"] = [kind for _name, _file, kind in vertices]
    graph.add_edges([(source, target) for source, target, _kind in edges])
    graph.es["type"] = [kind for _source, _target, kind in edges]
    return graph


def _call_chain():
    vertices = [
        ("entry", "api.py", "function"),
        ("dispatch", "core.py", "function"),
        ("run", "core.py", "function"),
        ("helper", "util.py", "function"),
        ("test_entry", "tests/test_api.py", "function"),
        ("core.py", "core.py", "file"),
    ]
    edges = [
        (0, 1, EDGE_TYPE_REFERENCE),
        (1, 2, EDGE_TYPE_REFERENCE),
        (1, 3, EDGE_TYPE_REFERENCE),
        (2, 3, EDGE_TYPE_REFERENCE),
        (4, 0, EDGE_TYPE_REFERENCE),
        (5, 1, EDGE_TYPE_CONTAIN),
    ]
    return _graph(vertices, edges)


def test_code_on_the_way_through_outweighs_a_leaf_helper():
    weights = file_weights(
        _call_chain(),
        is_test=lambda file: file.startswith("tests/"),
    )

    assert weights["core.py"] > weights["util.py"]
    assert weights["core.py"] > weights["api.py"]
    assert not any(file.startswith("tests/") for file in weights)
    assert sum(weights.values()) == __import__("pytest").approx(1.0)


def test_weights_are_empty_without_a_usable_graph():
    assert file_weights(None) == {}
    assert file_weights(igraph.Graph(directed=True)) == {}
    only_containment = _graph(
        [("a", "a.py", "function"), ("b", "b.py", "function")],
        [(0, 1, EDGE_TYPE_CONTAIN)],
    )
    assert file_weights(only_containment) == {}


def _named(graph):
    return graph.vs["unified_name"]


def test_among_starts_with_a_reason_the_heaviest_public_path_wins():
    vertices = [
        ("get", "api.py", "function"),
        ("Session.request", "sessions.py", "method"),
        ("Session.send", "sessions.py", "method"),
        ("Adapter.send", "adapters.py", "method"),
        ("to_native", "utils.py", "function"),
        ("_private_entry", "misc.py", "function"),
        ("lonely_caller", "misc.py", "function"),
        ("test_get", "tests/test_api.py", "function"),
    ]
    edges = [
        (0, 1, EDGE_TYPE_REFERENCE),
        (1, 2, EDGE_TYPE_REFERENCE),
        (2, 3, EDGE_TYPE_REFERENCE),
        (1, 4, EDGE_TYPE_REFERENCE),
        (3, 4, EDGE_TYPE_REFERENCE),
        (5, 1, EDGE_TYPE_REFERENCE),
        (6, 4, EDGE_TYPE_REFERENCE),
        (7, 0, EDGE_TYPE_REFERENCE),
    ]
    graph = _graph(vertices, edges)

    start = main_path_start(
        graph,
        is_test=lambda file: file.startswith("tests/"),
        program_files=["api.py", "misc.py"],
    )

    assert _named(graph)[start] == "get"
    # Without a reason to start anywhere, the heaviest path is no answer.
    assert main_path_start(graph, is_test=lambda f: f.startswith("tests/")) is None


def test_a_declared_program_is_preferred():
    vertices = [
        ("api", "api.py", "function"),
        ("core", "core.py", "function"),
        ("deep", "core.py", "function"),
        ("cli", "cli.py", "function"),
        ("small", "cli.py", "function"),
        ("smaller", "cli.py", "function"),
    ]
    edges = [
        (0, 1, EDGE_TYPE_REFERENCE),
        (1, 2, EDGE_TYPE_REFERENCE),
        (3, 4, EDGE_TYPE_REFERENCE),
        (4, 5, EDGE_TYPE_REFERENCE),
    ]
    graph = _graph(vertices, edges)
    both = ["api.py", "cli.py"]

    assert _named(graph)[main_path_start(graph, program_files=both)] == "api"
    preferred = main_path_start(graph, program_files=both, own_files=["cli.py"])
    assert _named(graph)[preferred] == "cli"


def test_a_readme_door_is_preferred_when_its_path_leaves_the_file():
    vertices = [
        ("demo_main", "pkg/browser.py", "function"),
        ("demo_step", "pkg/browser.py", "function"),
        ("demo_tool", "pkg/browser.py", "function"),
        ("Agent.__call__", "pkg/agents.py", "method"),
        ("Agent.run", "pkg/agents.py", "method"),
        ("log", "pkg/monitoring.py", "function"),
        ("run_main", "src/run.py", "function"),
        ("score", "src/evaluate.py", "function"),
        ("report", "src/report.py", "function"),
    ]
    edges = [
        (0, 1, EDGE_TYPE_REFERENCE),
        (1, 2, EDGE_TYPE_REFERENCE),
        (3, 4, EDGE_TYPE_REFERENCE),
        (4, 5, EDGE_TYPE_REFERENCE),
        (6, 7, EDGE_TYPE_REFERENCE),
        (7, 8, EDGE_TYPE_REFERENCE),
    ]
    graph = _graph(vertices, edges)
    named = _named(graph)

    # A declared program whose path never leaves its own file still wins on
    # its own, as before ...
    assert (
        named[main_path_start(graph, program_files=["pkg/browser.py"])] == "demo_main"
    )
    # ... but a file the README links to, whose path does leave, comes first.
    linked = main_path_start(
        graph, program_files=["pkg/browser.py"], linked_files=["pkg/agents.py"]
    )
    assert named[linked] == "Agent.__call__"
    # A self-contained demo is no door even when the README links to it, and
    # once the README names doors nothing else is one either.
    assert (
        main_path_start(
            graph, program_files=["pkg/browser.py"], linked_files=["pkg/browser.py"]
        )
        is None
    )
    # What the README tells readers to run beats what it links to.
    run = main_path_start(
        graph, linked_files=["pkg/agents.py"], run_files=["src/run.py"]
    )
    assert named[run] == "run_main"


def test_a_project_of_only_cycles_still_has_a_start():
    graph = _graph(
        [
            ("run", "a.py", "function"),
            ("step", "a.py", "function"),
            ("act", "a.py", "function"),
        ],
        [
            (0, 1, EDGE_TYPE_REFERENCE),
            (1, 2, EDGE_TYPE_REFERENCE),
            (2, 0, EDGE_TYPE_REFERENCE),
        ],
    )

    assert main_path_start(graph, program_files=["a.py"]) in {0, 1, 2}


def test_no_start_without_recorded_calls():
    assert main_path_start(None) is None
    assert (
        main_path_start(
            _graph(
                [("a", "a.py", "function"), ("b", "b.py", "function")],
                [(0, 1, EDGE_TYPE_CONTAIN)],
            )
        )
        is None
    )


def _page(page_id, *files, children=()):
    return {"id": page_id, "files": list(files), "children": list(children)}


def test_tiers_list_only_the_weightiest_areas_up_front():
    pages = [
        _page("overview"),
        *(_page(f"area-{index}", f"{index}.py") for index in range(7)),
    ]
    tiers = page_tiers(pages, {f"{index}.py": float(index + 1) for index in range(7)})

    assert tiers["overview"] == TIER_PRIMARY
    assert sum(tier == TIER_PRIMARY for tier in tiers.values()) == MAX_PRIMARY_PAGES + 1
    assert tiers["area-6"] == TIER_PRIMARY
    assert tiers["area-0"] == TIER_MORE


def test_a_top_level_page_counts_the_weight_of_its_children():
    pages = [
        _page("overview"),
        *(_page(f"small-{index}", f"s{index}.py") for index in range(5)),
        _page("big", children=[_page("inner", "big.py")]),
    ]
    weights = {"big.py": 10.0, **{f"s{index}.py": 1.0 for index in range(5)}}

    tiers = page_tiers(pages, weights)

    assert tiers["big"] == TIER_PRIMARY
    assert tiers["small-4"] == TIER_MORE


def test_only_the_weightiest_children_become_key_pages():
    children = [_page(f"child-{index}", f"c{index}.py") for index in range(12)]
    pages = [_page("overview"), _page("area", children=children)]
    weights = {f"c{index}.py": float(index) for index in range(12)}

    tiers = page_tiers(pages, weights)

    assert [tiers[f"child-{index}"] for index in (11, 10, 9, 8)] == [TIER_KEY] * 4
    assert tiers["child-7"] == TIER_DETAIL
    # A file with no recorded weight is never promoted.
    assert tiers["child-0"] == TIER_DETAIL


def test_without_a_graph_tiers_follow_the_outline():
    pages = [
        _page("overview"),
        *(_page(f"area-{index}", f"{index}.py") for index in range(7)),
        _page("nested", children=[_page("child", "c.py")]),
    ]

    tiers = page_tiers(pages)

    assert tiers["area-0"] == TIER_PRIMARY
    assert tiers["area-4"] == TIER_PRIMARY
    assert tiers["area-5"] == TIER_MORE
    assert tiers["child"] == TIER_DETAIL


def _wiki(tmp_path, graph, store=None):
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        manifest=SimpleNamespace(languages=["python"], indexes={}),
        code_graph=lambda: SimpleNamespace(get_graph=lambda: graph),
    )
    wiki = AgentWiki(bundle, model="fake", llm=SimpleNamespace(), store=store)
    wiki._outline = {
        "pages": [
            {"id": "overview", "title": "Overview", "children": []},
            {
                "id": "core",
                "title": "Core",
                "files": ["core.py"],
                "children": [
                    {"id": "run", "title": "Run", "files": ["core.py"], "children": []}
                ],
            },
        ]
    }
    return wiki


def test_page_tree_reports_tiers_ranked_from_the_graph(tmp_path):
    (tmp_path / "core.py").write_text("def dispatch():\n    pass\n")
    wiki = _wiki(tmp_path, _call_chain())

    tree = wiki.page_tree()

    assert [page["tier"] for page in tree] == [TIER_PRIMARY, TIER_PRIMARY]
    assert tree[1]["children"][0]["tier"] == TIER_KEY


def test_a_cached_read_never_loads_the_graph(tmp_path):
    def forbidden():
        raise AssertionError("a cached read must not load the code graph")

    wiki = _wiki(tmp_path, None)
    wiki._bundle.code_graph = forbidden

    tree = wiki.cached_page_tree()

    assert tree[1]["tier"] == TIER_PRIMARY
    assert tree[1]["children"][0]["tier"] == TIER_DETAIL


def test_a_graph_failure_falls_back_to_outline_order(tmp_path):
    def broken():
        raise RuntimeError("index unavailable")

    wiki = _wiki(tmp_path, None)
    wiki._bundle.code_graph = broken

    tree = wiki.page_tree()

    assert tree[1]["tier"] == TIER_PRIMARY
    assert tree[1]["children"][0]["tier"] == TIER_DETAIL


def test_peripheral_files_are_the_code_around_the_project():
    for file in (
        "deps/lua/src/lua.c",
        "sklearn/externals/array_api_extra/_funcs.py",
        "examples/chat.rs",
        "tokio-test/src/lib.rs",
        "crates/ruff_dev/src/main.rs",
        "crates/nu-test-support/src/harness/mod.rs",
        "benches/spawn.rs",
        "ci/numpydoc-public-api.py",
        "src/jq_test.c",
        "crates/ty_vendored/build.rs",
        "sklearn/utils/_compat.pyi",
    ):
        assert is_peripheral_file(file), file
    for file in (
        "deps/deps.go",
        "src/requests/api.py",
        "tokio/src/task/spawn.rs",
        "crates/ruff/src/main.rs",
        "packages/babel-generator/src/printer.ts",
        "latest.py",
    ):
        assert not is_peripheral_file(file), file


def _stops_graph():
    vertices = [
        ("main", "src/main.c", "function"),
        ("serve", "src/server.c", "function"),
        ("_serverAssert", "src/debug.c", "function"),
        ("unknown", "src/span.c", "function"),
        ("free_all", "src/mem.c", "function"),
    ]
    callers = [(f"caller{index}", f"src/c{index}.c", "function") for index in range(8)]
    vertices += callers
    edges = [
        (0, 1, EDGE_TYPE_REFERENCE),
        (1, 2, EDGE_TYPE_REFERENCE),
        (1, 3, EDGE_TYPE_REFERENCE),
        (5, 2, EDGE_TYPE_REFERENCE),
        (6, 2, EDGE_TYPE_REFERENCE),
        (2, 4, EDGE_TYPE_REFERENCE),
    ]
    # free_all() is called from every caller file: a helper everything uses.
    edges += [(5 + index, 4, EDGE_TYPE_REFERENCE) for index in range(8)]
    graph = _graph(vertices, edges)
    graph.vs["start_line"] = [0, 0, 0, 0, 0] + [0] * 8
    graph.vs["end_line"] = [30, 40, 20, 2, 25] + [10] * 8
    return graph


def test_a_path_stops_before_helpers_failure_reports_and_tiny_leaves():
    graph = _stops_graph()
    named = _named(graph)

    stops = {named[vertex] for vertex in path_stops(graph)}

    assert stops == {"free_all", "_serverAssert", "unknown"}


def test_a_start_is_never_in_vendored_code_and_needs_a_reason():
    vertices = [
        ("main", "deps/lua/src/lua.c", "function"),
        ("lua_close", "deps/lua/src/lstate.c", "function"),
        ("close_state", "deps/lua/src/lstate.c", "function"),
        ("main", "src/server.c", "function"),
        ("initServer", "src/server.c", "function"),
        ("aeMain", "src/ae.c", "function"),
        ("xaddCommand", "src/t_stream.c", "function"),
        ("streamAppend", "src/t_stream.c", "function"),
        ("lpAppend", "src/listpack.c", "function"),
    ]
    edges = [
        (0, 1, EDGE_TYPE_REFERENCE),
        (1, 2, EDGE_TYPE_REFERENCE),
        (3, 4, EDGE_TYPE_REFERENCE),
        (4, 5, EDGE_TYPE_REFERENCE),
        (6, 7, EDGE_TYPE_REFERENCE),
        (7, 8, EDGE_TYPE_REFERENCE),
    ]
    graph = _graph(vertices, edges)

    start = main_path_start(graph)

    assert graph.vs[start]["file"] == "src/server.c"
    # Without the program, a command handler the code never calls has no
    # reason to be the way in.
    graph.vs[3]["file"] = "examples/server.c"
    assert main_path_start(graph) is None


def test_a_call_in_the_readme_examples_starts_the_path():
    vertices = [
        ("api.py:get()", "src/pkg/api.py", "function"),
        ("api.py:request()", "src/pkg/api.py", "function"),
        ("sessions.py:Session.send()", "src/pkg/sessions.py", "method"),
        ("cookies.py:Jar.get()", "src/pkg/cookies.py", "method"),
        ("util.py:heavy()", "src/pkg/util.py", "function"),
        ("util.py:heavier()", "src/pkg/util.py", "function"),
        ("util.py:heaviest()", "src/pkg/core.py", "function"),
    ]
    edges = [
        (0, 1, EDGE_TYPE_REFERENCE),
        (1, 2, EDGE_TYPE_REFERENCE),
        (2, 3, EDGE_TYPE_REFERENCE),
        (4, 5, EDGE_TYPE_REFERENCE),
        (5, 6, EDGE_TYPE_REFERENCE),
        (4, 6, EDGE_TYPE_REFERENCE),
    ]
    graph = _graph(vertices, edges)

    start = main_path_start(graph, readme_calls=[("pkg", "get")])

    # ``pkg.get(`` is the module's function, not ``Jar.get()``.
    assert _named(graph)[start] == "api.py:get()"


def test_a_test_beside_the_code_is_turned_away():
    vertices = [
        ("lib.rs:wakes_local()", "tokio/src/task/local.rs", "function"),
        ("lib.rs:build()", "tokio/src/runtime/builder.rs", "function"),
        ("lib.rs:launch()", "tokio/src/runtime/worker.rs", "function"),
        ("lib.rs:spawn()", "tokio/src/task/spawn.rs", "function"),
        ("lib.rs:spawn_inner()", "tokio/src/task/spawn.rs", "function"),
        ("lib.rs:schedule()", "tokio/src/runtime/handle.rs", "function"),
    ]
    edges = [
        (0, 1, EDGE_TYPE_REFERENCE),
        (1, 2, EDGE_TYPE_REFERENCE),
        (3, 4, EDGE_TYPE_REFERENCE),
        (4, 5, EDGE_TYPE_REFERENCE),
    ]
    graph = _graph(vertices, edges)
    programs = ["tokio/src/task/local.rs", "tokio/src/task/spawn.rs"]

    first = main_path_start(graph, program_files=programs)
    second = main_path_start(
        graph, program_files=programs, is_test_callable=lambda vertex: vertex == first
    )
    assert first is not None
    assert second not in (None, first)


def test_the_package_named_after_the_repository_owns_the_entry():
    entry = SimpleNamespace
    entries = [
        entry(path="crates/mdtest/src/lib.rs", label="mdtest", root=False),
        entry(path="crates/ruff/src/lib.rs", label="ruff", root=False),
        entry(path="crates/ruff_dev/src/main.rs", label="ruff_dev", root=False),
    ]
    assert own_entry_files(entries, "astral-sh/ruff") == {"crates/ruff/src/lib.rs"}
    nushell = [
        entry(path="src/main.rs", label="nu", root=True),
        entry(path="crates/nu_plugin_inc/src/main.rs", label="inc", root=False),
    ]
    assert own_entry_files(nushell, "nushell/nushell") == {"src/main.rs"}


def test_a_libraries_side_programs_are_no_reason_to_start():
    vertices = [
        ("volint.py:main()", "astropy/io/votable/volint.py", "function"),
        ("table.py:parse()", "astropy/io/votable/table.py", "function"),
        ("tree.py:check()", "astropy/io/votable/tree.py", "function"),
    ]
    edges = [(0, 1, EDGE_TYPE_REFERENCE), (1, 2, EDGE_TYPE_REFERENCE)]
    graph = _graph(vertices, edges)
    scripts = ["astropy/io/votable/volint.py"]

    assert main_path_start(graph, program_files=scripts) == 0
    library = main_path_start(
        graph, program_files=scripts, own_files=["astropy/__init__.py"]
    )
    assert library is None


def test_setting_up_and_reporting_are_side_roads():
    from codenib.wiki.importance import is_side_road

    for name in (
        "NewHugoSites()",
        "App.config()",
        "initCommands()",
        "WorkerMetrics.from_config()",
        "loadI18n()",
        "getDefaultLocaleConfig()",
        "NewDiagnostic()",
        "setup_localization_or_exit()",
    ):
        assert is_side_road(name), name
    for name in ("Session.send()", "run_controller()", "evaluate_file()", "render()", "get()"):
        assert not is_side_road(name), name


def test_path_weight_favours_the_work_over_setting_up():
    from codenib.wiki.importance import path_weight

    # bat's run(): the controller that prints files, not the listing or config.
    assert path_weight("run_controller()", 67) > path_weight("list_themes()", 107)
    assert path_weight("run_controller()", 67) > path_weight("App.config()", 123)
    assert path_weight("get()", 40) == 40
