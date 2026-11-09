# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Export CodeNib graphs and configure SeeRepo's graph tool and agent."""

from __future__ import annotations

import argparse
import os
import pickle
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from ..types import EDGE_TYPE_CONTAIN, ROOT_NODE

if TYPE_CHECKING:
    from ..compiler.manifest import RepoManifest

_GRAPH_INSTRUCTIONS = r"""### Repository Graph Visualization Tool
Query repo_graph.pkl using contains (parent to child), import (importer to
imported element), reference (element to a symbol it uses), or type-use
(element to a type it uses).
Start at / and copy exact symbol IDs from graph results:
```bash
python -m minisweagent.run.extra.utils.graph_visualization \
  --pkl repo_graph.pkl --node "/" --edge-type contains \
  --up-depth 0 --down-depth 1
```
Then query a file or symbol with --edge-type reference or --edge-type import.
Use --up-depth and --down-depth to choose how far to traverse each direction.

"""


def make_graph(code_graph):
    """Convert to NetworkX with IDs shared by labels and queries."""
    import networkx as nx

    graph = nx.MultiDiGraph()
    source = code_graph.graph
    names = {}
    for vertex in source.vs:
        attributes = vertex.attributes()
        canonical = attributes["name"]
        name = "/" if canonical == ROOT_NODE else canonical
        if name in graph:
            raise ValueError(f"ambiguous exported node ID: {name}")
        names[vertex.index] = name
        attributes["name"] = name
        graph.add_node(name, **attributes)
    for edge in source.es:
        attributes = edge.attributes()
        if attributes["type"] == EDGE_TYPE_CONTAIN:
            attributes["type"] = "contains"
        graph.add_edge(
            names[edge.source],
            names[edge.target],
            **attributes,
        )
    return graph


def write_graph(graph, output_dir: str | Path, instance_id: str) -> Path:
    """Atomically write or replace one task pickle."""
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", instance_id):
        raise ValueError("invalid instance ID")
    output = directory / f"{instance_id}.pkl"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=directory, suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            pickle.dump(graph, stream, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output


def make_config(config: dict, index_dir: str | Path) -> dict:
    """Replace graph guidance and the prebuilt index path."""
    text = config["agent"]["instance_template"]
    start = text.index("### Repository Graph Visualization Tool")
    end = text.index("## Command Execution Rules", start)
    config["agent"]["instance_template"] = (
        text[:start] + _GRAPH_INSTRUCTIONS + text[end:]
    )
    config.setdefault("graph", {}).update(
        enabled=True,
        index_dir=str(Path(index_dir).expanduser().resolve()),
    )
    return config


def select_graph_manifest(
    repo: Path,
    *,
    manifest_path: Path | None,
    base_commit: str,
    build_if_missing: bool,
) -> RepoManifest:
    """Resolve a source-bound graph, optionally preparing it through CodeNib."""
    from ..clients._manifest import load_checkout_manifest
    from ..compiler.manifest import MANIFEST_FILENAME
    from ..paths import repo_index_dir
    from ..source_fingerprint import lexical_repository_path

    repo = lexical_repository_path(repo)
    try:
        selected, manifest = load_checkout_manifest(
            repo, integration_name="SeeRepo", manifest_path=manifest_path
        )
    except FileNotFoundError:
        if not build_if_missing or manifest_path is not None:
            raise
        selected = repo_index_dir(repo) / MANIFEST_FILENAME
        manifest = None
    else:
        if not base_commit or manifest.commit != base_commit:
            raise ValueError("base commit does not match the CodeNib manifest")
        if not build_if_missing or manifest.index_is_current("symbol_graph"):
            return manifest

    from ..codegraph_preparation import prepare_graph
    from ..compiler.checkout_identity import checkout_commit
    from ..repository_filters import detect_repository_languages
    from ..repository_source_selection import RepositorySourceSelection
    from ..source_fingerprint import fingerprint_repository

    if not base_commit or checkout_commit(repo) != base_commit:
        raise ValueError(
            "base commit does not match the checkout for graph preparation"
        )
    selection = (
        RepositorySourceSelection() if manifest is None else manifest.source_selection
    )
    if selection is None:
        raise ValueError(
            "manifest has no source selection; rebuild the index explicitly"
        )
    languages = detect_repository_languages(
        repo, surface="graph", source_selection=selection
    )
    if not languages:
        raise ValueError("no graph-capable source language was detected")
    fingerprint = (
        manifest.source_fingerprint
        if manifest is not None
        else fingerprint_repository(
            repo, selection=selection, exclude_roots=(selected.parent,)
        ).value
    )
    prepare_graph(
        repo,
        languages=languages,
        source_selection=selection,
        expected_source_fingerprint=fingerprint,
        cache_dir=selected.parent,
    )
    _selected, manifest = load_checkout_manifest(
        repo, integration_name="SeeRepo", manifest_path=selected
    )
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--build-if-missing",
        action="store_true",
        help="build a missing symbol graph",
    )
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    checkout = args.upstream.expanduser().resolve()
    import yaml

    from ..compiler.graph_artifact import load_authenticated_graph_artifact

    manifest = select_graph_manifest(
        args.repo,
        manifest_path=args.manifest,
        base_commit=args.base_commit,
        build_if_missing=args.build_if_missing,
    )
    if not manifest.index_is_current("symbol_graph"):
        parser.error("symbol_graph is missing or stale; use --build-if-missing")
    graph = make_graph(
        load_authenticated_graph_artifact(manifest.indexes["symbol_graph"])
    )
    template_path = (
        args.config
        if args.config.exists()
        else checkout / "src/minisweagent/config/extra/SeeRepo.yaml"
    )
    config = make_config(yaml.safe_load(template_path.read_text()), args.output_dir)
    output = write_graph(graph, args.output_dir, args.instance_id)
    args.config.parent.mkdir(parents=True, exist_ok=True)
    args.config.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
