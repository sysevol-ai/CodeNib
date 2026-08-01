# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from codenib.graph.layers import MultiGraphIndex
from codenib.scip_interface.scip_decode_ts import SCIPTypeScriptGraphDecoder
from codenib.types import EDGE_TYPE_IMPORT, EDGE_TYPE_REFERENCE, GRAPH_LAYER_IMPORT

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "typescript_schema_v5"
INDEX_FILE = FIXTURE_ROOT / "index.decoded"
PREFIX = "schema-v5-fixture@1.0.0:"


def _decode(index_file: Path = INDEX_FILE):
    return SCIPTypeScriptGraphDecoder(
        str(index_file), project_root=str(FIXTURE_ROOT)
    ).decode()


def _attrs(graph, suffix: str):
    name = f"{PREFIX}{suffix}"
    return graph.graph.vs[graph.name_to_vertex[name]].attributes()


def _typed_edges(graph, edge_type: str):
    edges = set()
    for edge in graph.graph.es:
        attrs = edge.attributes()
        if attrs.get("type") != edge_type:
            continue
        edges.add(
            (
                graph.graph.vs[edge.source]["name"],
                graph.graph.vs[edge.target]["name"],
                attrs.get("anchor_file"),
                attrs.get("anchor_line"),
            )
        )
    return edges


def test_typescript_schema_v5_preserves_coarse_type_and_adds_semantic_kind():
    graph = _decode()

    expected = {
        "src/types.ts:Runner": "interface",
        "src/types.ts:RunnerId": "type_alias",
        "src/types.ts:Mode": "enum",
        "src/types.ts:DefaultRunner": "class",
    }
    for suffix, symbol_kind in expected.items():
        attrs = _attrs(graph, suffix)
        assert attrs["type"] == "class"
        assert attrs["symbol_kind"] == symbol_kind
        assert attrs["has_definition"] is True


def test_typescript_schema_v5_promotes_late_definitions_and_marks_missing_ones():
    graph = _decode()

    promoted = _attrs(graph, "src/types.ts:Runner")
    assert promoted["file"] == "src/types.ts"
    assert promoted["selection_line"] == 0
    assert promoted["has_definition"] is True

    missing = _attrs(graph, "src/missing.ts:MissingType")
    assert missing["has_definition"] is False
    assert missing.get("start_line") is None
    assert missing.get("selection_line") is None


def test_typescript_schema_v5_import_layer_is_anchored_and_keeps_references():
    graph = _decode()

    imports = _typed_edges(graph, EDGE_TYPE_IMPORT)
    assert imports == {
        ("src/use.ts", "src/types.ts", "src/use.ts", 0),
        ("src/reexport.ts", "src/types.ts", "src/reexport.ts", 0),
        ("src/view.tsx", "src/types.ts", "src/view.tsx", 0),
    }
    assert len(MultiGraphIndex(graph).edge_ids(GRAPH_LAYER_IMPORT)) == 3

    references = _typed_edges(graph, EDGE_TYPE_REFERENCE)
    assert (
        "src/use.ts",
        f"{PREFIX}src/types.ts:DefaultRunner",
        "src/use.ts",
        0,
    ) in references
    assert (
        "src/view.tsx",
        f"{PREFIX}src/types.ts:Runner",
        "src/view.tsx",
        0,
    ) in references
    assert (
        "src/reexport.ts",
        f"{PREFIX}src/types.ts:Runner",
        "src/reexport.ts",
        0,
    ) in references


def test_explicit_symbol_kind_precedes_legacy_signature_fallback(tmp_path):
    content = INDEX_FILE.read_text(encoding="utf-8")
    marker = (
        'symbol: "scip-typescript npm schema-v5-fixture 1.0.0 '
        'src/`types.ts`/Runner#"\n'
        '    documentation: "```ts\\ninterface Runner\\n```"'
    )
    assert marker in content
    explicit = content.replace(
        marker,
        marker.replace("\n    documentation", "\n    kind: Class\n    documentation"),
    )
    index_file = tmp_path / "index.decoded"
    index_file.write_text(explicit, encoding="utf-8")

    graph = _decode(index_file)

    assert _attrs(graph, "src/types.ts:Runner")["symbol_kind"] == "class"


def test_legacy_kind_fallback_only_accepts_first_documentation_entry(tmp_path):
    content = INDEX_FILE.read_text(encoding="utf-8")
    marker = (
        'symbol: "scip-typescript npm schema-v5-fixture 1.0.0 '
        'src/`types.ts`/Runner#"\n'
        '    documentation: "```ts\\ninterface Runner\\n```"'
    )
    assert marker in content
    modified = content.replace(
        marker,
        marker.replace(
            "\n    documentation",
            '\n    documentation: "User-authored prose"\n    documentation',
        ),
        1,
    )
    index_file = tmp_path / "index.decoded"
    index_file.write_text(modified, encoding="utf-8")

    graph = _decode(index_file)

    assert _attrs(graph, "src/types.ts:Runner").get("symbol_kind") is None


def test_explicit_import_role_is_honored_outside_static_module_span(tmp_path):
    content = INDEX_FILE.read_text(encoding="utf-8")
    marker = (
        "    range: 3\n"
        "    range: 28\n"
        "    range: 36\n"
        f'    symbol: "scip-typescript npm schema-v5-fixture 1.0.0 '
        'src/`types.ts`/RunnerId#"'
    )
    assert marker in content
    explicit = content.replace(marker, marker + "\n    symbol_roles: 2", 1)
    index_file = tmp_path / "index.decoded"
    index_file.write_text(explicit, encoding="utf-8")

    graph = _decode(index_file)

    assert (
        "src/use.ts",
        "src/types.ts",
        "src/use.ts",
        3,
    ) in _typed_edges(graph, EDGE_TYPE_IMPORT)
