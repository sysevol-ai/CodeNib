# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit coverage for the scip-ruby candidate decoder."""

from __future__ import annotations

from codeminer.graph.code_graph import CodeGraph
from codeminer.scip_interface.scip_decode_ruby import SCIPRubyGraphDecoder
from codeminer.scip_interface.scip_indexer_ruby import SCIPRubyIndexer
from codeminer.types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_CLASS,
    NODE_TYPE_DIRECTORY,
    NODE_TYPE_FILE,
    NODE_TYPE_METHOD,
)

RUBY_INDEX = """
metadata {
  tool_info {
    name: "scip-ruby"
    version: "0.4.7"
  }
}
documents {
  relative_path: "lib/invoice.rb"
  occurrences {
    range: 0
    range: 7
    range: 12
    symbol: "scip-ruby gem smoke 0.1 Smoke#"
    symbol_roles: 1
  }
  occurrences {
    range: 1
    range: 8
    range: 15
    symbol: "scip-ruby gem smoke 0.1 Smoke#Invoice#"
    symbol_roles: 1
  }
  occurrences {
    range: 2
    range: 8
    range: 13
    symbol: "scip-ruby gem smoke 0.1 Smoke#Invoice#total()."
    symbol_roles: 1
  }
  occurrences {
    range: 7
    range: 11
    range: 20
    symbol: "scip-ruby gem smoke 0.1 `<Class:Smoke>`#normalize()."
    symbol_roles: 1
  }
  occurrences {
    range: 8
    range: 16
    range: 21
    symbol: "scip-ruby gem smoke 0.1 Smoke#Invoice#total()."
  }
  occurrences {
    range: 10
    range: 4
    range: 12
    symbol: "scip-ruby gem smoke 0.1 Rake#`<Class:Backtrace>`#collapse()."
    symbol_roles: 1
  }
  symbols {
    symbol: "scip-ruby gem smoke 0.1 Smoke#"
    documentation: "```ruby\\nmodule Smoke\\n```"
  }
  symbols {
    symbol: "scip-ruby gem smoke 0.1 Smoke#Invoice#"
    documentation: "```ruby\\nclass Smoke::Invoice\\n```"
  }
  symbols {
    symbol: "scip-ruby gem smoke 0.1 Smoke#Invoice#total()."
    documentation: "```ruby\\ndef total\\n```"
  }
  symbols {
    symbol: "scip-ruby gem smoke 0.1 `<Class:Smoke>`#normalize()."
    documentation: "```ruby\\ndef self.normalize\\n```"
  }
  symbols {
    symbol: "scip-ruby gem smoke 0.1 Rake#`<Class:Backtrace>`#collapse()."
    documentation: "```ruby\\ndef self.collapse\\n```"
  }
}
""".strip()


def test_scip_ruby_decoder_builds_symbols_and_references(tmp_path):
    index = tmp_path / "index.decoded"
    index.write_text(RUBY_INDEX, encoding="utf-8")

    graph = SCIPRubyGraphDecoder(str(index), project_root=str(tmp_path)).decode()
    graph.build_range_indexes()

    smoke = graph.graph.vs[graph.name_to_vertex["lib/invoice.rb:Smoke"]]
    invoice = graph.graph.vs[graph.name_to_vertex["Smoke#Invoice"]]
    total = graph.graph.vs[graph.name_to_vertex["Smoke#Invoice#total"]]
    normalize = graph.graph.vs[graph.name_to_vertex["Smoke#normalize"]]

    assert smoke["unified_name"] == "lib/invoice.rb:Smoke"
    assert invoice["unified_name"] == "lib/invoice.rb:Smoke.Invoice"
    assert total["unified_name"] == "lib/invoice.rb:Smoke.Invoice.total()"
    assert normalize["unified_name"] == "lib/invoice.rb:Smoke.normalize()"
    collapse = graph.graph.vs[graph.name_to_vertex["Rake#Backtrace#collapse"]]
    assert collapse["unified_name"] == "lib/invoice.rb:Rake.Backtrace.collapse()"

    contain = graph.graph.es.find(
        _source=graph.name_to_vertex["Smoke#Invoice"],
        _target=graph.name_to_vertex["Smoke#Invoice#total"],
    )
    assert contain["type"] == EDGE_TYPE_CONTAIN

    reference = graph.graph.es.find(
        _source=graph.name_to_vertex["Smoke#normalize"],
        _target=graph.name_to_vertex["Smoke#Invoice#total"],
    )
    assert reference["type"] == EDGE_TYPE_REFERENCE
    assert reference["anchor_file"] == "lib/invoice.rb"
    assert reference["anchor_line"] == 8


def test_scip_ruby_decoder_keeps_module_reopens_per_file(tmp_path):
    index = tmp_path / "index.decoded"
    index.write_text(
        """
metadata {
  tool_info {
    name: "scip-ruby"
    version: "0.4.7"
  }
}
documents {
  relative_path: "lib/a.rb"
  occurrences {
    range: 0
    range: 7
    range: 11
    symbol: "scip-ruby gem smoke 0.1 Rake#"
    symbol_roles: 1
  }
  occurrences {
    range: 1
    range: 8
    range: 19
    symbol: "scip-ruby gem smoke 0.1 Rake#Application#"
    symbol_roles: 1
  }
}
documents {
  relative_path: "lib/b.rb"
  occurrences {
    range: 0
    range: 7
    range: 11
    symbol: "scip-ruby gem smoke 0.1 Rake#"
    symbol_roles: 1
  }
  occurrences {
    range: 1
    range: 8
    range: 12
    symbol: "scip-ruby gem smoke 0.1 Rake#Task#"
    symbol_roles: 1
  }
}
""".strip(),
        encoding="utf-8",
    )

    graph = SCIPRubyGraphDecoder(str(index), project_root=str(tmp_path)).decode()

    assert graph.graph.vs[graph.name_to_vertex["lib/a.rb:Rake"]]["unified_name"] == (
        "lib/a.rb:Rake"
    )
    assert graph.graph.vs[graph.name_to_vertex["lib/b.rb:Rake"]]["unified_name"] == (
        "lib/b.rb:Rake"
    )

    contain_application = graph.graph.es.find(
        _source=graph.name_to_vertex["lib/a.rb:Rake"],
        _target=graph.name_to_vertex["Rake#Application"],
    )
    assert contain_application["type"] == EDGE_TYPE_CONTAIN

    contain_task = graph.graph.es.find(
        _source=graph.name_to_vertex["lib/b.rb:Rake"],
        _target=graph.name_to_vertex["Rake#Task"],
    )
    assert contain_task["type"] == EDGE_TYPE_CONTAIN


def test_scip_ruby_decoder_normalizes_attr_generated_writers_only(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib/application.rb").write_text(
        "attr_writer :tty_output\nself.tty_output = true\n",
        encoding="utf-8",
    )
    (tmp_path / "lib/task.rb").write_text(
        "def comment=(comment)\nend\n",
        encoding="utf-8",
    )
    index = tmp_path / "index.decoded"
    index.write_text(
        """
metadata {
  tool_info {
    name: "scip-ruby"
    version: "0.4.7"
  }
}
documents {
  relative_path: "lib/application.rb"
  occurrences {
    range: 0
    range: 12
    range: 22
    symbol: "scip-ruby gem rake 13.3 Rake#Application#`tty_output=`()."
    symbol_roles: 1
  }
  occurrences {
    range: 1
    range: 5
    range: 15
    symbol: "scip-ruby gem rake 13.3 Rake#Application#`tty_output=`()."
  }
}
documents {
  relative_path: "lib/task.rb"
  occurrences {
    range: 0
    range: 4
    range: 12
    symbol: "scip-ruby gem rake 13.3 Rake#Task#`comment=`()."
    symbol_roles: 1
  }
}
""".strip(),
        encoding="utf-8",
    )

    graph = SCIPRubyGraphDecoder(str(index), project_root=str(tmp_path)).decode()

    attr_writer = graph.graph.vs[graph.name_to_vertex["Rake#Application#tty_output="]]
    explicit_writer = graph.graph.vs[graph.name_to_vertex["Rake#Task#comment="]]
    assert (
        attr_writer["unified_name"]
        == "lib/application.rb:Rake.Application.tty_output()"
    )
    assert explicit_writer["unified_name"] == "lib/task.rb:Rake.Task.comment=()"


def test_scip_ruby_indexer_builds_registered_command(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEMINER_RUBY_SCIP_CMD", "bundle exec scip-ruby")

    indexer = SCIPRubyIndexer(tmp_path, output_dir=tmp_path / "out")

    assert indexer._build_index_command() == [
        "bundle",
        "exec",
        "scip-ruby",
        "--dir",
        ".",
        "--index-file",
        str(tmp_path / "out/index.scip"),
        "--gem-metadata",
        f"{tmp_path.name}@workspace",
        "--silence-dev-message",
        "--suppress-non-critical",
    ]
    assert indexer._get_decoder_class() is SCIPRubyGraphDecoder


def test_scip_ruby_indexer_unsets_gem_path_for_project_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("GEM_HOME", "/tmp/codeminer-tools/gems")
    monkeypatch.setenv("GEM_PATH", "/tmp/codeminer-tools/gems")
    (tmp_path / "Gemfile").write_text(
        'source "https://rubygems.org"\n', encoding="utf-8"
    )

    env = SCIPRubyIndexer(tmp_path, output_dir=tmp_path / "out")._ruby_env()

    assert env["GEM_HOME"] == "/tmp/codeminer-tools/gems"
    assert "GEM_PATH" not in env
    assert env["BUNDLE_GEMFILE"] == str(tmp_path / "Gemfile")


def test_scip_ruby_indexer_uses_overlay_bundle_gemfile(tmp_path, monkeypatch):
    overlay = tmp_path / ".codeminer/Gemfile"
    overlay.parent.mkdir()
    overlay.write_text('source "https://rubygems.org"\n', encoding="utf-8")
    monkeypatch.setenv("CODEMINER_RUBY_BUNDLE_GEMFILE", ".codeminer/Gemfile")

    env = SCIPRubyIndexer(tmp_path, output_dir=tmp_path / "out")._ruby_env()

    assert env["BUNDLE_GEMFILE"] == str(overlay)


def test_scip_ruby_indexer_filters_vendor_symbols_after_decode(tmp_path):
    graph = CodeGraph(project_root=str(tmp_path))
    graph._add_vertex(".", {"type": "root"})
    graph._add_vertex("lib", {"type": NODE_TYPE_DIRECTORY})
    graph._add_vertex("lib/invoice.rb", {"type": NODE_TYPE_FILE})
    graph._add_vertex("vendor", {"type": NODE_TYPE_DIRECTORY})
    graph._add_vertex(
        "Smoke",
        {
            "type": NODE_TYPE_CLASS,
            "file": "lib/invoice.rb",
            "start_line": 0,
            "end_line": 0,
            "unified_name": "lib/invoice.rb:Smoke",
        },
    )
    graph._add_vertex(
        "Kernel#to_s",
        {
            "type": NODE_TYPE_METHOD,
            "file": "lib/invoice.rb",
            "start_line": 1,
            "end_line": 1,
            "unified_name": "vendor/bundle/ruby/3.3.0/gems/ruby-lsp/lib/utils.rb:Kernel.to_s()",
        },
    )

    indexer = SCIPRubyIndexer(
        tmp_path,
        output_dir=tmp_path / "out",
        exclude_patterns=["vendor/**"],
    )
    indexer._target_dir = "lib"

    filtered = indexer._filter_project_graph(graph)

    assert "Smoke" in filtered.name_to_vertex
    assert "Kernel#to_s" not in filtered.name_to_vertex
    assert "vendor" not in filtered.name_to_vertex
