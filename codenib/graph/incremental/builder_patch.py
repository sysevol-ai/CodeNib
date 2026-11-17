# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Patch a Python graph by rebuilding its changed files with the graph's builder.

The language-server patcher is exact only where it does not ask the language
server: on edited lines the server and the builder disagree on what a line
defines or refers to. This patch asks the builder instead. pyrefly reports a
file the same whether it is checked alone or with the whole project, so the
changed files are checked alone, their references are resolved through the
symbol table the whole-project build left behind, and their part of the graph
is replaced.

A change can also alter what an unchanged file refers to: a new
``Request.hooks`` takes over the references that went to the ``hooks`` it now
shadows. When the changed files gain or lose a definition, the files that
refer into them are rebuilt as well. What stays unseen is a file with no
reference into the changed files whose references still resolve differently,
such as an import that only now finds its target.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Iterable, Optional

from ...log_utils import get_logger
from ...types import node_has_definition
from ..code_graph import CodeGraph

logger = get_logger(__name__)


# Past this many referring files a rebuild of the project is the better tool.
MAX_DEPENDENT_FILES = 200


class BuilderPatchUnavailable(RuntimeError):
    """The changed files could not be rebuilt; the caller rebuilds the graph."""


def _definitions(graph: CodeGraph, files: set[str]) -> set[str]:
    """Unified names defined in ``files``; position markers do not count."""

    core = graph.graph
    if "unified_name" not in core.vs.attributes():
        return set()
    return {
        vertex["unified_name"]
        for vertex in core.vs
        if vertex["file"] in files
        and vertex["unified_name"]
        and node_has_definition(vertex.attributes())
    }


def _dependents(graph: CodeGraph, files: set[str]) -> set[str]:
    """Files outside ``files`` with a reference into them."""

    core = graph.graph
    found: set[str] = set()
    anchors = core.es["anchor_file"] if "anchor_file" in core.es.attributes() else None
    if anchors is None:
        return found
    targets = core.vs["file"]
    for index, ((_, target), kind) in enumerate(
        zip(core.get_edgelist(), core.es["type"], strict=True)
    ):
        anchor = anchors[index]
        if (
            kind == "reference"
            and targets[target] in files
            and isinstance(anchor, str)
            and anchor not in files
        ):
            found.add(anchor)
    return found


def _covered(graph: CodeGraph, paths: Iterable[str]) -> list[str]:
    """The paths the graph's build covered: those with a file vertex."""

    known = set(graph.graph.vs["name"]) if graph.graph.vcount() else set()
    return [path for path in paths if path in known]


def rebuild_changed_files(
    graph: CodeGraph,
    project_root: str | Path,
    changed_files: dict,
    *,
    symbol_table: str | Path,
    exclude_patterns: Optional[list] = None,
    follow_dependents: bool = True,
) -> dict:
    """Replace the changed Python files' part of ``graph`` in place.

    ``changed_files`` is what ``change_mgr.detect_changed_files`` returns, and
    the checkout at ``project_root`` holds the target commit. ``symbol_table``
    is the file the graph's build wrote beside it; it is updated so the next
    patch can follow this one. Returns counts.

    Raises :class:`BuilderPatchUnavailable`, leaving the graph and the symbol
    table untouched, when the builder cannot check the files alone.
    """

    from ...scip_interface.pyrefly_glean import SYMBOL_TABLE_NAME
    from ...scip_interface.scip_indexer_python import SCIPPythonIndexer

    root = Path(project_root)
    symbol_table = Path(symbol_table)
    if not symbol_table.is_file():
        raise BuilderPatchUnavailable(
            "the graph was built without a symbol table; rebuild it once"
        )
    renamed = changed_files.get("renamed", [])
    gone = _covered(
        graph, [*changed_files.get("deleted", []), *(old for old, _ in renamed)]
    )
    # A new file is rebuilt if the builder's own scope takes it: pyrefly
    # applies the project's excludes and reports nothing for a file they drop.
    present = [
        *_covered(graph, changed_files.get("modified", [])),
        *changed_files.get("added", []),
        *(new for _, new in renamed),
    ]
    present = [path for path in dict.fromkeys(present) if (root / path).is_file()]
    stats = {
        "files_rebuilt": 0,
        "files_deleted": len(gone),
        "vertices_deleted": 0,
        "edges_replaced": 0,
    }
    if not present and not gone:
        return stats

    part: Optional[CodeGraph] = None
    with tempfile.TemporaryDirectory(prefix="codenib-builder-patch-") as scratch:
        if present:
            indexer = SCIPPythonIndexer(
                project_root=root,
                output_dir=Path(scratch),
                exclude_patterns=exclude_patterns,
            )
            part = indexer.run_pipeline(
                output_file=str(Path(scratch) / "graph.pkl"),
                skip_level=None,
                report_profile=False,
                only_files=present,
                symbol_table=str(symbol_table),
                replaced_files=[*present, *gone],
            )
            if part is None:
                raise BuilderPatchUnavailable(
                    "the graph builder could not check the changed files alone"
                )
            stats["files_rebuilt"] = len(present)
            updated = Path(scratch) / SYMBOL_TABLE_NAME
            if updated.is_file():
                shutil.copyfile(
                    updated, symbol_table.with_name(symbol_table.name + ".new")
                )

    # Everything below mutates the graph; nothing above did.
    files = set(present) | set(gone)
    core = graph.graph
    defined_before = _definitions(graph, files)
    dependents = _dependents(graph, files) if follow_dependents else set()
    stale_edges = [
        edge.index for edge in core.es if edge.attributes().get("anchor_file") in files
    ]
    stats["edges_replaced"] = len(stale_edges)
    core.delete_edges(stale_edges)

    rebuilt = set(part.graph.vs["name"]) if part is not None else set()
    removed_files = set(gone)
    stale_vertices = {
        vertex.index
        for vertex in core.vs
        if vertex["name"] in removed_files
        or (
            vertex["file"] in files
            and node_has_definition(vertex.attributes())
            and vertex["name"] not in rebuilt
        )
    }
    stats["vertices_deleted"] = len(stale_vertices)
    # Same-named definitions carry their position in their name, so an
    # overload that only moved gets a new name. Files that were not rebuilt
    # still refer to it under the old one; those references follow the
    # unified name to whatever now bears it. The builder refers to every
    # same-named definition from each such site, so each site is given all
    # of them.
    unified = core.vs["unified_name"] if "unified_name" in core.vs.attributes() else []
    followers = set()
    for index in stale_vertices if unified else ():
        if not unified[index]:
            continue
        for edge_id in core.incident(index, mode="in"):
            edge = core.es[edge_id]
            attributes = edge.attributes()
            source = core.vs[edge.source]
            if (
                attributes.get("type") != "reference"
                or attributes.get("anchor_file") in files
                or source["file"] in files
                or source["name"] in files
            ):
                continue
            followers.add(
                (
                    source["name"],
                    attributes.get("anchor_file"),
                    attributes.get("anchor_line"),
                    unified[index],
                )
            )
    core.delete_vertices(sorted(stale_vertices))
    # Deleting renumbers vertices.
    graph.name_to_vertex = {name: index for index, name in enumerate(core.vs["name"])}
    graph._invalidate_edge_index()
    graph.build_range_indexes()
    if part is not None:
        graph.merge_from(part)
        if followers:
            bearers: dict[str, list[str]] = {}
            for vertex in graph.graph.vs:
                attributes = vertex.attributes()
                if attributes.get("file") in files and node_has_definition(attributes):
                    bearers.setdefault(attributes.get("unified_name"), []).append(
                        attributes["name"]
                    )
            with graph.batch_edges():
                for source, anchor_file, anchor_line, name in sorted(
                    followers, key=repr
                ):
                    for target in bearers.get(name, ()):
                        graph._add_edge(
                            source,
                            target,
                            "reference",
                            anchor_file=anchor_file,
                            anchor_line=anchor_line,
                        )
            stats["references_followed"] = len(followers)
        graph.build_range_indexes()
    pending = symbol_table.with_name(symbol_table.name + ".new")
    if pending.is_file():
        pending.replace(symbol_table)
    if dependents and _definitions(graph, files) != defined_before:
        if len(dependents) > MAX_DEPENDENT_FILES:
            # Reported, not hidden: the caller decides whether to rebuild.
            stats["dependents_skipped"] = len(dependents)
            logger.warning(
                "%d files refer into the changed files; they were not rebuilt",
                len(dependents),
            )
        else:
            again = rebuild_changed_files(
                graph,
                root,
                {"modified": sorted(dependents)},
                symbol_table=symbol_table,
                exclude_patterns=exclude_patterns,
                follow_dependents=False,
            )
            stats["dependents_rebuilt"] = again["files_rebuilt"]
    return stats
