# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""The credential-free repository map: folders, recorded lines, main path."""

from __future__ import annotations

import io
import threading
import zipfile
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codenib.graph.code_graph import CodeGraph
from codenib.source_fingerprint import capture_repository_source
from codenib.storage import SQLiteWikiStore
from codenib.types import EDGE_TYPE_REFERENCE, NODE_TYPE_FUNCTION
from codenib.web.visitor_wikis import router
from codenib.wiki import (
    visitor_graph,
    visitor_map,
    visitor_provider,
    visitor_source,
    visitor_wiki,
)
from codenib.wiki.visitor_provider import WikiRunStopped
from codenib.wiki.visitor_wiki import VisitorWikiError, VisitorWikis

COMMIT = "c" * 40

# A tiny program whose recorded calls run cli -> app -> engine -> store.
SOURCE = {
    "cli.py": 'def main():\n    """Run the tool."""\n    run()\n',
    "pkg/app.py": "def run():\n    engine = 1\n    process()\n    return engine\n",
    "pkg/engine.py": (
        "def process():\n    value = 1\n    value += 1\n    save()\n    return value\n"
    ),
    "pkg/store.py": (
        "def save():\n    data = []\n    data.append(1)\n    data.append(2)\n"
        "    data.append(3)\n    data.append(4)\n    return data\n"
    ),
    "pkg/extra/report.py": (
        "def report():\n    value = save()\n    return value\n\n\n"
        "def summary():\n    return report()\n"
    ),
    "tests/test_app.py": "def test_run():\n    run()\n",
}
FUNCTIONS = [
    # (file, function, first line, last line), 0-based lines as the index has
    ("cli.py", "main", 0, 2),
    ("pkg/app.py", "run", 0, 3),
    ("pkg/engine.py", "process", 0, 4),
    ("pkg/store.py", "save", 0, 6),
    ("pkg/extra/report.py", "report", 0, 2),
    ("pkg/extra/report.py", "summary", 5, 6),
    ("tests/test_app.py", "test_run", 0, 1),
]
CALLS = [
    # (caller, callee, 0-based anchor line in the caller's file)
    ("cli.py:main()", "pkg/app.py:run()", 2),
    ("pkg/app.py:run()", "pkg/engine.py:process()", 2),
    ("pkg/engine.py:process()", "pkg/store.py:save()", 3),
    ("pkg/extra/report.py:report()", "pkg/store.py:save()", 1),
    ("pkg/extra/report.py:summary()", "pkg/extra/report.py:report()", 6),
    ("tests/test_app.py:test_run()", "pkg/app.py:run()", 1),
]


def code_graph(functions=FUNCTIONS, calls=CALLS):
    graph = CodeGraph()
    for file, name, start, end in functions:
        symbol = f"{file}:{name}()"
        graph._add_vertex(
            symbol,
            {
                "type": NODE_TYPE_FUNCTION,
                "file": file,
                "start_line": start,
                "end_line": end,
                "unified_name": symbol,
            },
        )
    for caller, callee, line in calls:
        graph._add_edge(
            caller,
            callee,
            EDGE_TYPE_REFERENCE,
            anchor_file=caller.split(":", 1)[0],
            anchor_line=line,
        )
    graph.build_range_indexes()
    return graph


def area_ids(areas):
    return [area["id"] for area in areas]


# -- folder areas -------------------------------------------------------------


def test_a_flat_src_package_maps_its_files_and_leaves_tests_docs_and_imports_out():
    paths = [
        "src/pkg/__init__.py",
        "src/pkg/api.py",
        "src/pkg/sessions.py",
        "src/pkg/models.py",
        "src/pkg/utils.py",
        "tests/test_api.py",
        "docs/conf.py",
        "setup.py",
    ]
    weights = {
        "src/pkg/api.py": 3,
        "src/pkg/sessions.py": 9,
        "src/pkg/models.py": 6,
        "src/pkg/utils.py": 2,
        "tests/test_api.py": 4,
    }
    file_area, areas = visitor_map.folder_areas(
        paths, repository="owner/pkg", weights=weights
    )
    assert [area["title"] for area in areas] == [
        "pkg/sessions.py",
        "pkg/models.py",
        "pkg/api.py",
        "pkg/utils.py",
    ]
    assert {area["kind"] for area in areas} == {"file"}
    assert areas[0]["path"] == "src/pkg/sessions.py"
    assert areas[0]["id"] == "pkg-sessions-py"
    # An __init__.py with no definitions draws no box; tests, docs and the
    # build script belong to no area at all.
    assert set(file_area) == {
        "src/pkg/api.py",
        "src/pkg/sessions.py",
        "src/pkg/models.py",
        "src/pkg/utils.py",
    }


def test_a_flat_package_past_twelve_files_keeps_the_heaviest_and_an_other():
    paths = [f"pkg/m{index:02d}.py" for index in range(20)]
    weights = {path: 100 - index for index, path in enumerate(paths)}
    file_area, areas = visitor_map.folder_areas(paths, weights=weights)
    assert len(areas) == visitor_map.MAX_AREAS
    assert [area["title"] for area in areas[:2]] == ["pkg/m00.py", "pkg/m01.py"]
    other = areas[-1]
    assert other == {
        "id": "other",
        "title": "Other",
        "path": None,
        "kind": "other",
        "files": [f"pkg/m{index:02d}.py" for index in range(11, 20)],
    }
    assert file_area["pkg/m19.py"] == "other"


def test_main_path_files_keep_their_own_area_in_a_flat_package():
    paths = [f"pkg/m{index:02d}.py" for index in range(20)]
    weights = {path: 100 - index for index, path in enumerate(paths)}
    file_area, areas = visitor_map.folder_areas(
        paths, weights=weights, pinned=["pkg/m19.py"]
    )
    assert len(areas) == visitor_map.MAX_AREAS
    assert file_area["pkg/m19.py"] == "pkg-m19-py"
    assert file_area["pkg/m09.py"] == "pkg-m09-py"
    assert file_area["pkg/m10.py"] == "other"


def test_a_src_layout_with_subpackages_opens_the_package_folder():
    paths = [
        "src/pkg/__init__.py",
        "src/pkg/core.py",
        "src/pkg/cli/main.py",
        "src/pkg/cli/args.py",
        "src/pkg/io/read.py",
        "src/pkg/io/write.py",
        "src/pkg/net/http.py",
        "src/pkg/net/tls/certs.py",
        "examples/demo.py",
        "benchmarks/bench_io.py",
    ]
    file_area, areas = visitor_map.folder_areas(paths, repository="owner/pkg")
    assert area_ids(areas) == ["pkg", "pkg-cli", "pkg-io", "pkg-net"]
    assert [area["path"] for area in areas] == [
        "src/pkg",
        "src/pkg/cli",
        "src/pkg/io",
        "src/pkg/net",
    ]
    assert {area["kind"] for area in areas} == {"folder"}
    assert file_area["src/pkg/net/tls/certs.py"] == "pkg-net"
    assert "examples/demo.py" not in file_area
    assert "benchmarks/bench_io.py" not in file_area


def test_root_code_is_named_after_the_repository():
    paths = [
        "manage.py",
        "core/a.py",
        "core/b.py",
        "web/c.py",
        "web/d.py",
        "jobs/e.py",
    ]
    file_area, areas = visitor_map.folder_areas(paths, repository="owner/proj")
    root = next(area for area in areas if area["kind"] == "root")
    assert root["id"] == "root"
    assert root["title"] == "proj"
    assert root["files"] == ["manage.py"]
    assert file_area["manage.py"] == "root"
    assert sorted(area_ids(areas)) == ["core", "jobs", "root", "web"]


def test_many_folders_merge_the_tiny_then_the_smallest_into_other():
    paths = [f"f{index:02d}/m{item}.py" for index in range(11) for item in range(10)]
    paths += [f"f11/m{item}.py" for item in range(5)]
    paths += ["t1/x.py", "t2/x.py", "other/x.py"]
    file_area, areas = visitor_map.folder_areas(paths)
    assert len(areas) == visitor_map.MAX_AREAS
    assert area_ids(areas)[:11] == [f"f{index:02d}" for index in range(11)]
    other = areas[-1]
    assert other["id"] == "other" and other["kind"] == "other"
    # Folders under 3% of the files (one file of 118) go first; then the
    # smallest remaining folder, so that twelve areas remain.
    assert len(other["files"]) == 8
    assert file_area["f11/m0.py"] == "other"
    assert file_area["other/x.py"] == "other"


def test_a_folder_named_other_does_not_take_the_special_id():
    paths = ["other/a.py", "other/b.py", "core/c.py", "core/d.py", "web/e.py"]
    _file_area, areas = visitor_map.folder_areas(paths)
    assert sorted(area_ids(areas)) == ["core", "other-2", "web"]


def test_only_peripheral_code_has_no_areas():
    assert visitor_map.folder_areas(["tests/test_a.py", "docs/conf.py"]) == ({}, [])


# -- lines between areas -----------------------------------------------------


def test_area_links_count_references_as_the_wiki_map_does():
    graph = code_graph(
        calls=[
            *CALLS,
            # A second call site of the same pair adds weight, not a pair.
            ("pkg/engine.py:process()", "pkg/store.py:save()", 4),
        ]
    )
    file_area = {
        "cli.py": "root",
        "pkg/app.py": "core",
        "pkg/engine.py": "core",
        "pkg/store.py": "store",
        "pkg/extra/report.py": "store",
    }
    areas = [
        {"id": "root", "title": "proj", "path": "", "kind": "root"},
        {"id": "core", "title": "core", "path": "pkg", "kind": "folder"},
        {"id": "store", "title": "store", "path": "pkg/store", "kind": "folder"},
    ]
    result = visitor_map.area_links(graph, file_area, areas)
    assert result["available"] is True and result["reason"] is None
    rows = {area["id"]: area for area in result["areas"]}
    assert rows["core"] == {
        "id": "core",
        "title": "core",
        "symbols": 2,
        "files": 2,
        "path": "pkg",
        "kind": "folder",
    }
    links = {(link["source"], link["target"]): link for link in result["links"]}
    # Same-area calls (summary -> report, run -> process) draw no line, and
    # the test file's call into run() is not counted.
    assert set(links) == {("root", "core"), ("core", "store")}
    assert links[("core", "store")]["weight"] == 2
    assert links[("core", "store")]["calls"] == 1
    assert links[("root", "core")]["example"] == {
        "source": "cli.py:main()",
        "target": "pkg/app.py:run()",
        "anchor": {"file": "cli.py", "line": 3},
    }


def test_areas_without_cross_area_references_say_so():
    graph = code_graph()
    result = visitor_map.area_links(
        graph, {path: "all" for path in SOURCE if not path.startswith("tests/")}
    )
    assert result["available"] is False
    assert result["reason"] == "no_cross_area_calls"
    assert result["links"] == []


# -- main path ---------------------------------------------------------------


@contextmanager
def source_bundle(tmp_path, files=SOURCE, graph=None):
    for path, text in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    with capture_repository_source(tmp_path) as binding:
        built = code_graph() if graph is None else graph
        yield SimpleNamespace(
            entry=SimpleNamespace(
                repo="owner/proj",
                repo_dir=str(tmp_path),
                language="python",
                base_commit=COMMIT,
                commit_short=COMMIT[:8],
                instance_id="map",
            ),
            manifest=None,
            source_reader=binding.borrow_reader(),
            code_graph=lambda: built,
        )


@pytest.fixture
def no_model(monkeypatch):
    """Any model or provider construction fails the test."""
    litellm = pytest.importorskip("litellm")

    def refuse(*_args, **_kwargs):
        pytest.fail("The free map must not call a model")

    monkeypatch.setattr(litellm, "completion", refuse)
    monkeypatch.setattr(litellm, "acompletion", refuse)
    monkeypatch.setattr(visitor_provider.VisitorProvider, "__init__", refuse)


def test_main_path_follows_recorded_calls_with_1_based_lines(tmp_path, no_model):
    file_area = {"cli.py": "root", "pkg/app.py": "core", "pkg/engine.py": "core"}
    with source_bundle(tmp_path) as bundle:
        path = visitor_map.main_path(bundle, file_area)
    assert path["title"] == "From main() to save()"
    assert path["stops"] == [
        {
            "symbol": "main()",
            "kind": "function",
            "file": "cli.py",
            "line": 1,
            "area": "root",
            "call": None,
        },
        {
            "symbol": "run()",
            "kind": "function",
            "file": "pkg/app.py",
            "line": 1,
            "area": "core",
            "call": {"file": "cli.py", "line": 3},
        },
        {
            "symbol": "process()",
            "kind": "function",
            "file": "pkg/engine.py",
            "line": 1,
            "area": "core",
            "call": {"file": "pkg/app.py", "line": 3},
        },
        {
            "symbol": "save()",
            "kind": "function",
            "file": "pkg/store.py",
            "line": 1,
            # A file outside every area has no area.
            "area": None,
            "call": {"file": "pkg/engine.py", "line": 4},
        },
    ]


def test_no_start_means_no_path(tmp_path, no_model):
    # Without a program, manifest or README door the graph names no start.
    graph = code_graph(
        functions=[f for f in FUNCTIONS if f[1] != "main"],
        calls=[c for c in CALLS if not c[0].startswith("cli.py")],
    )
    with source_bundle(tmp_path, graph=graph) as bundle:
        assert visitor_map.main_path(bundle, {}) is None


def test_the_path_cannot_reach_a_model():
    guard = visitor_map._NoModel()
    with pytest.raises(visitor_map.ModelCallRefused):
        guard.completion()
    with pytest.raises(visitor_map.ModelCallRefused):
        guard("prompt")
    assert guard.cache_identity == "visitor-map-no-model"


def test_readable_symbols_drop_the_file():
    assert (
        visitor_map.readable_symbol(
            "requests/sessions.py:Session.send()", "requests/sessions.py"
        )
        == "Session.send()"
    )
    assert visitor_map.readable_symbol("pkg/api.py:get()") == "get()"
    assert visitor_map.readable_symbol("Session.send()") == "Session.send()"


# -- map_repository: source to map, no graph needed for other languages ------


def source_archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(f"repo/{path}", content)
    return output.getvalue()


@pytest.fixture
def archive(monkeypatch):
    files = dict(SOURCE)
    monkeypatch.setattr(visitor_source, "repository_revision", lambda *_: COMMIT)
    monkeypatch.setattr(
        visitor_source, "github_bytes", lambda *_: source_archive(files)
    )
    roots = []
    original = visitor_source.capture_repository_source

    @contextmanager
    def capture(root, **kwargs):
        roots.append(root)
        with original(root, **kwargs) as binding:
            yield binding

    monkeypatch.setattr(visitor_source, "capture_repository_source", capture)
    return SimpleNamespace(files=files, roots=roots)


def fake_index(monkeypatch, graph=None, coverage=None):
    calls = []

    def build(bundle, check, progress):
        calls.append(bundle.entry.repo)
        bundle.graph_coverage = coverage or {
            "available": True,
            "reason": None,
            "note": "fixture",
            "files": 6,
            "nodes": 7,
            "edges": 6,
        }
        if graph is not None:
            bundle.code_graph = lambda: graph

    monkeypatch.setattr(visitor_graph, "build_visitor_graph", build)
    return calls


def test_python_source_maps_folders_lines_and_path_without_a_model(
    archive, monkeypatch, no_model
):
    indexed = fake_index(monkeypatch, graph=code_graph())
    result = visitor_source.map_repository("owner/proj", lambda: None)
    assert indexed == ["owner/proj"]
    assert result["eligible"] is True
    assert result["commit"] == COMMIT
    assert result["languages"] == ["python"]
    assert result["source_files"] == len(SOURCE)
    assert result["source_tree"]["files"] == len(SOURCE)
    assert result["code_index"]["available"] is True
    site = result["map"]
    assert site["available"] is True and site["reason"] is None
    # pkg/ holds most files, so the map opens it; with one subfolder it is a
    # flat package and its files are the areas. The root's cli.py is named
    # after the repository; report.py's two definitions rank pkg/extra first.
    assert area_ids(site["areas"]) == [
        "pkg-extra",
        "pkg-app-py",
        "pkg-engine-py",
        "pkg-store-py",
        "root",
    ]
    assert all(
        set(area) == {"id", "title", "path", "kind", "symbols", "files"}
        for area in site["areas"]
    )
    links = {(link["source"], link["target"]) for link in site["links"]}
    assert links == {
        ("root", "pkg-app-py"),
        ("pkg-app-py", "pkg-engine-py"),
        ("pkg-engine-py", "pkg-store-py"),
        ("pkg-extra", "pkg-store-py"),
    }
    stops = result["path"]["stops"]
    assert [stop["symbol"] for stop in stops] == [
        "main()",
        "run()",
        "process()",
        "save()",
    ]
    assert [stop["area"] for stop in stops] == [
        "root",
        "pkg-app-py",
        "pkg-engine-py",
        "pkg-store-py",
    ]
    assert "no model was called" in result["message"]
    assert archive.roots and all(not root.exists() for root in archive.roots)


def test_other_languages_return_the_folder_outline_without_indexing(
    archive, monkeypatch
):
    archive.files.clear()
    archive.files.update(
        {
            "cmd/tool/main.go": "package main\n\nfunc main() {}\n",
            "internal/run/run.go": "package run\n\nfunc Run() {}\n",
            "internal/store/store.go": "package store\n\nfunc Save() {}\n",
            "scripts/release.py": "def release():\n    return 1\n",
        }
    )
    monkeypatch.setattr(
        visitor_graph,
        "build_visitor_graph",
        lambda *_: pytest.fail("Other languages must not be indexed"),
    )
    result = visitor_source.map_repository("owner/tool", lambda: None)
    assert result["eligible"] is True
    assert result["map"]["available"] is False
    assert result["map"]["reason"] == "graph_language"
    assert result["map"]["links"] == []
    # scripts/ is tooling, so only the two Go folders are areas.
    assert [area["title"] for area in result["map"]["areas"]] == ["internal", "cmd"]
    assert result["path"] is None
    assert archive.roots and all(not root.exists() for root in archive.roots)


@pytest.mark.parametrize("reason", ["tool_unavailable", "index_failed", "timeout"])
def test_a_graph_that_cannot_be_built_keeps_the_areas_and_says_why(
    archive, monkeypatch, reason
):
    fake_index(
        monkeypatch,
        coverage={"available": False, "reason": reason, "note": "No index."},
    )
    result = visitor_source.map_repository("owner/proj", lambda: None)
    assert result["eligible"] is True
    assert result["map"]["reason"] == reason
    assert result["map"]["areas"] and result["map"]["links"] == []
    assert result["path"] is None
    assert result["message"] == "No index."


def test_running_out_of_time_while_indexing_keeps_the_areas(archive, monkeypatch):
    def stopped(*_):
        raise WikiRunStopped("The repository map took too long.")

    monkeypatch.setattr(visitor_graph, "build_visitor_graph", stopped)
    result = visitor_source.map_repository("owner/proj", lambda: None)
    assert result["eligible"] is True
    assert result["map"]["reason"] == "timeout"
    assert result["map"]["areas"]
    assert result["message"] == "The repository map took too long."
    assert archive.roots and all(not root.exists() for root in archive.roots)


def test_rejected_source_has_an_empty_map(archive, monkeypatch):
    monkeypatch.setattr(visitor_source, "MAX_FILES", 0)
    result = visitor_source.map_repository("owner/proj", lambda: None)
    assert result["eligible"] is False
    assert result["map"] == {
        "available": False,
        "reason": "not_eligible",
        "areas": [],
        "links": [],
    }
    assert result["path"] is None


# -- admission ---------------------------------------------------------------


def test_check_and_map_from_one_visitor_do_not_rate_limit_each_other(tmp_path):
    manager = VisitorWikis(
        SQLiteWikiStore(tmp_path / "wiki.sqlite3"),
        inspect_repository=lambda *_: {"eligible": True},
        preview_map=lambda *_: {"eligible": True, "map": {}},
    )
    assert manager.check_repository("owner/repo", "client") == {"eligible": True}
    assert manager.map_preview("owner/repo", "client")["eligible"] is True
    # Each keeps its own minute per client.
    with pytest.raises(VisitorWikiError, match=r"Please wait \d+ seconds"):
        manager.map_preview("owner/repo", "client")
    with pytest.raises(VisitorWikiError, match=r"Please wait \d+ seconds"):
        manager.check_repository("owner/repo", "client")
    assert not manager._active
    assert not manager.store.scan()


def test_a_map_holds_one_of_the_two_shared_source_slots(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    failures = []

    def slow_map(repository, check):
        check()
        entered.set()
        assert release.wait(5)
        return {"eligible": True}

    manager = VisitorWikis(
        SQLiteWikiStore(tmp_path / "wiki.sqlite3"),
        inspect_repository=lambda *_: {"eligible": True},
        preview_map=slow_map,
    )
    manager._active.add("submission:already-running")

    def run():
        try:
            manager.map_preview("owner/repo", "mapper")
        except Exception as exc:  # noqa: BLE001 - reported below
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(VisitorWikiError, match="busy"):
            manager.check_repository("owner/other", "checker")
        with pytest.raises(VisitorWikiError, match="busy"):
            manager.map_preview("owner/other", "another")
    finally:
        release.set()
        thread.join(5)
    assert not failures
    assert manager._active == {"submission:already-running"}


def test_the_map_has_its_own_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(visitor_wiki, "_MAP_SECONDS", 0)
    manager = VisitorWikis(
        SQLiteWikiStore(tmp_path / "wiki.sqlite3"),
        preview_map=lambda *_: pytest.fail("A late map must not start"),
    )
    with pytest.raises(VisitorWikiError, match="map took too long"):
        manager.map_preview("owner/repo", "client")
    assert not manager._active


# -- route -------------------------------------------------------------------


def app_with(manager):
    app = FastAPI()
    app.include_router(router)
    app.state.visitor_wikis = manager
    return TestClient(app)


@pytest.fixture
def enabled(monkeypatch):
    from codenib.agent.runtime import grep_jev

    monkeypatch.setenv("CODENIB_VISITOR_WIKI", "1")
    monkeypatch.setattr(grep_jev, "resolve_ripgrep", lambda: "/usr/bin/rg")


def test_the_route_is_off_unless_the_operator_turns_it_on(
    tmp_path, monkeypatch, enabled
):
    monkeypatch.delenv("CODENIB_VISITOR_FREE_MAP", raising=False)
    client = app_with(
        VisitorWikis(
            SQLiteWikiStore(tmp_path / "wiki.sqlite3"),
            preview_map=lambda *_: pytest.fail("A disabled map must not run"),
        )
    )
    response = client.get("/api/visitor-wikis/map", params={"repository": "o/r"})
    assert response.status_code == 404
    assert response.json()["detail"] == "The free repository map is not enabled here."
    assert client.get("/api/visitor-wikis").json()["free_map"] is False


def test_the_route_needs_visitor_wikis_as_well(tmp_path, monkeypatch):
    monkeypatch.setenv("CODENIB_VISITOR_FREE_MAP", "1")
    monkeypatch.delenv("CODENIB_VISITOR_WIKI", raising=False)
    client = app_with(VisitorWikis(SQLiteWikiStore(tmp_path / "wiki.sqlite3")))
    response = client.get("/api/visitor-wikis/map", params={"repository": "o/r"})
    assert response.status_code == 503
    assert client.get("/api/visitor-wikis").json()["free_map"] is False


def test_the_route_maps_without_a_key_and_rate_limits_with_retry_after(
    archive, tmp_path, monkeypatch, enabled, no_model
):
    monkeypatch.setenv("CODENIB_VISITOR_FREE_MAP", "1")
    fake_index(monkeypatch, graph=code_graph())
    store = SQLiteWikiStore(tmp_path / "wiki.sqlite3")
    client = app_with(
        VisitorWikis(
            store,
            verify=lambda _: pytest.fail("No key is checked"),
            wiki_factory=lambda *_a, **_k: pytest.fail("No Wiki is written"),
        )
    )
    assert client.get("/api/visitor-wikis").json()["free_map"] is True
    response = client.get("/api/visitor-wikis/map", params={"repository": "owner/proj"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
    body = response.json()
    assert set(body) >= {
        "eligible",
        "commit",
        "languages",
        "source_files",
        "source_tree",
        "map",
        "path",
        "message",
    }
    assert set(body["map"]) >= {"available", "reason", "areas", "links"}
    assert body["map"]["available"] is True
    assert set(body["path"]) == {"title", "stops"}
    assert not store.scan()
    limited = client.get("/api/visitor-wikis/map", params={"repository": "owner/proj"})
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    # The page's own check is still admitted after its map.
    checked = client.get(
        "/api/visitor-wikis/check", params={"repository": "owner/proj"}
    )
    assert checked.status_code == 200
    invalid = client.get("/api/visitor-wikis/map", params={"repository": "../repo"})
    assert invalid.status_code == 422
