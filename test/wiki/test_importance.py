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
    main_path_start,
    page_tiers,
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


def test_main_path_starts_where_the_heaviest_public_path_begins():
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

    start = main_path_start(graph, is_test=lambda file: file.startswith("tests/"))

    assert _named(graph)[start] == "get"


def test_a_manifest_entry_point_is_preferred():
    vertices = [
        ("api", "api.py", "function"),
        ("core", "core.py", "function"),
        ("deep", "core.py", "function"),
        ("cli", "cli.py", "function"),
        ("small", "cli.py", "function"),
    ]
    edges = [
        (0, 1, EDGE_TYPE_REFERENCE),
        (1, 2, EDGE_TYPE_REFERENCE),
        (3, 4, EDGE_TYPE_REFERENCE),
    ]
    graph = _graph(vertices, edges)

    assert _named(graph)[main_path_start(graph)] == "api"
    assert _named(graph)[main_path_start(graph, entry_files=["cli.py"])] == "cli"


def test_a_project_of_only_cycles_still_has_a_start():
    graph = _graph(
        [("run", "a.py", "function"), ("step", "a.py", "function")],
        [(0, 1, EDGE_TYPE_REFERENCE), (1, 0, EDGE_TYPE_REFERENCE)],
    )

    assert main_path_start(graph) in {0, 1}


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
