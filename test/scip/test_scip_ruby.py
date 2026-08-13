# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit coverage for the scip-ruby decoder and Ruby hybrid route."""

from __future__ import annotations

from codenib.graph.code_graph import CodeGraph
from codenib.scip_interface.scip_decode_ruby import SCIPRubyGraphDecoder
from codenib.scip_interface.scip_indexer_ruby import RubyHybridIndexer, SCIPRubyIndexer
from codenib.types import (
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
documents {
  relative_path: "lib/c.rb"
  occurrences {
    range: 0
    range: 6
    range: 15
    symbol: "scip-ruby gem smoke 0.1 Rake#NameSpace#"
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

    contain_namespace = graph.graph.es.find(
        _source=graph.name_to_vertex["lib/c.rb"],
        _target=graph.name_to_vertex["Rake#NameSpace"],
    )
    assert contain_namespace["type"] == EDGE_TYPE_CONTAIN


def test_scip_ruby_decoder_normalizes_nested_and_local_singleton_methods(
    tmp_path,
):
    (tmp_path / "lib/rake").mkdir(parents=True)
    (tmp_path / "lib/rake/application.rb").write_text(
        "\n".join(
            [
                "class Application",
                "  def load_debug_at_stop_feature",
                "    Module.new do",
                "      def execute(*)",
                "      end",
                "    end",
                "  end",
                "end",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "lib/rake/phony.rb").write_text(
        "task = Object.new\n" "def task.timestamp\n" "end\n",
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
  relative_path: "lib/rake/application.rb"
  occurrences {
    range: 0
    range: 6
    range: 17
    symbol: "scip-ruby gem smoke 0.1 Rake#Application#"
    symbol_roles: 1
  }
  occurrences {
    range: 1
    range: 6
    range: 32
    symbol: "scip-ruby gem smoke 0.1 Rake#Application#load_debug_at_stop_feature()."
    symbol_roles: 1
    enclosing_range: 1
    enclosing_range: 2
    enclosing_range: 6
    enclosing_range: 0
  }
  occurrences {
    range: 3
    range: 10
    range: 17
    symbol: "scip-ruby gem smoke 0.1 Rake#Application#execute()."
    symbol_roles: 1
  }
}
documents {
  relative_path: "lib/rake/phony.rb"
  occurrences {
    range: 1
    range: 4
    range: 18
    symbol: "scip-ruby gem smoke 0.1 `<Class:Object>`#timestamp()."
    symbol_roles: 1
  }
}
""".strip(),
        encoding="utf-8",
    )

    graph = SCIPRubyGraphDecoder(str(index), project_root=str(tmp_path)).decode()

    execute = graph.graph.vs[graph.name_to_vertex["Rake#Application#execute"]]
    timestamp = graph.graph.vs[graph.name_to_vertex["Object#timestamp"]]
    assert execute["unified_name"] == (
        "lib/rake/application.rb:"
        "Rake.Application.load_debug_at_stop_feature().execute()"
    )
    assert timestamp["unified_name"] == "lib/rake/phony.rb:timestamp()"

    contain_edges = {
        (
            graph.graph.vs[edge.source].attributes().get("unified_name")
            or graph.graph.vs[edge.source]["name"],
            graph.graph.vs[edge.target].attributes().get("unified_name")
            or graph.graph.vs[edge.target]["name"],
        )
        for edge in graph.graph.es
        if edge["type"] == EDGE_TYPE_CONTAIN
    }
    assert (
        "lib/rake/application.rb:Rake.Application.load_debug_at_stop_feature()",
        "lib/rake/application.rb:"
        "Rake.Application.load_debug_at_stop_feature().execute()",
    ) in contain_edges
    assert (
        "lib/rake/phony.rb",
        "lib/rake/phony.rb:timestamp()",
    ) in contain_edges


def test_scip_ruby_decoder_synthesizes_source_alias_methods(tmp_path):
    (tmp_path / "lib/rake").mkdir(parents=True)
    (tmp_path / "lib/rake/file_list.rb").write_text(
        "\n".join(
            [
                "module Rake",
                "  class FileList",
                "    def include(*filenames)",
                "    end",
                "    alias :add :include",
                "",
                "    def is_a?(klass)",
                "    end",
                "    alias kind_of? is_a?",
                "  end",
                "end",
            ]
        ),
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
  relative_path: "lib/rake/file_list.rb"
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
    range: 16
    symbol: "scip-ruby gem smoke 0.1 Rake#FileList#"
    symbol_roles: 1
  }
  occurrences {
    range: 2
    range: 8
    range: 15
    symbol: "scip-ruby gem smoke 0.1 Rake#FileList#include()."
    symbol_roles: 1
  }
  occurrences {
    range: 6
    range: 8
    range: 13
    symbol: "scip-ruby gem smoke 0.1 Rake#FileList#is_a?()."
    symbol_roles: 1
  }
}
""".strip(),
        encoding="utf-8",
    )

    graph = SCIPRubyGraphDecoder(str(index), project_root=str(tmp_path)).decode()

    add = graph.graph.vs[graph.name_to_vertex["Rake#FileList#add"]]
    kind_of = graph.graph.vs[graph.name_to_vertex["Rake#FileList#kind_of?"]]
    assert add["unified_name"] == "lib/rake/file_list.rb:Rake.FileList.add()"
    assert add["start_line"] == 4
    assert kind_of["unified_name"] == ("lib/rake/file_list.rb:Rake.FileList.kind_of?()")
    assert kind_of["start_line"] == 8

    contain_edges = {
        (
            graph.graph.vs[edge.source].attributes().get("unified_name")
            or graph.graph.vs[edge.source]["name"],
            graph.graph.vs[edge.target].attributes().get("unified_name")
            or graph.graph.vs[edge.target]["name"],
        )
        for edge in graph.graph.es
        if edge["type"] == EDGE_TYPE_CONTAIN
    }
    assert (
        "lib/rake/file_list.rb:Rake.FileList",
        "lib/rake/file_list.rb:Rake.FileList.add()",
    ) in contain_edges
    assert (
        "lib/rake/file_list.rb:Rake.FileList",
        "lib/rake/file_list.rb:Rake.FileList.kind_of?()",
    ) in contain_edges


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
    monkeypatch.setenv("CODENIB_RUBY_SCIP_CMD", "bundle exec scip-ruby")

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


def test_read_only_scip_ruby_generation_skips_bundle_preparation(tmp_path, monkeypatch):
    import codenib.scip_interface.scip_indexer_ruby as ruby_module

    (tmp_path / "Gemfile").write_text(
        'source "https://rubygems.org"\ngem "scip-ruby"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODENIB_RUBY_SCIP_CMD", "bundle exec scip-ruby")
    indexer = SCIPRubyIndexer(tmp_path, output_dir=tmp_path / "out")
    monkeypatch.setattr(indexer, "_check_indexer_available", lambda: True)
    monkeypatch.setattr(
        indexer,
        "_prepare_bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("read-only onboarding must not run Bundler preparation")
        ),
    )

    def run(_command, **_kwargs):
        indexer.index_file.write_text("scip\n", encoding="utf-8")

    monkeypatch.setattr(ruby_module.subprocess, "run", run)

    assert indexer.generate_index(allow_project_preparation=False)
    assert not (tmp_path / ".bundle").exists()
    assert not (tmp_path / "vendor").exists()


def test_ruby_hybrid_indexer_uses_lsp_without_prepared_scip_bundle(tmp_path):
    (tmp_path / "Gemfile").write_text(
        'source "https://rubygems.org"\ngem "rake"\n',
        encoding="utf-8",
    )

    indexer = RubyHybridIndexer(tmp_path, output_dir=tmp_path / "out")

    assert indexer._delegate.__class__.__name__ == "GenericLSPIndexer"


def test_ruby_hybrid_indexer_prefers_scip_for_overlay_bundle(
    tmp_path,
    monkeypatch,
):
    overlay = tmp_path / ".codenib/Gemfile"
    overlay.parent.mkdir()
    overlay.write_text(
        'source "https://rubygems.org"\ngem "scip-ruby"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODENIB_RUBY_BUNDLE_GEMFILE", ".codenib/Gemfile")

    indexer = RubyHybridIndexer(tmp_path, output_dir=tmp_path / "out")

    assert indexer._delegate.__class__ is SCIPRubyIndexer


def test_ruby_hybrid_indexer_prefers_scip_for_project_scip_gemfile(tmp_path):
    (tmp_path / "Gemfile").write_text(
        'source "https://rubygems.org"\ngem "scip-ruby"\n',
        encoding="utf-8",
    )

    indexer = RubyHybridIndexer(tmp_path, output_dir=tmp_path / "out")

    assert indexer._delegate.__class__ is SCIPRubyIndexer


def test_ruby_hybrid_indexer_falls_back_to_lsp_when_scip_fails(
    tmp_path,
    monkeypatch,
):
    overlay = tmp_path / ".codenib/Gemfile"
    overlay.parent.mkdir()
    overlay.write_text(
        'source "https://rubygems.org"\ngem "scip-ruby"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODENIB_RUBY_BUNDLE_GEMFILE", ".codenib/Gemfile")
    calls = []

    def fake_scip_run_pipeline(self, **kwargs):
        calls.append(("scip", kwargs))
        return None

    def fake_lsp_run_pipeline(self, **kwargs):
        calls.append(("lsp", kwargs))
        graph = CodeGraph(str(tmp_path))
        graph.add_file_node("lib/fallback.rb")
        return graph

    monkeypatch.setattr(SCIPRubyIndexer, "run_pipeline", fake_scip_run_pipeline)
    monkeypatch.setattr(
        "codenib.ls_index.lsp_indexer.GenericLSPIndexer.run_pipeline",
        fake_lsp_run_pipeline,
    )

    graph = RubyHybridIndexer(tmp_path, output_dir=tmp_path / "out").run_pipeline(
        skip_level="graph",
        target_dir="lib",
    )

    assert graph is not None
    assert [call[0] for call in calls] == ["scip", "lsp"]
    assert calls[1][1]["target_dir"] == "lib"


def test_scip_ruby_indexer_unsets_gem_path_for_project_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("GEM_HOME", "/tmp/codenib-tools/gems")
    monkeypatch.setenv("GEM_PATH", "/tmp/codenib-tools/gems")
    (tmp_path / "Gemfile").write_text(
        'source "https://rubygems.org"\n', encoding="utf-8"
    )

    env = SCIPRubyIndexer(tmp_path, output_dir=tmp_path / "out")._ruby_env()

    assert env["GEM_HOME"] == "/tmp/codenib-tools/gems"
    assert "GEM_PATH" not in env
    assert env["BUNDLE_GEMFILE"] == str(tmp_path / "Gemfile")


def test_scip_ruby_indexer_uses_overlay_bundle_gemfile(tmp_path, monkeypatch):
    overlay = tmp_path / ".codenib/Gemfile"
    overlay.parent.mkdir()
    overlay.write_text('source "https://rubygems.org"\n', encoding="utf-8")
    monkeypatch.setenv("CODENIB_RUBY_BUNDLE_GEMFILE", ".codenib/Gemfile")

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
