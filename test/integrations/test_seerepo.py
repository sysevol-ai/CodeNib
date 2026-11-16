# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""SeeRepo export, source identity and renderer compatibility."""

import copy
import importlib.util
import json
import os
import pickle
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

pytest.importorskip("networkx")

from codenib.compiler.manifest import RepoManifest
from codenib.graph.code_graph import CodeGraph
from codenib.integrations import seerepo
from codenib.types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_IMPORT,
    EDGE_TYPE_REFERENCE,
    EDGE_TYPE_TYPE_USE,
    NODE_TYPE_CLASS,
    NODE_TYPE_DIRECTORY,
    NODE_TYPE_FIELD,
    NODE_TYPE_FILE,
    NODE_TYPE_METHOD,
    NODE_TYPE_SYMBOL,
    ROOT_NODE,
)


@pytest.fixture()
def graph(tmp_path):
    graph = CodeGraph(str(tmp_path))
    for name, kind in (
        (ROOT_NODE, NODE_TYPE_DIRECTORY),
        ("a.py", NODE_TYPE_FILE),
        ("scip:Box", NODE_TYPE_CLASS),
        ("scip:Box.run", NODE_TYPE_METHOD),
        ("scip:Box.value", NODE_TYPE_FIELD),
        ("scip:External", NODE_TYPE_SYMBOL),
    ):
        graph._add_vertex(name, {"type": kind, "custom": ["retained"]})
    for source, target in (
        (ROOT_NODE, "a.py"),
        ("a.py", "scip:Box"),
        ("scip:Box", "scip:Box.run"),
        ("scip:Box", "scip:Box.value"),
    ):
        graph._add_edge(source, target, EDGE_TYPE_CONTAIN)
    for line in (2, 4):
        graph._add_edge(
            "scip:Box.run",
            "scip:Box.value",
            EDGE_TYPE_REFERENCE,
            anchor_file="a.py",
            anchor_line=line,
        )
    graph._add_edge("a.py", "scip:External", EDGE_TYPE_IMPORT)
    graph._add_edge("scip:Box", "scip:External", EDGE_TYPE_TYPE_USE)
    return graph


def test_export_preserves_types_attributes_and_occurrences(graph):
    for name in ("overload:one", "overload:two"):
        graph._add_vertex(
            name, {"type": NODE_TYPE_METHOD, "unified_name": "a.py:run()"}
        )
    before = copy.deepcopy(graph.graph.to_dict_list())
    exported = seerepo.make_graph(graph)
    assert len(exported) == graph.graph.vcount()
    assert exported.number_of_edges() == graph.graph.ecount()
    for key, actual in exported.nodes(data=True):
        canonical = ROOT_NODE if key == "/" else key
        expected = graph.graph.vs.find(name=canonical).attributes()
        assert actual["name"] == key
        assert {k: v for k, v in actual.items() if k != "name"} == {
            k: v for k, v in expected.items() if k != "name"
        }
    assert exported.nodes["scip:External"]["name"] == "scip:External"
    assert {d["type"] for _, _, d in exported.edges(data=True)} == {
        "contains",
        EDGE_TYPE_IMPORT,
        EDGE_TYPE_REFERENCE,
        EDGE_TYPE_TYPE_USE,
    }
    references = exported.get_edge_data("scip:Box.run", "scip:Box.value")
    assert sorted(
        (e["anchor_file"], e["anchor_line"]) for e in references.values()
    ) == [
        ("a.py", 2),
        ("a.py", 4),
    ]
    assert graph.graph.to_dict_list() == before


def test_publication_roundtrip_and_reexport(graph, tmp_path):
    exported = seerepo.make_graph(graph)
    target = seerepo.write_graph(exported, tmp_path, "task")
    before = target.read_bytes()
    restored = pickle.loads(before)
    assert dict(restored.nodes(data=True)) == dict(exported.nodes(data=True))
    assert list(restored.edges(data=True)) == list(exported.edges(data=True))
    graph._add_vertex("b.py", {"type": NODE_TYPE_FILE})
    assert seerepo.write_graph(seerepo.make_graph(graph), tmp_path, "task") == target
    assert "b.py" in pickle.loads(target.read_bytes())
    with pytest.raises(ValueError, match="invalid instance ID"):
        seerepo.write_graph(exported, tmp_path, "../task")


def test_failed_reexport_preserves_previous_pickle(graph, tmp_path, monkeypatch):
    exported = seerepo.make_graph(graph)
    target = seerepo.write_graph(exported, tmp_path, "task")
    before = target.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("serialization failed")

    monkeypatch.setattr(seerepo.pickle, "dump", fail)
    with pytest.raises(OSError, match="serialization failed"):
        seerepo.write_graph(exported, tmp_path, "task")
    assert target.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_config_changes_only_graph_guidance_and_path(tmp_path):
    original = {
        "agent": {
            "instance_template": (
                "prefix\n### Repository Graph Visualization Tool\nold\n"
                "## Command Execution Rules\nsuffix"
            ),
            "cost_limit": 3,
        },
        "model": {"model_name": "unchanged"},
        "environment": {"environment_class": "docker"},
    }
    before = copy.deepcopy(original)
    config = seerepo.make_config(original, tmp_path)
    assert config is original
    text = config["agent"]["instance_template"]
    assert text.startswith("prefix\n")
    assert text.endswith("## Command Execution Rules\nsuffix")
    assert "--edge-type contains" in text and "--edge-type reference" in text
    assert config["graph"] == {"enabled": True, "index_dir": str(tmp_path)}
    config["agent"]["instance_template"] = before["agent"]["instance_template"]
    config.pop("graph")
    assert config == original == before


@pytest.mark.parametrize("tampered", [False, True])
def test_cli_loads_authenticated_graph_without_changing_upstream(
    integration_manifest,
    tmp_path,
    monkeypatch,
    tampered,
):
    manifest = RepoManifest.load(integration_manifest)
    load_manifest = RepoManifest.load
    reads = []

    def load(path):
        reads.append(path)
        return load_manifest(path)

    monkeypatch.setattr(RepoManifest, "load", load)
    template = tmp_path / "upstream/src/minisweagent/config/extra/SeeRepo.yaml"
    template.parent.mkdir(parents=True)
    template.write_text(
        yaml.safe_dump(
            {
                "agent": {
                    "instance_template": (
                        "### Repository Graph Visualization Tool\nold\n"
                        "## Command Execution Rules"
                    )
                }
            }
        )
    )
    upstream_before = template.read_bytes()
    options = {
        "upstream": tmp_path / "upstream",
        "repo": manifest.repo_path,
        "manifest": integration_manifest,
        "instance-id": "task",
        "base-commit": manifest.commit,
        "output-dir": tmp_path / "export",
        "config": tmp_path / "config.yaml",
    }
    argv = [s for k, v in options.items() for s in (f"--{k}", str(v))]
    if tampered:
        (Path(manifest.indexes["symbol_graph"].path) / "graph.pkl").write_bytes(b"bad")
        with pytest.raises(ValueError, match="receipt"):
            seerepo.main(argv)
        assert not (tmp_path / "export/task.pkl").exists()
        assert reads == [integration_manifest]
    else:
        assert seerepo.main(argv) == 0
        assert reads == [integration_manifest]
        exported = pickle.loads((tmp_path / "export/task.pkl").read_bytes())
        assert (
            exported.nodes["scip:service:BillingService.calculate_tax"]["type"]
            == "method"
        )
        assert {d["type"] for _, _, d in exported.edges(data=True)} == {
            "contains",
            "reference",
            "import",
            "type-use",
        }
        assert yaml.safe_load(options["config"].read_text())["graph"][
            "index_dir"
        ] == str(tmp_path / "export")
        config = yaml.safe_load(options["config"].read_text())
        config["model"] = {"model_name": "custom"}
        options["config"].write_text(yaml.safe_dump(config))
        assert seerepo.main(argv) == 0
        assert reads == [integration_manifest, integration_manifest]
        assert yaml.safe_load(options["config"].read_text()) == config
    assert template.read_bytes() == upstream_before
    assert list((tmp_path / "upstream").rglob("*.*")) == [template]


@pytest.mark.parametrize("state", ["current", "source_changed", "wrong_commit"])
def test_existing_graph_guards(integration_manifest, monkeypatch, state):
    manifest = RepoManifest.load(integration_manifest)
    root = Path(manifest.repo_path)
    if state == "source_changed":
        (root / "src/service.py").write_text("def changed(): pass\n")

    def unexpected(*args, **kwargs):
        pytest.fail("an existing graph or source mismatch must not start a build")

    monkeypatch.setattr("codenib.codegraph_preparation.prepare_graph", unexpected)
    kwargs = dict(
        manifest_path=integration_manifest,
        build_if_missing=True,
        base_commit="other" if state == "wrong_commit" else manifest.commit,
    )
    if state == "current":
        assert seerepo.select_graph_manifest(root, **kwargs) == manifest
        from codenib.clients._manifest import select_checkout_manifest

        assert (
            select_checkout_manifest(
                root, integration_name="SeeRepo", manifest_path=integration_manifest
            )
            == integration_manifest
        )
    else:
        with pytest.raises(ValueError, match="source fingerprint|base commit"):
            seerepo.select_graph_manifest(root, **kwargs)


@pytest.mark.parametrize(
    "state", ["missing_manifest", "missing_graph", "wrong_commit", "failure"]
)
def test_missing_graph_uses_existing_preparation(
    integration_manifest, monkeypatch, state
):
    from codenib.codegraph_preparation import CodeGraphPreparationError

    manifest = RepoManifest.load(integration_manifest)
    root = Path(manifest.repo_path)
    original = integration_manifest.read_bytes()
    if state == "missing_manifest":
        integration_manifest.unlink()
    else:
        manifest.indexes.pop("symbol_graph")
        manifest.save(integration_manifest)
    monkeypatch.setattr(
        "codenib.paths.repo_index_dir", lambda repo: integration_manifest.parent
    )
    monkeypatch.setattr(
        "codenib.compiler.checkout_identity.checkout_commit",
        lambda repo: "other" if state == "wrong_commit" else manifest.commit,
    )
    calls = []

    def prepare(repository, **kwargs):
        assert state != "wrong_commit"
        assert repository == root
        assert kwargs == dict(
            languages=["python"],
            source_selection=manifest.source_selection,
            expected_source_fingerprint=manifest.source_fingerprint,
            cache_dir=integration_manifest.parent,
        )
        calls.append(repository)
        if state == "failure":
            raise CodeGraphPreparationError("provider unavailable")
        integration_manifest.write_bytes(original)

    monkeypatch.setattr("codenib.codegraph_preparation.prepare_graph", prepare)
    kwargs = dict(
        manifest_path=None if state == "missing_manifest" else integration_manifest,
        base_commit=manifest.commit,
        build_if_missing=True,
    )
    if state in {"wrong_commit", "failure"}:
        with pytest.raises(
            (ValueError, CodeGraphPreparationError),
            match="base commit|provider unavailable",
        ):
            seerepo.select_graph_manifest(root, **kwargs)
    else:
        prepared = seerepo.select_graph_manifest(root, **kwargs)
        assert prepared == RepoManifest.from_dict(json.loads(original))
    assert len(calls) == (0 if state == "wrong_commit" else 1)


@pytest.fixture()
def renderer(tmp_path, monkeypatch):
    checkout = os.environ.get("SEEREPO_CHECKOUT")
    if not checkout:
        pytest.skip("set SEEREPO_CHECKOUT to an upstream clone")
    pytest.importorskip("graphviz")
    if not shutil.which("dot"):
        pytest.skip("Graphviz dot is required")
    source = subprocess.check_output(
        [
            "git",
            "-C",
            checkout,
            "show",
            "c4a2c735abcf568aa9286dfcea001641ef0d6d1a:"
            "src/minisweagent/run/extra/utils/graph_visualization.py",
        ],
        text=True,
    )
    path = tmp_path / "renderer.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("seerepo_renderer_probe", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.integration
@pytest.mark.parametrize(
    "edge_type,center",
    [
        ("contains", "/"),
        ("reference", "scip:Box.run"),
        ("import", "a.py"),
        ("type-use", "scip:Box"),
    ],
)
def test_upstream_text_and_png(
    graph, renderer, tmp_path, monkeypatch, edge_type, center
):
    target = seerepo.write_graph(seerepo.make_graph(graph), tmp_path / "index", "task")
    monkeypatch.delenv("MSWEA_REPO_GRAPH_PKL", raising=False)
    monkeypatch.delenv("MSWEA_INSTANCE_DIR", raising=False)
    args = [
        "--pkl",
        str(target),
        "--node",
        center,
        "--edge-type",
        edge_type,
        "--up-depth",
        "0",
        "--down-depth",
        "3",
        "--out-dir",
        str(tmp_path / "views"),
    ]
    runner = CliRunner()
    text = runner.invoke(renderer.app, args + ["--text"])
    assert text.exit_code == 0, text.output
    if edge_type == "contains":
        assert "scip:Box.run [method]" in text.output
        assert "scip:Box.value [field]" in text.output
    recorded = []
    original = renderer._add_edges_with_junction

    def capture(*args, **kwargs):
        recorded.append(kwargs["center_only"])
        return original(*args, **kwargs)

    monkeypatch.setattr(renderer, "_add_edges_with_junction", capture)
    image = runner.invoke(renderer.app, args)
    assert image.exit_code == 0, image.output
    payload = json.loads(image.stdout)
    assert Path(payload["out_png"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert recorded == [edge_type != "contains"]
