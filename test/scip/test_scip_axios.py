#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration test for axios/axios SCIP indexing — verify start_line values
for convertValue and toFormData in lib/helpers/toFormData.js.

These functions live in the same file and their graph node start_line values
should match the actual source (SCIP enclosing_range is 0-based).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from codenib.dataset.swebench_multilingual import SwebenchMultilingualDataset
from codenib.ls_router import LSIndexer
from codenib.types import is_symbol_node

pytestmark = pytest.mark.integration_serial


def _pick_axios_instance() -> dict:
    """Pick the axios__axios-4738 instance from SWE-bench Multilingual."""
    dataset_obj = SwebenchMultilingualDataset(
        split="test", filter_instance="axios__axios-4738"
    )
    rows = dataset_obj.load()
    if len(rows) == 0:
        raise RuntimeError("axios__axios-4738 not found in SWE-bench_Multilingual")
    return dict(rows[0])


def _find_node_by_symbol(ig, symbol_name, file_path=None, node_type=None):
    """Find a graph node whose symbol name ends with *symbol_name*.

    Matches against the last segment of unified_name (after the last ':')
    to avoid false matches against file paths in the name.
    """
    for v in ig.vs:
        attrs = v.attributes()
        unified = attrs.get("unified_name") or ""
        # Extract symbol portion: everything after the last ':'
        symbol_part = unified.rsplit(":", 1)[-1] if ":" in unified else unified
        # Also try the node name
        name = v["name"]
        name_part = name.rsplit(":", 1)[-1] if ":" in name else name

        # Match symbol name (strip trailing parens for comparison)
        symbol_clean = symbol_part.rstrip("()")
        name_clean = name_part.rstrip("()")

        if symbol_name not in (symbol_clean, name_clean):
            continue

        # Optional file path filter
        if file_path is not None:
            node_file = attrs.get("file", "")
            if file_path not in node_file:
                continue

        # Optional type filter
        if node_type is not None and attrs.get("type") != node_type:
            continue

        return v
    return None


def _find_nodes_in_file(ig, file_path):
    """Return all symbol nodes in a given file path."""
    results = []
    for v in ig.vs:
        attrs = v.attributes()
        if not is_symbol_node(attrs.get("type", "")):
            continue
        node_file = attrs.get("file", "")
        if file_path in node_file:
            results.append(v)
    return results


@pytest.fixture(scope="module")
def axios_graph(tmp_path_factory):
    """Build the SCIP code graph for the axios/axios repo."""
    if not shutil.which("scip-typescript"):
        pytest.skip("scip-typescript not available in PATH")

    try:
        instance = _pick_axios_instance()
    except Exception as exc:
        pytest.skip(f"SWE-bench_Multilingual unavailable: {exc}")

    dataset_obj = SwebenchMultilingualDataset(split="test", filter_instance=".*")
    dataset_obj.process_instance(instance)
    repo_path = Path(dataset_obj.get_repo_path(instance))

    # Verify the correct commit is checked out
    expected_commit = instance["base_commit"]
    actual_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert actual_commit.startswith(expected_commit) or expected_commit.startswith(
        actual_commit
    ), (
        f"Commit mismatch: expected {expected_commit}, got {actual_commit}. "
        f"process_instance may have failed to checkout the correct commit."
    )

    tmp_path = tmp_path_factory.mktemp("scip_axios")
    indexer = LSIndexer(
        project_root=repo_path,
        output_dir=tmp_path,
        language="ts",
    )
    graph = indexer.run_pipeline(
        skip_level="graph", report_profile=False, infer_tsconfig=True
    )
    assert graph is not None, "run_pipeline returned None for axios"
    return graph, repo_path


def test_dump_toFormData_file_nodes(axios_graph):
    """Diagnostic: dump all symbol nodes in lib/helpers/toFormData.js to
    understand what the SCIP decoder produced."""
    graph, repo_path = axios_graph
    ig = graph.graph

    nodes = _find_nodes_in_file(ig, "toFormData.js")
    print(f"\n=== All symbol nodes in toFormData.js ({len(nodes)} found) ===")
    for v in sorted(nodes, key=lambda v: v.attributes().get("start_line") or 0):
        attrs = v.attributes()
        print(
            f"  name={v['name']!r}\n"
            f"    unified_name={attrs.get('unified_name')!r}\n"
            f"    type={attrs.get('type')!r}\n"
            f"    file={attrs.get('file')!r}\n"
            f"    start_line={attrs.get('start_line')}, end_line={attrs.get('end_line')}"
        )
    assert len(nodes) > 0, "No symbol nodes found in toFormData.js"


def test_toFormData_start_line(axios_graph):
    """toFormData should start at line 48 (0-based) in lib/helpers/toFormData.js."""
    graph, repo_path = axios_graph
    ig = graph.graph

    # Match by symbol name in the correct file
    node = _find_node_by_symbol(ig, "toFormData", file_path="toFormData.js")
    toFormData_nodes = [
        (v["name"], v.attributes().get("type"), v.attributes().get("start_line"))
        for v in ig.vs
        if "toFormData" in v["name"]
    ]
    assert node is not None, (
        "toFormData function node not found in toFormData.js; "
        f"all toFormData nodes: {toFormData_nodes}"
    )

    attrs = node.attributes()
    start_line = attrs.get("start_line")
    end_line = attrs.get("end_line")
    file_path = attrs.get("file")

    print(f"toFormData: file={file_path}, start_line={start_line}, end_line={end_line}")
    print(f"  node name: {node['name']}")
    print(f"  unified_name: {attrs.get('unified_name')}")
    print(f"  type: {attrs.get('type')}")

    assert start_line is not None, "toFormData has no start_line"
    assert end_line is not None, "toFormData has no end_line"
    assert file_path is not None, "toFormData has no file attribute"

    # Verify start_line matches expected position (0-based from SCIP).
    # toFormData starts at line 48 in 1-based editors, i.e. line 47 in 0-based.
    assert start_line == 47, (
        f"toFormData start_line expected 47 (0-based), got {start_line}; "
        f"this may indicate a line-numbering convention mismatch"
    )


def test_graph_start_line_vs_source(axios_graph):
    """Cross-check that graph start_line values can correctly index into
    the source file — verifying the 0-based vs 1-based convention."""
    graph, repo_path = axios_graph
    ig = graph.graph

    for symbol_name in ("convertValue", "toFormData"):
        node = _find_node_by_symbol(ig, symbol_name, file_path="toFormData.js")
        if node is None:
            print(f"  {symbol_name}: node not found, skipping")
            continue

        attrs = node.attributes()
        start_line = attrs.get("start_line")
        file_path = attrs.get("file")
        if start_line is None or file_path is None:
            continue

        source_file = repo_path / file_path
        if not source_file.exists():
            continue

        lines = source_file.read_text().splitlines()

        # If start_line is 0-based, lines[start_line] should contain the symbol
        if start_line < len(lines):
            line_0based = lines[start_line]
            print(
                f"{symbol_name}: line[{start_line}] (0-based) = {line_0based.strip()!r}"
            )

        # If start_line is 1-based, lines[start_line - 1] should contain the symbol
        if start_line - 1 >= 0 and start_line - 1 < len(lines):
            line_1based = lines[start_line - 1]
            print(
                f"{symbol_name}: line[{start_line - 1}] (1-based interp) = "
                f"{line_1based.strip()!r}"
            )

        # At least one interpretation should have the function keyword or name
        found_0 = start_line < len(lines) and (
            symbol_name in lines[start_line]
            or "function" in lines[start_line]
            or "const" in lines[start_line]
        )
        found_1 = (
            start_line - 1 >= 0
            and start_line - 1 < len(lines)
            and (
                symbol_name in lines[start_line - 1]
                or "function" in lines[start_line - 1]
                or "const" in lines[start_line - 1]
            )
        )
        assert found_0 or found_1, (
            f"{symbol_name}: neither 0-based line[{start_line}] nor "
            f"1-based line[{start_line - 1}] contains the function definition. "
            f"Graph start_line={start_line}, file={file_path}"
        )
        if found_0 and not found_1:
            print(f"  => {symbol_name} start_line is 0-based")
        elif found_1 and not found_0:
            print(f"  => {symbol_name} start_line is 1-based")
        else:
            print(f"  => {symbol_name} ambiguous (both lines match)")
