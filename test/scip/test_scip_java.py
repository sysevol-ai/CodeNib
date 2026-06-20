# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit coverage for the scip-java decoder."""

from __future__ import annotations

import sys

from codeminer.scip_interface import scip_indexer_java
from codeminer.scip_interface.scip_decode_java import SCIPJavaGraphDecoder
from codeminer.scip_interface.scip_indexer_java import (
    SCIPJavaIndexer,
    SCIPKotlinIndexer,
    SCIPScalaIndexer,
)
from codeminer.types import EDGE_TYPE_CONTAIN, EDGE_TYPE_REFERENCE

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
    monkeypatch.setenv("CODEMINER_KOTLIN_SCIP_CMD", "custom-scip-java index")

    indexer = SCIPKotlinIndexer(tmp_path, output_dir=tmp_path / "out")

    assert indexer.language == "kotlin"
    assert indexer._build_index_command() == [
        "custom-scip-java",
        "index",
        "--output",
        str(tmp_path / "out/index.scip"),
    ]
    assert indexer._get_decoder_class() is SCIPJavaGraphDecoder


def test_scip_scala_indexer_uses_scala_registry_command(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEMINER_SCALA_SCIP_CMD", "custom-scip-java index")

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
