# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit coverage for the scip-dotnet candidate decoder."""

from __future__ import annotations

from codeminer.scip_interface.scip_decode_csharp import SCIPCSharpGraphDecoder
from codeminer.scip_interface.scip_indexer_csharp import SCIPCSharpIndexer
from codeminer.types import EDGE_TYPE_CONTAIN, EDGE_TYPE_REFERENCE

CSHARP_INDEX = """
metadata {
  tool_info {
    name: "scip-dotnet"
    version: "0.2.14"
  }
}
documents {
  relative_path: "Program.cs"
  occurrences {
    range: 0
    range: 10
    range: 15
    symbol: "scip-dotnet nuget . . Smoke/"
  }
  occurrences {
    range: 2
    range: 13
    range: 20
    symbol: "scip-dotnet nuget . . Smoke/Invoice#"
    symbol_roles: 1
  }
  occurrences {
    range: 4
    range: 15
    range: 20
    symbol: "scip-dotnet nuget . . Smoke/Invoice#Total()."
    symbol_roles: 1
  }
  occurrences {
    range: 7
    range: 20
    range: 27
    symbol: "scip-dotnet nuget . . Smoke/Helpers#"
    symbol_roles: 1
  }
  occurrences {
    range: 9
    range: 25
    range: 34
    symbol: "scip-dotnet nuget . . Smoke/Helpers#Normalize()."
    symbol_roles: 1
  }
  occurrences {
    range: 9
    range: 54
    range: 59
    symbol: "scip-dotnet nuget . . Smoke/Invoice#Total()."
  }
  symbols {
    symbol: "scip-dotnet nuget . . Smoke/Invoice#"
    documentation: "```cs\\nclass Invoice\\n```"
  }
  symbols {
    symbol: "scip-dotnet nuget . . Smoke/Invoice#Total()."
    documentation: "```cs\\npublic int Invoice.Total()\\n```"
  }
  symbols {
    symbol: "scip-dotnet nuget . . Smoke/Helpers#"
    documentation: "```cs\\nclass Helpers\\n```"
  }
  symbols {
    symbol: "scip-dotnet nuget . . Smoke/Helpers#Normalize()."
    documentation: "```cs\\npublic static string Helpers.Normalize()\\n```"
  }
}
documents {
  relative_path: "obj/Debug/net8.0/Smoke.GlobalUsings.g.cs"
  occurrences {
    range: 1
    range: 21
    range: 27
    symbol: "scip-dotnet nuget . . System/"
  }
}
""".strip()


CSHARP_ENTRYPOINT_INDEX = """
metadata {
  tool_info {
    name: "scip-dotnet"
    version: "0.2.14"
  }
}
documents {
  relative_path: "Program.cs"
  occurrences {
    range: 0
    range: 6
    range: 12
    symbol: "scip-dotnet nuget . . System/"
  }
  occurrences {
    range: 2
    range: 10
    range: 15
    symbol: "scip-dotnet nuget . . Hello/"
  }
  occurrences {
    range: 4
    range: 10
    range: 17
    symbol: "scip-dotnet nuget . . Hello/Program#"
    symbol_roles: 1
  }
  occurrences {
    range: 6
    range: 20
    range: 24
    symbol: "scip-dotnet nuget . . Hello/Program#Main()."
    symbol_roles: 1
  }
  occurrences {
    range: 6
    range: 34
    range: 38
    symbol: "scip-dotnet nuget . . Hello/Program#Main().(args)"
    symbol_roles: 1
  }
  symbols {
    symbol: "scip-dotnet nuget . . Hello/Program#"
    documentation: "```cs\\nclass Program\\n```"
  }
  symbols {
    symbol: "scip-dotnet nuget . . Hello/Program#Main()."
    documentation: "```cs\\nprivate static void Program.Main(string[] args)\\n```"
  }
  symbols {
    symbol: "scip-dotnet nuget . . Hello/Program#Main().(args)"
    documentation: "```cs\\nstring[] args\\n```"
  }
}
""".strip()


def test_scip_csharp_decoder_builds_symbols_and_references(tmp_path):
    index = tmp_path / "index.decoded"
    index.write_text(CSHARP_INDEX, encoding="utf-8")

    graph = SCIPCSharpGraphDecoder(str(index), project_root=str(tmp_path)).decode()
    graph.build_range_indexes()

    namespace = graph.graph.vs[graph.name_to_vertex["Smoke"]]
    invoice = graph.graph.vs[graph.name_to_vertex["Smoke/Invoice"]]
    total = graph.graph.vs[graph.name_to_vertex["Smoke/Invoice#Total"]]
    helpers = graph.graph.vs[graph.name_to_vertex["Smoke/Helpers"]]
    normalize = graph.graph.vs[graph.name_to_vertex["Smoke/Helpers#Normalize"]]

    assert namespace["unified_name"] == "Program.cs:Smoke"
    assert invoice["unified_name"] == "Program.cs:Smoke.Invoice"
    assert total["unified_name"] == "Program.cs:Smoke.Invoice.Total()"
    assert helpers["unified_name"] == "Program.cs:Smoke.Helpers"
    assert normalize["unified_name"] == "Program.cs:Smoke.Helpers.Normalize()"
    assert "obj/Debug/net8.0/Smoke.GlobalUsings.g.cs" not in graph.name_to_vertex

    contain = graph.graph.es.find(
        _source=graph.name_to_vertex["Smoke"],
        _target=graph.name_to_vertex["Smoke/Invoice"],
    )
    assert contain["type"] == EDGE_TYPE_CONTAIN

    reference = graph.graph.es.find(
        _source=graph.name_to_vertex["Smoke/Helpers#Normalize"],
        _target=graph.name_to_vertex["Smoke/Invoice#Total"],
    )
    assert reference["type"] == EDGE_TYPE_REFERENCE
    assert reference["anchor_file"] == "Program.cs"
    assert reference["anchor_line"] == 9


def test_scip_csharp_decoder_aligns_entrypoint_signature(tmp_path):
    (tmp_path / "Program.cs").write_text(
        """
using System;

namespace Hello
{
    class Program
    {
        static void Main(string[] args)
        {
            Console.WriteLine("Hello World!");
        }
    }
}
""".lstrip(),
        encoding="utf-8",
    )
    index = tmp_path / "index.decoded"
    index.write_text(CSHARP_ENTRYPOINT_INDEX, encoding="utf-8")

    graph = SCIPCSharpGraphDecoder(str(index), project_root=str(tmp_path)).decode()

    program = graph.graph.vs[graph.name_to_vertex["Hello/Program"]]
    main = graph.graph.vs[graph.name_to_vertex["Hello/Program#Main"]]

    assert "System" not in graph.name_to_vertex
    assert "Hello/Program#Main().(args)" not in graph.name_to_vertex
    assert program["unified_name"] == "Program.cs:Hello.Program"
    assert main["unified_name"] == "Program.cs:Hello.Program.Main(string[] args)()"

    contain = graph.graph.es.find(
        _source=graph.name_to_vertex["Hello/Program"],
        _target=graph.name_to_vertex["Hello/Program#Main"],
    )
    assert contain["type"] == EDGE_TYPE_CONTAIN


def test_scip_csharp_indexer_builds_registered_command(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEMINER_CSHARP_SCIP_CMD", "custom-scip-dotnet")
    (tmp_path / "Smoke.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk" />\n',
        encoding="utf-8",
    )

    indexer = SCIPCSharpIndexer(tmp_path, output_dir=tmp_path / "out")

    assert indexer._build_index_command() == [
        "custom-scip-dotnet",
        "index",
        str(tmp_path / "Smoke.csproj"),
        "--output",
        str(tmp_path / "out/index.scip"),
        "--working-directory",
        str(tmp_path),
    ]
    assert indexer._get_decoder_class() is SCIPCSharpGraphDecoder
