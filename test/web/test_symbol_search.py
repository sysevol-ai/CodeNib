# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from contextlib import contextmanager
from types import SimpleNamespace

import igraph
import pytest
from fastapi.testclient import TestClient

import codenib.web.app as web_app
from codenib.web.symbol_search import SymbolIndex, symbol_index

REPO_ID = "acme__widget"


class _Graph:
    def __init__(self, vertices):
        self._graph = igraph.Graph(directed=True)
        self._graph.add_vertices(len(vertices))
        for attribute in {name for vertex in vertices for name in vertex}:
            self._graph.vs[attribute] = [vertex.get(attribute) for vertex in vertices]

    def get_graph(self):
        return self._graph


def _vertex(unified, kind="function", file="src/widget/core.py", start=9, end=20):
    return {
        "name": unified,
        "unified_name": unified,
        "type": kind,
        "file": file,
        "start_line": start,
        "end_line": end,
    }


GRAPH = _Graph(
    [
        _vertex("src/widget/core.py:Session.send()", "method", start=751, end=828),
        _vertex("src/widget/core.py:send_all()", start=40),
        _vertex("src/widget/core.py:resend()", start=60),
        _vertex("src/widget/core.py:_send_raw()", start=80),
        _vertex("tests/test_core.py:send()", file="tests/test_core.py", start=3),
        _vertex("examples/demo.py:send()", file="examples/demo.py", start=1),
        _vertex("src/widget/core.py:Session", "class", start=700, end=900),
        _vertex("src/widget/core.py:timeout", "variable", start=5),
        {
            "name": "requests.compat",
            "unified_name": "requests.compat",
            "type": "module",
        },
    ]
)


def test_matches_rank_exact_names_then_prefixes_then_substrings():
    found = SymbolIndex.from_graph(GRAPH).search("send")
    names = [item["name"] for item in found]
    assert names[0] == "Session.send()"
    assert names.index("send_all()") < names.index("resend()")
    # Tests and examples follow the project's own code; private names follow public.
    assert names.index("_send_raw()") < names.index("send()")


def test_results_carry_one_based_lines_for_the_source_view():
    found = SymbolIndex.from_graph(GRAPH).search("Session.send")
    assert found == [
        {
            "name": "Session.send()",
            "kind": "method",
            "file": "src/widget/core.py",
            "line": 752,
            "end_line": 829,
        }
    ]


def test_only_definitions_with_a_place_in_the_source_are_listed():
    index = SymbolIndex.from_graph(GRAPH)
    assert index.search("timeout") == []
    assert index.search("compat") == []
    assert [item["kind"] for item in index.search("Session")][0] == "class"


def test_an_empty_query_finds_nothing_and_the_limit_is_capped():
    index = SymbolIndex.from_graph(GRAPH)
    assert index.search("   ") == []
    assert len(index.search("s", limit=2)) == 2
    assert len(index.search("s", limit=10_000)) <= 50


def test_one_index_per_loaded_graph():
    assert symbol_index(REPO_ID, GRAPH) is symbol_index(REPO_ID, GRAPH)


@pytest.fixture()
def client(monkeypatch):
    graphs = {REPO_ID: GRAPH, "acme__nograph": None}

    class Registry:
        @contextmanager
        def pin(self, repo_id):
            graph = graphs.get(repo_id, "missing")
            yield (
                None
                if graph == "missing"
                else SimpleNamespace(code_graph=lambda graph=graph: graph)
            )

    monkeypatch.setattr(web_app.app.state, "registry", Registry(), raising=False)
    return TestClient(web_app.app)


def test_route_answers_matches(client):
    response = client.get(
        f"/api/repos/{REPO_ID}/symbols", params={"q": "send", "limit": 3}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert [item["name"] for item in body["symbols"]][:1] == ["Session.send()"]
    assert len(body["symbols"]) == 3


def test_route_without_a_graph_says_so(client):
    response = client.get("/api/repos/acme__nograph/symbols", params={"q": "send"})
    assert response.json() == {"available": False, "symbols": []}


def test_route_for_an_unknown_repository_is_404(client):
    assert (
        client.get("/api/repos/nope__nope/symbols", params={"q": "x"}).status_code
        == 404
    )


def test_route_names_the_page_that_cites_each_file(client, monkeypatch):
    places = {"src/widget/core.py": {"id": "sessions", "title": "Sessions"}}
    monkeypatch.setattr(web_app, "_symbol_pages", lambda repo_id: places)
    found = client.get(f"/api/repos/{REPO_ID}/symbols", params={"q": "send"}).json()
    by_name = {item["name"]: item for item in found["symbols"]}
    assert by_name["Session.send()"]["page"] == {"id": "sessions", "title": "Sessions"}
    assert by_name["send()"]["page"] is None
