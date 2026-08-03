# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit coverage for the scip-java decoder."""

from __future__ import annotations

import sys

from codenib.graph.code_graph import CodeGraph
from codenib.scip_interface import scip_indexer_java
from codenib.scip_interface.scip_decode_java import (
    SCIPJavaGraphDecoder,
    SCIPKotlinGraphDecoder,
)
from codenib.scip_interface.scip_indexer_java import (
    SCIPJavaIndexer,
    SCIPKotlinIndexer,
    SCIPScalaIndexer,
)
from codenib.types import EDGE_TYPE_CONTAIN, EDGE_TYPE_REFERENCE, NODE_TYPE_FIELD

JAVA_INDEX = """
metadata {
  tool_info {
    name: "scip-java"
    version: "0.12.3"
  }
}
documents {
  relative_path: "src/main/java/app/Foo.java"
  occurrences {
    range: 0
    range: 8
    range: 11
    symbol: "semanticdb maven . . app/"
  }
  occurrences {
    range: 0
    range: 26
    range: 29
    symbol: "semanticdb maven maven/app/scip-smoke 1.0 app/Foo#"
    symbol_roles: 1
    enclosing_range: 0
    enclosing_range: 13
    enclosing_range: 64
  }
  occurrences {
    range: 0
    range: 26
    range: 29
    symbol: "semanticdb maven maven/app/scip-smoke 1.0 app/Foo#`<init>`()."
    symbol_roles: 1
  }
  occurrences {
    range: 0
    range: 43
    range: 46
    symbol: "semanticdb maven maven/app/scip-smoke 1.0 app/Foo#run()."
    symbol_roles: 1
    enclosing_range: 0
    enclosing_range: 32
    enclosing_range: 62
  }
  symbols {
    symbol: "semanticdb maven maven/app/scip-smoke 1.0 app/Foo#"
    kind: Class
    display_name: "Foo"
    signature_documentation {
      relative_path: "src/main/java/app/Foo.java"
      language: "java"
      text: "public class Foo"
    }
  }
  symbols {
    symbol: "semanticdb maven maven/app/scip-smoke 1.0 app/Foo#run()."
    kind: Method
    display_name: "run"
    signature_documentation {
      relative_path: "src/main/java/app/Foo.java"
      language: "java"
      text: "public int run(String name)"
    }
  }
}
documents {
  relative_path: "src/main/java/app/Bar.java"
  occurrences {
    range: 0
    range: 26
    range: 29
    symbol: "semanticdb maven maven/app/scip-smoke 1.0 app/Bar#"
    symbol_roles: 1
    enclosing_range: 0
    enclosing_range: 13
    enclosing_range: 79
  }
  occurrences {
    range: 0
    range: 26
    range: 29
    symbol: "semanticdb maven maven/app/scip-smoke 1.0 app/Bar#`<init>`()."
    symbol_roles: 1
  }
  occurrences {
    range: 0
    range: 43
    range: 47
    symbol: "semanticdb maven maven/app/scip-smoke 1.0 app/Bar#call()."
    symbol_roles: 1
    enclosing_range: 0
    enclosing_range: 32
    enclosing_range: 77
  }
  occurrences {
    range: 0
    range: 69
    range: 72
    symbol: "semanticdb maven maven/app/scip-smoke 1.0 app/Foo#run()."
  }
  symbols {
    symbol: "semanticdb maven maven/app/scip-smoke 1.0 app/Bar#"
    kind: Class
    display_name: "Bar"
    signature_documentation {
      relative_path: "src/main/java/app/Bar.java"
      language: "java"
      text: "public class Bar"
    }
  }
  symbols {
    symbol: "semanticdb maven maven/app/scip-smoke 1.0 app/Bar#call()."
    kind: Method
    display_name: "call"
    signature_documentation {
      relative_path: "src/main/java/app/Bar.java"
      language: "java"
      text: "public int call()"
    }
  }
}
""".strip()


def test_scip_java_decoder_builds_symbols_and_references(tmp_path):
    index = tmp_path / "index.decoded"
    index.write_text(JAVA_INDEX, encoding="utf-8")

    graph = SCIPJavaGraphDecoder(str(index), project_root=str(tmp_path)).decode()
    graph.build_range_indexes()

    foo = graph.graph.vs[graph.name_to_vertex["app/Foo"]]
    foo_run = graph.graph.vs[graph.name_to_vertex["app/Foo#run"]]
    bar_call = graph.graph.vs[graph.name_to_vertex["app/Bar#call"]]

    assert foo["unified_name"] == "src/main/java/app/Foo.java:Foo"
    assert foo_run["unified_name"] == "src/main/java/app/Foo.java:Foo.run(String)()"
    assert bar_call["unified_name"] == "src/main/java/app/Bar.java:Bar.call()"
    assert "app/Foo#<init>" not in graph.name_to_vertex

    contain = graph.graph.es.find(
        _source=graph.name_to_vertex["app/Foo"],
        _target=graph.name_to_vertex["app/Foo#run"],
    )
    assert contain["type"] == EDGE_TYPE_CONTAIN

    reference = graph.graph.es.find(
        _source=graph.name_to_vertex["app/Bar#call"],
        _target=graph.name_to_vertex["app/Foo#run"],
    )
    assert reference["type"] == EDGE_TYPE_REFERENCE
    assert reference["anchor_file"] == "src/main/java/app/Bar.java"
    assert reference["anchor_line"] == 0

    query = graph.query_range(
        "src/main/java/app/Bar.java",
        0,
        0,
        kinds={EDGE_TYPE_REFERENCE},
    )
    assert [edge.target_vid for edge in query.outgoing] == [
        graph.name_to_vertex["app/Foo#run"]
    ]


def test_scip_java_indexer_uses_registered_command_and_output_path(
    tmp_path,
    monkeypatch,
):
    writer = tmp_path / "write_index.py"
    writer.write_text(
        """
import sys
from pathlib import Path

out = Path(sys.argv[sys.argv.index("--output") + 1])
out.write_bytes(b"scip")
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        scip_indexer_java,
        "scip_cold_start_command_for_language",
        lambda language: [sys.executable, str(writer)],
    )

    indexer = SCIPJavaIndexer(tmp_path, output_dir=tmp_path / "out")

    assert indexer._build_index_command() == [
        sys.executable,
        str(writer),
        "--output",
        str(tmp_path / "out/index.scip"),
    ]
    assert indexer.generate_index()
    assert (tmp_path / "out/index.scip").read_bytes() == b"scip"


def test_scip_java_indexer_exposes_java_decoder(tmp_path):
    indexer = SCIPJavaIndexer(tmp_path, output_dir=tmp_path / "out")

    assert indexer._get_decoder_class() is SCIPJavaGraphDecoder


def test_scip_java_indexer_combines_stdout_without_stderr():
    assert scip_indexer_java._combine_command_output("only stdout", None) == (
        "only stdout"
    )


def test_scip_kotlin_indexer_uses_kotlin_registry_command(tmp_path, monkeypatch):
    monkeypatch.setenv("CODENIB_KOTLIN_SCIP_CMD", "custom-scip-java index")

    indexer = SCIPKotlinIndexer(tmp_path, output_dir=tmp_path / "out")

    assert indexer.language == "kotlin"
    assert indexer._build_index_command() == [
        "custom-scip-java",
        "index",
        "--output",
        str(tmp_path / "out/index.scip"),
    ]
    assert indexer._get_decoder_class() is SCIPKotlinGraphDecoder


def test_scip_kotlin_indexer_filters_target_dir_after_decode(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "index.decoded").write_text(
        """
documents {
  relative_path: "src/main/kotlin/app/Invoice.kt"
  occurrences {
    range: 0
    range: 6
    range: 13
    symbol: "semanticdb maven . . app/Invoice#"
    symbol_roles: 1
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#"
    kind: Class
    display_name: "Invoice"
    signature_documentation {
      relative_path: "src/main/kotlin/app/Invoice.kt"
      language: "kotlin"
      text: "class Invoice"
    }
  }
}
documents {
  relative_path: "src/test/kotlin/app/InvoiceTest.kt"
  occurrences {
    range: 0
    range: 6
    range: 17
    symbol: "semanticdb maven . . app/InvoiceTest#"
    symbol_roles: 1
  }
  symbols {
    symbol: "semanticdb maven . . app/InvoiceTest#"
    kind: Class
    display_name: "InvoiceTest"
    signature_documentation {
      relative_path: "src/test/kotlin/app/InvoiceTest.kt"
      language: "kotlin"
      text: "class InvoiceTest"
    }
  }
}
""".strip(),
        encoding="utf-8",
    )

    graph = SCIPKotlinIndexer(tmp_path, output_dir=output_dir).run_pipeline(
        skip_level="decode",
        target_dir="src/main/kotlin",
        reset_profiler=False,
        report_profile=False,
    )

    assert graph is not None
    assert "app/Invoice" in graph.name_to_vertex
    assert "src/main/kotlin/app/Invoice.kt" in graph.name_to_vertex
    assert "app/InvoiceTest" not in graph.name_to_vertex
    assert "src/test/kotlin/app/InvoiceTest.kt" not in graph.name_to_vertex

    saved = CodeGraph.load_graph(str(output_dir / "graph.pkl"))
    assert "app/Invoice" in saved.name_to_vertex
    assert "app/InvoiceTest" not in saved.name_to_vertex


def test_scip_kotlin_indexer_filters_cached_graph_target_dir(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "index.decoded").write_text(
        """
documents {
  relative_path: "src/main/kotlin/app/Invoice.kt"
  occurrences {
    range: 0
    range: 6
    range: 13
    symbol: "semanticdb maven . . app/Invoice#"
    symbol_roles: 1
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#"
    kind: Class
    display_name: "Invoice"
    signature_documentation {
      relative_path: "src/main/kotlin/app/Invoice.kt"
      language: "kotlin"
      text: "class Invoice"
    }
  }
}
documents {
  relative_path: "src/test/kotlin/app/InvoiceTest.kt"
  occurrences {
    range: 0
    range: 6
    range: 17
    symbol: "semanticdb maven . . app/InvoiceTest#"
    symbol_roles: 1
  }
  symbols {
    symbol: "semanticdb maven . . app/InvoiceTest#"
    kind: Class
    display_name: "InvoiceTest"
    signature_documentation {
      relative_path: "src/test/kotlin/app/InvoiceTest.kt"
      language: "kotlin"
      text: "class InvoiceTest"
    }
  }
}
""".strip(),
        encoding="utf-8",
    )
    indexer = SCIPKotlinIndexer(tmp_path, output_dir=output_dir)
    unfiltered = indexer.run_pipeline(
        skip_level="decode",
        reset_profiler=False,
        report_profile=False,
    )
    assert unfiltered is not None
    assert "app/InvoiceTest" in unfiltered.name_to_vertex

    filtered = indexer.run_pipeline(
        skip_level="graph",
        target_dir="src/main/kotlin",
        reset_profiler=False,
        report_profile=False,
    )

    assert filtered is not None
    assert "app/Invoice" in filtered.name_to_vertex
    assert "app/InvoiceTest" not in filtered.name_to_vertex
    saved = CodeGraph.load_graph(str(output_dir / "graph.pkl"))
    assert "app/InvoiceTest" not in saved.name_to_vertex


def test_scip_scala_indexer_uses_scala_registry_command(tmp_path, monkeypatch):
    monkeypatch.setenv("CODENIB_SCALA_SCIP_CMD", "custom-scip-java index")

    indexer = SCIPScalaIndexer(tmp_path, output_dir=tmp_path / "out")

    assert indexer.language == "scala"
    assert indexer._build_index_command() == [
        "custom-scip-java",
        "index",
        "--output",
        str(tmp_path / "out/index.scip"),
    ]
    assert indexer._get_decoder_class() is SCIPJavaGraphDecoder


def test_scip_java_decoder_handles_kotlin_semanticdb_without_ranges(tmp_path):
    index = tmp_path / "index.decoded"
    index.write_text(
        """
documents {
  relative_path: "src/main/kotlin/app/Invoice.kt"
  occurrences {
    range: 2
    range: 6
    range: 13
    symbol: "semanticdb maven . . app/Invoice#"
    symbol_roles: 1
  }
  occurrences {
    range: 3
    range: 8
    range: 13
    symbol: "semanticdb maven . . app/Invoice#total()."
    symbol_roles: 1
  }
  occurrences {
    range: 6
    range: 4
    range: 13
    symbol: "semanticdb maven . . app/normalize()."
    symbol_roles: 1
  }
  occurrences {
    range: 6
    range: 36
    range: 41
    symbol: "semanticdb maven . . app/Invoice#total()."
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#"
    documentation: "```kotlin\\npublic final class Invoice : Any\\n```"
    display_name: "Invoice"
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#total()."
    documentation: "```kotlin\\npublic final fun total(): Int\\n```"
    display_name: "total"
  }
  symbols {
    symbol: "semanticdb maven . . app/normalize()."
    documentation: "```kotlin\\npublic final fun normalize(): String\\n```"
    display_name: "normalize"
  }
}
""".strip(),
        encoding="utf-8",
    )

    graph = SCIPJavaGraphDecoder(str(index), project_root=str(tmp_path)).decode()

    invoice = graph.graph.vs[graph.name_to_vertex["app/Invoice"]]
    total = graph.graph.vs[graph.name_to_vertex["app/Invoice#total"]]
    normalize = graph.graph.vs[graph.name_to_vertex["app/normalize"]]

    assert invoice["unified_name"] == "src/main/kotlin/app/Invoice.kt:Invoice"
    assert total["unified_name"] == "src/main/kotlin/app/Invoice.kt:Invoice.total()"
    assert normalize["unified_name"] == "src/main/kotlin/app/Invoice.kt:normalize()"

    contain = graph.graph.es.find(
        _source=graph.name_to_vertex["app/Invoice"],
        _target=graph.name_to_vertex["app/Invoice#total"],
    )
    assert contain["type"] == EDGE_TYPE_CONTAIN


def test_scip_kotlin_decoder_normalizes_companion_constructor_and_locals(
    tmp_path,
):
    source_file = tmp_path / "src/main/kotlin/app/Invoice.kt"
    source_file.parent.mkdir(parents=True)
    source_file.write_text(
        """
class Invoice {
  val code: String get() = "paid"

  enum class Kind {
    STANDARD,
    SPECIAL,
  }
}

fun normalize(): String = "ok"

val String.invoiceId: Boolean
  get() = startsWith("INV")

object Dynamic {
  override fun copy(): String = "copy"
  override fun emit(): String = "emit"
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    index = tmp_path / "index.decoded"
    index.write_text(
        """
documents {
  relative_path: "src/main/kotlin/app/Invoice.kt"
  occurrences {
    range: 0
    range: 6
    range: 13
    symbol: "semanticdb maven . . app/Invoice#"
    symbol_roles: 1
    enclosing_range: 0
    enclosing_range: 0
    enclosing_range: 8
    enclosing_range: 1
  }
  occurrences {
    range: 1
    range: 14
    range: 21
    symbol: "semanticdb maven . . app/Invoice#`<init>`()."
    symbol_roles: 1
  }
  occurrences {
    range: 2
    range: 12
    range: 21
    symbol: "semanticdb maven . . app/Invoice#Companion#"
    symbol_roles: 1
  }
  occurrences {
    range: 1
    range: 13
    range: 17
    symbol: "semanticdb maven . . app/Invoice#Kind#"
    symbol_roles: 1
  }
  occurrences {
    range: 2
    range: 4
    range: 12
    symbol: "semanticdb maven . . app/Invoice#Kind#STANDARD."
  }
  occurrences {
    range: 2
    range: 4
    range: 12
    symbol: "semanticdb maven . . app/Invoice#Kind#values()."
    symbol_roles: 1
  }
  occurrences {
    range: 2
    range: 4
    range: 12
    symbol: "semanticdb maven . . app/Invoice#Kind#valueOf()."
    symbol_roles: 1
  }
  occurrences {
    range: 2
    range: 4
    range: 12
    symbol: "semanticdb maven . . app/Invoice#Kind#entries."
    symbol_roles: 1
  }
  occurrences {
    range: 3
    range: 8
    range: 15
    symbol: "semanticdb maven . . app/Invoice#Companion#builder()."
    symbol_roles: 1
  }
  occurrences {
    range: 4
    range: 8
    range: 13
    symbol: "semanticdb maven . . app/Invoice#Companion#EMPTY."
    symbol_roles: 1
  }
  occurrences {
    range: 4
    range: 8
    range: 16
    symbol: "semanticdb maven . . app/Invoice#Companion#getEMPTY()."
    symbol_roles: 1
  }
  occurrences {
    range: 5
    range: 8
    range: 11
    symbol: "semanticdb maven . . app/Invoice#Companion#get(+1)."
    symbol_roles: 1
  }
  occurrences {
    range: 3
    range: 16
    range: 20
    symbol: "semanticdb maven . . app/Invoice#Companion#builder().(name)"
    symbol_roles: 1
  }
  occurrences {
    range: 5
    range: 16
    range: 23
    symbol: "semanticdb maven . . app/Invoice#Companion#builder(+1).(altName)"
    symbol_roles: 1
  }
  occurrences {
    range: 0
    range: 14
    range: 15
    symbol: "semanticdb maven . . app/Invoice#[T]"
    symbol_roles: 1
  }
  occurrences {
    range: 12
    range: 7
    range: 14
    symbol: "semanticdb maven . . app/Dynamic#"
    symbol_roles: 1
  }
  occurrences {
    range: 13
    range: 15
    range: 19
    symbol: "semanticdb maven . . app/Dynamic#copy()."
    symbol_roles: 1
  }
  occurrences {
    range: 14
    range: 15
    range: 19
    symbol: "semanticdb maven . . app/Dynamic#emit()."
    symbol_roles: 1
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#"
    kind: Class
    display_name: "Invoice"
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#`<init>`()."
    kind: Method
    display_name: "<init>"
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#Companion#"
    kind: Object
    display_name: "Companion"
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#Kind#"
    kind: Enum
    display_name: "Kind"
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#Kind#values()."
    documentation: "static fun values(): Array<Invoice.Kind>"
    display_name: "values"
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#Kind#valueOf()."
    documentation: "static fun valueOf(value: String): Invoice.Kind"
    display_name: "valueOf"
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#Kind#entries."
    documentation: "static val entries: EnumEntries<Invoice.Kind>"
    display_name: "entries"
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#Companion#builder()."
    kind: Method
    display_name: "builder"
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#Companion#EMPTY."
    display_name: "EMPTY"
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#Companion#getEMPTY()."
    kind: Method
    display_name: "getEMPTY"
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#Companion#get(+1)."
    display_name: "get"
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#Companion#builder().(name)"
    display_name: "name"
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#Companion#builder(+1).(altName)"
    display_name: "altName"
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#[T]"
    display_name: "FirTypeParameterSymbol T"
  }
  symbols {
    symbol: "semanticdb maven . . app/Dynamic#"
    kind: Object
    display_name: "Dynamic"
  }
  symbols {
    symbol: "semanticdb maven . . app/Dynamic#copy()."
    kind: Method
    display_name: "copy"
  }
  symbols {
    symbol: "semanticdb maven . . app/Dynamic#emit()."
    kind: Method
    display_name: "emit"
  }
}
""".strip(),
        encoding="utf-8",
    )

    graph = SCIPKotlinGraphDecoder(str(index), project_root=str(tmp_path)).decode()

    constructor = graph.graph.vs[graph.name_to_vertex["app/Invoice#<init>"]]
    kind = graph.graph.vs[graph.name_to_vertex["app/Invoice#Kind"]]
    builder = graph.graph.vs[graph.name_to_vertex["app/Invoice#Companion#builder"]]
    empty = graph.graph.vs[graph.name_to_vertex["app/Invoice#Companion#EMPTY"]]
    overload = graph.graph.vs[graph.name_to_vertex["app/Invoice#Companion#get(+1)"]]
    assert constructor["unified_name"] == (
        "src/main/kotlin/app/Invoice.kt:Invoice.constructor()"
    )
    assert kind["unified_name"] == "src/main/kotlin/app/Invoice.kt:Invoice.Kind"
    assert builder["unified_name"] == "src/main/kotlin/app/Invoice.kt:Invoice.builder()"
    assert empty["unified_name"] == "src/main/kotlin/app/Invoice.kt:Invoice.EMPTY"
    assert overload["unified_name"] == "src/main/kotlin/app/Invoice.kt:Invoice.get()"
    standard_defs = [
        vertex
        for vertex in graph.graph.vs
        if vertex.attributes().get("unified_name")
        == "src/main/kotlin/app/Invoice.kt:Invoice.Kind.STANDARD"
        and vertex.attributes().get("start_line") is not None
    ]
    assert len(standard_defs) == 1
    standard_name = standard_defs[0]["name"]
    assert standard_name != "app/Invoice#Kind#STANDARD"
    assert (
        graph.graph.vs[graph.name_to_vertex["app/Invoice#Kind#SPECIAL"]]["unified_name"]
        == "src/main/kotlin/app/Invoice.kt:Invoice.Kind.SPECIAL"
    )
    normalize_defs = [
        vertex
        for vertex in graph.graph.vs
        if vertex.attributes().get("unified_name")
        == "src/main/kotlin/app/Invoice.kt:normalize()"
        and vertex.attributes().get("start_line") is not None
    ]
    assert len(normalize_defs) == 1
    assert "app/Invoice#Companion" not in graph.name_to_vertex
    assert "app/Invoice#Companion#getEMPTY" not in graph.name_to_vertex
    assert "app/Invoice#Companion#builder().(name)" not in graph.name_to_vertex
    assert "app/Invoice#Companion#builder(+1).(altName)" not in graph.name_to_vertex
    assert "app/Invoice#[T]" not in graph.name_to_vertex
    assert "app/Invoice#Kind#values" not in graph.name_to_vertex
    assert "app/Invoice#Kind#valueOf" not in graph.name_to_vertex
    assert "app/Invoice#Kind#entries" not in graph.name_to_vertex
    assert (
        graph.graph.vs[graph.name_to_vertex["app/Dynamic#copy"]]["unified_name"]
        == "src/main/kotlin/app/Invoice.kt:Dynamic.copy()"
    )
    assert (
        graph.graph.vs[graph.name_to_vertex["app/Dynamic#emit"]]["unified_name"]
        == "src/main/kotlin/app/Invoice.kt:Dynamic.emit()"
    )

    contain = graph.graph.es.find(
        _source=graph.name_to_vertex["app/Invoice"],
        _target=graph.name_to_vertex["app/Invoice#Companion#builder"],
    )
    assert contain["type"] == EDGE_TYPE_CONTAIN

    enum_contain = graph.graph.es.find(
        _source=graph.name_to_vertex["app/Invoice#Kind"],
        _target=graph.name_to_vertex[standard_name],
    )
    assert enum_contain["type"] == EDGE_TYPE_CONTAIN

    function_contain = graph.graph.es.find(
        _source=graph.name_to_vertex["src/main/kotlin/app/Invoice.kt"],
        _target=normalize_defs[0].index,
    )
    assert function_contain["type"] == EDGE_TYPE_CONTAIN

    property_defs = [
        vertex
        for vertex in graph.graph.vs
        if vertex.attributes().get("unified_name")
        == "src/main/kotlin/app/Invoice.kt:Invoice.code"
        and vertex.attributes().get("start_line") is not None
    ]
    assert len(property_defs) == 1
    property_contain = graph.graph.es.find(
        _source=graph.name_to_vertex["app/Invoice"],
        _target=property_defs[0].index,
    )
    assert property_contain["type"] == EDGE_TYPE_CONTAIN
    getter_defs = [
        vertex
        for vertex in graph.graph.vs
        if vertex.attributes().get("unified_name")
        == "src/main/kotlin/app/Invoice.kt:Invoice.code.get()"
        and vertex.attributes().get("start_line") is not None
    ]
    assert len(getter_defs) == 1
    getter_contain = graph.graph.es.find(
        _source=property_defs[0].index,
        _target=getter_defs[0].index,
    )
    assert getter_contain["type"] == EDGE_TYPE_CONTAIN

    extension_property_defs = [
        vertex
        for vertex in graph.graph.vs
        if vertex.attributes().get("unified_name")
        == "src/main/kotlin/app/Invoice.kt:invoiceId"
        and vertex.attributes().get("start_line") is not None
    ]
    assert len(extension_property_defs) == 1
    extension_property_contain = graph.graph.es.find(
        _source=graph.name_to_vertex["src/main/kotlin/app/Invoice.kt"],
        _target=extension_property_defs[0].index,
    )
    assert extension_property_contain["type"] == EDGE_TYPE_CONTAIN
    extension_getter_defs = [
        vertex
        for vertex in graph.graph.vs
        if vertex.attributes().get("unified_name")
        == "src/main/kotlin/app/Invoice.kt:invoiceId.get()"
        and vertex.attributes().get("start_line") is not None
    ]
    assert len(extension_getter_defs) == 1
    extension_getter_contain = graph.graph.es.find(
        _source=extension_property_defs[0].index,
        _target=extension_getter_defs[0].index,
    )
    assert extension_getter_contain["type"] == EDGE_TYPE_CONTAIN

    copy_alias_defs = [
        vertex
        for vertex in graph.graph.vs
        if vertex.attributes().get("unified_name")
        == "src/main/kotlin/app/Invoice.kt:copy()"
        and vertex.attributes().get("start_line") is not None
    ]
    assert len(copy_alias_defs) == 1
    copy_alias_contain = graph.graph.es.find(
        _source=graph.name_to_vertex["src/main/kotlin/app/Invoice.kt"],
        _target=copy_alias_defs[0].index,
    )
    assert copy_alias_contain["type"] == EDGE_TYPE_CONTAIN
    emit_alias_defs = [
        vertex
        for vertex in graph.graph.vs
        if vertex.attributes().get("unified_name")
        == "src/main/kotlin/app/Invoice.kt:emit()"
        and vertex.attributes().get("start_line") is not None
    ]
    assert len(emit_alias_defs) == 1
    emit_alias_contain = graph.graph.es.find(
        _source=graph.name_to_vertex["src/main/kotlin/app/Invoice.kt"],
        _target=emit_alias_defs[0].index,
    )
    assert emit_alias_contain["type"] == EDGE_TYPE_CONTAIN


def test_scip_kotlin_decoder_contains_companion_members_without_ranges(tmp_path):
    index = tmp_path / "index.decoded"
    index.write_text(
        """
documents {
  relative_path: "src/main/kotlin/app/Factory.kt"
  occurrences {
    range: 0
    range: 6
    range: 13
    symbol: "semanticdb maven . . app/Factory#"
    symbol_roles: 1
  }
  occurrences {
    range: 1
    range: 13
    range: 22
    symbol: "semanticdb maven . . app/Factory#Companion#"
    symbol_roles: 1
  }
  occurrences {
    range: 2
    range: 16
    range: 22
    symbol: "semanticdb maven . . app/Factory#Companion#create()."
    symbol_roles: 1
  }
  symbols {
    symbol: "semanticdb maven . . app/Factory#"
    kind: Class
    display_name: "Factory"
  }
  symbols {
    symbol: "semanticdb maven . . app/Factory#Companion#"
    kind: Object
    display_name: "Companion"
  }
  symbols {
    symbol: "semanticdb maven . . app/Factory#Companion#create()."
    kind: Method
    display_name: "create"
  }
}
""".strip(),
        encoding="utf-8",
    )

    graph = SCIPKotlinGraphDecoder(str(index), project_root=str(tmp_path)).decode()

    assert "app/Factory#Companion" not in graph.name_to_vertex
    assert (
        graph.graph.vs[graph.name_to_vertex["app/Factory#Companion#create"]][
            "unified_name"
        ]
        == "src/main/kotlin/app/Factory.kt:Factory.create()"
    )
    contain = graph.graph.es.find(
        _source=graph.name_to_vertex["app/Factory"],
        _target=graph.name_to_vertex["app/Factory#Companion#create"],
    )
    assert contain["type"] == EDGE_TYPE_CONTAIN


def test_scip_kotlin_decoder_filters_metadata_property_accessors(tmp_path):
    index = tmp_path / "index.decoded"
    index.write_text(
        """
documents {
  relative_path: "src/main/kotlin/app/Config.kt"
  occurrences {
    range: 0
    range: 12
    range: 17
    symbol: "semanticdb maven . . app/VALUE."
    symbol_roles: 1
  }
  occurrences {
    range: 0
    range: 12
    range: 17
    symbol: "semanticdb maven . . app/getVALUE()."
    symbol_roles: 1
  }
  occurrences {
    range: 2
    range: 6
    range: 9
    symbol: "semanticdb maven . . app/Box#"
    symbol_roles: 1
  }
  occurrences {
    range: 3
    range: 14
    range: 18
    symbol: "semanticdb maven . . app/Box#name."
    symbol_roles: 1
  }
  occurrences {
    range: 3
    range: 14
    range: 18
    symbol: "semanticdb maven . . app/Box#setName()."
    symbol_roles: 1
  }
  symbols {
    symbol: "semanticdb maven . . app/VALUE."
    documentation: "```kotlin\\nfield:public final val VALUE: String\\n```"
    display_name: "VALUE"
  }
  symbols {
    symbol: "semanticdb maven . . app/getVALUE()."
    documentation: "```kotlin\\npublic get(): String\\n```"
    display_name: "VALUE"
  }
  symbols {
    symbol: "semanticdb maven . . app/Box#"
    kind: Class
    display_name: "Box"
  }
  symbols {
    symbol: "semanticdb maven . . app/Box#name."
    documentation: "```kotlin\\nprivate final var name: String\\n```"
    display_name: "name"
  }
  symbols {
    symbol: "semanticdb maven . . app/Box#setName()."
    documentation: "```kotlin\\nprivate set(value: String): Unit\\n```"
    display_name: "name"
  }
}
""".strip(),
        encoding="utf-8",
    )

    graph = SCIPKotlinGraphDecoder(str(index), project_root=str(tmp_path)).decode()

    assert "app/getVALUE" not in graph.name_to_vertex
    assert "app/Box#setName" not in graph.name_to_vertex

    value = graph.graph.vs[graph.name_to_vertex["app/VALUE"]]
    assert value["type"] == NODE_TYPE_FIELD
    assert value["unified_name"] == "src/main/kotlin/app/Config.kt:VALUE"

    name = graph.graph.vs[graph.name_to_vertex["app/Box#name"]]
    assert name["type"] == NODE_TYPE_FIELD
    assert name["unified_name"] == "src/main/kotlin/app/Config.kt:Box.name"


def test_scip_java_decoder_normalizes_nested_kotlin_owner_and_overload_suffix(
    tmp_path,
):
    index = tmp_path / "index.decoded"
    index.write_text(
        """
documents {
  relative_path: "src/main/kotlin/app/Outer.kt"
  occurrences {
    range: 0
    range: 6
    range: 11
    symbol: "semanticdb maven . . app/Outer#"
    symbol_roles: 1
  }
  occurrences {
    range: 1
    range: 8
    range: 13
    symbol: "semanticdb maven . . app/Outer#Inner#"
    symbol_roles: 1
  }
  occurrences {
    range: 2
    range: 10
    range: 13
    symbol: "semanticdb maven . . app/Outer#Inner#run(+1)."
    symbol_roles: 1
  }
  symbols {
    symbol: "semanticdb maven . . app/Outer#"
    kind: Class
    display_name: "Outer"
  }
  symbols {
    symbol: "semanticdb maven . . app/Outer#Inner#"
    kind: Class
    display_name: "Inner"
  }
  symbols {
    symbol: "semanticdb maven . . app/Outer#Inner#run(+1)."
    kind: Method
    display_name: "run"
  }
}
""".strip(),
        encoding="utf-8",
    )

    graph = SCIPJavaGraphDecoder(str(index), project_root=str(tmp_path)).decode()

    outer = graph.graph.vs[graph.name_to_vertex["app/Outer"]]
    inner = graph.graph.vs[graph.name_to_vertex["app/Outer#Inner"]]
    run = graph.graph.vs[graph.name_to_vertex["app/Outer#Inner#run(+1)"]]

    assert outer["unified_name"] == "src/main/kotlin/app/Outer.kt:Outer"
    assert inner["unified_name"] == "src/main/kotlin/app/Outer.kt:Outer.Inner"
    assert run["unified_name"] == "src/main/kotlin/app/Outer.kt:Outer.Inner.run()"

    contain = graph.graph.es.find(
        _source=graph.name_to_vertex["app/Outer#Inner"],
        _target=graph.name_to_vertex["app/Outer#Inner#run(+1)"],
    )
    assert contain["type"] == EDGE_TYPE_CONTAIN


def test_scip_java_decoder_handles_scala_object_members(tmp_path):
    index = tmp_path / "index.decoded"
    index.write_text(
        """
documents {
  relative_path: "src/main/scala/app/Invoice.scala"
  occurrences {
    range: 2
    range: 6
    range: 13
    symbol: "semanticdb maven . . app/Invoice#"
    symbol_roles: 1
  }
  occurrences {
    range: 3
    range: 6
    range: 11
    symbol: "semanticdb maven . . app/Invoice#total()."
    symbol_roles: 1
  }
  occurrences {
    range: 6
    range: 7
    range: 14
    symbol: "semanticdb maven . . app/Helpers."
    symbol_roles: 1
  }
  occurrences {
    range: 7
    range: 6
    range: 15
    symbol: "semanticdb maven . . app/Helpers.normalize()."
    symbol_roles: 1
  }
  occurrences {
    range: 7
    range: 42
    range: 47
    symbol: "semanticdb maven . . app/Invoice#total()."
  }
  symbols {
    symbol: "semanticdb maven . . app/Helpers.normalize()."
    kind: Method
    display_name: "normalize"
    signature_documentation {
      relative_path: "src/main/scala/app/Invoice.scala"
      language: "scala"
      text: "def normalize(): String"
    }
  }
  symbols {
    symbol: "semanticdb maven . . app/Helpers."
    kind: Object
    display_name: "Helpers"
    signature_documentation {
      relative_path: "src/main/scala/app/Invoice.scala"
      language: "scala"
      text: "object Helpers"
    }
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#"
    kind: Class
    display_name: "Invoice"
    signature_documentation {
      relative_path: "src/main/scala/app/Invoice.scala"
      language: "scala"
      text: "class Invoice"
    }
  }
  symbols {
    symbol: "semanticdb maven . . app/Invoice#total()."
    kind: Method
    display_name: "total"
    signature_documentation {
      relative_path: "src/main/scala/app/Invoice.scala"
      language: "scala"
      text: "def total(): Int"
    }
  }
}
""".strip(),
        encoding="utf-8",
    )

    graph = SCIPJavaGraphDecoder(str(index), project_root=str(tmp_path)).decode()

    invoice = graph.graph.vs[graph.name_to_vertex["app/Invoice"]]
    total = graph.graph.vs[graph.name_to_vertex["app/Invoice#total"]]
    helpers = graph.graph.vs[graph.name_to_vertex["app/Helpers"]]
    normalize = graph.graph.vs[graph.name_to_vertex["app/Helpers.normalize"]]

    assert invoice["unified_name"] == "src/main/scala/app/Invoice.scala:Invoice"
    assert total["unified_name"] == "src/main/scala/app/Invoice.scala:Invoice.total()"
    assert helpers["unified_name"] == "src/main/scala/app/Invoice.scala:Helpers"
    assert (
        normalize["unified_name"]
        == "src/main/scala/app/Invoice.scala:Helpers.normalize()"
    )

    contain = graph.graph.es.find(
        _source=graph.name_to_vertex["app/Helpers"],
        _target=graph.name_to_vertex["app/Helpers.normalize"],
    )
    assert contain["type"] == EDGE_TYPE_CONTAIN
