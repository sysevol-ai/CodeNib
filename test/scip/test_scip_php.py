# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit coverage for the scip-php decoder and PHP hybrid route."""

from __future__ import annotations

import subprocess

from codenib.graph.code_graph import CodeGraph
from codenib.scip_interface.scip_decode_php import SCIPPHPGraphDecoder
from codenib.scip_interface.scip_indexer_php import PHPHybridIndexer, SCIPPHPIndexer
from codenib.types import EDGE_TYPE_CONTAIN, EDGE_TYPE_REFERENCE

PHP_INDEX = """
metadata {
  version: 1
  tool_info {
    name: "scip-php"
    version: "0.0.1"
  }
}
documents {
  relative_path: "src/Billing/Invoice.php"
  occurrences {
    range: 5
    range: 20
    range: 5
    range: 25
    symbol: "scip-php composer codenib/php-scip-smoke abc Smoke/Billing/Invoice#total()."
    symbol_roles: 1
    syntax_kind: Identifier
  }
  occurrences {
    range: 5
    range: 26
    range: 5
    range: 32
    symbol: "scip-php composer smoke abc Smoke/Billing/Invoice#total().($value)"
    symbol_roles: 1
    syntax_kind: IdentifierParameter
  }
  occurrences {
    range: 3
    range: 6
    range: 3
    range: 13
    symbol: "scip-php composer codenib/php-scip-smoke abc Smoke/Billing/Invoice#"
    symbol_roles: 1
    syntax_kind: IdentifierType
  }
  occurrences {
    range: 13
    range: 16
    range: 13
    range: 23
    symbol: "scip-php composer codenib/php-scip-smoke abc Smoke/Billing/Invoice#"
    syntax_kind: Identifier
  }
  occurrences {
    range: 13
    range: 28
    range: 13
    range: 33
    symbol: "scip-php composer codenib/php-scip-smoke abc Smoke/Billing/Invoice#total()."
    syntax_kind: Identifier
  }
  symbols {
    symbol: "scip-php composer codenib/php-scip-smoke abc Smoke/Billing/Invoice#total()."
    documentation: "```php\\npublic function total(): int\\n```"
  }
  symbols {
    symbol: "scip-php composer smoke abc Smoke/Billing/Invoice#total().($value)"
    documentation: "```php\\nint $value\\n```"
  }
  symbols {
    symbol: "scip-php composer codenib/php-scip-smoke abc Smoke/Billing/Invoice#"
    documentation: "```php\\nclass Invoice\\n```"
  }
  language: "19"
}
""".strip()


def test_scip_php_decoder_builds_symbols_and_references(tmp_path):
    source = tmp_path / "src/Billing/Invoice.php"
    source.parent.mkdir(parents=True)
    source.write_text(
        """<?php
namespace Smoke\\Billing;

class Invoice
{
    public function total(): int
    {
        return 1;
    }
}

function normalize(): string
{
    return (new Invoice())->total();
}
""",
        encoding="utf-8",
    )
    index = tmp_path / "index.decoded"
    index.write_text(PHP_INDEX, encoding="utf-8")

    graph = SCIPPHPGraphDecoder(str(index), project_root=str(tmp_path)).decode()
    graph.build_range_indexes()

    namespace_key = "src/Billing/Invoice.php:Smoke/Billing"
    namespace = graph.graph.vs[graph.name_to_vertex[namespace_key]]
    invoice = graph.graph.vs[graph.name_to_vertex["Smoke/Billing/Invoice"]]
    total = graph.graph.vs[graph.name_to_vertex["Smoke/Billing/Invoice#total"]]
    normalize = graph.graph.vs[graph.name_to_vertex["Smoke/Billing/normalize"]]

    assert namespace["unified_name"] == "src/Billing/Invoice.php:Smoke\\Billing"
    assert invoice["unified_name"] == "src/Billing/Invoice.php:Invoice"
    assert total["unified_name"] == "src/Billing/Invoice.php:Invoice.total()"
    assert normalize["unified_name"] == "src/Billing/Invoice.php:normalize()"
    assert "Smoke/Billing/Invoice#total().($value)" not in graph.name_to_vertex

    namespace_contain = graph.graph.es.find(
        _source=graph.name_to_vertex["src/Billing/Invoice.php"],
        _target=graph.name_to_vertex[namespace_key],
    )
    assert namespace_contain["type"] == EDGE_TYPE_CONTAIN

    class_contain = graph.graph.es.find(
        _source=graph.name_to_vertex["src/Billing/Invoice.php"],
        _target=graph.name_to_vertex["Smoke/Billing/Invoice"],
    )
    assert class_contain["type"] == EDGE_TYPE_CONTAIN

    contain = graph.graph.es.find(
        _source=graph.name_to_vertex["Smoke/Billing/Invoice"],
        _target=graph.name_to_vertex["Smoke/Billing/Invoice#total"],
    )
    assert contain["type"] == EDGE_TYPE_CONTAIN

    function_contain = graph.graph.es.find(
        _source=graph.name_to_vertex["src/Billing/Invoice.php"],
        _target=graph.name_to_vertex["Smoke/Billing/normalize"],
    )
    assert function_contain["type"] == EDGE_TYPE_CONTAIN

    reference = graph.graph.es.find(
        _source=graph.name_to_vertex["src/Billing/Invoice.php"],
        _target=graph.name_to_vertex["Smoke/Billing/Invoice#total"],
    )
    assert reference["type"] == EDGE_TYPE_REFERENCE
    assert reference["anchor_file"] == "src/Billing/Invoice.php"
    assert reference["anchor_line"] == 13


def test_scip_php_indexer_builds_registered_command(tmp_path, monkeypatch):
    monkeypatch.setenv("CODENIB_PHP_SCIP_CMD", "vendor/bin/scip-php")

    indexer = SCIPPHPIndexer(tmp_path, output_dir=tmp_path / "out")

    assert indexer._build_index_command() == ["vendor/bin/scip-php"]
    assert indexer._get_decoder_class() is SCIPPHPGraphDecoder


def test_php_hybrid_indexer_uses_lsp_for_non_composer_projects(tmp_path):
    indexer = PHPHybridIndexer(tmp_path, output_dir=tmp_path / "out")

    assert indexer._delegate.__class__.__name__ == "GenericLSPIndexer"


def test_php_hybrid_indexer_prefers_scip_for_composer_projects(tmp_path):
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")

    indexer = PHPHybridIndexer(tmp_path, output_dir=tmp_path / "out")

    assert indexer._delegate.__class__ is SCIPPHPIndexer


def test_php_hybrid_indexer_falls_back_to_lsp_when_scip_fails(tmp_path, monkeypatch):
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    calls = []

    def fake_scip_run_pipeline(self, **kwargs):
        calls.append(("scip", kwargs))
        return None

    def fake_lsp_run_pipeline(self, **kwargs):
        calls.append(("lsp", kwargs))
        graph = CodeGraph(str(tmp_path))
        graph.add_file_node("src/Fallback.php")
        return graph

    monkeypatch.setattr(SCIPPHPIndexer, "run_pipeline", fake_scip_run_pipeline)
    monkeypatch.setattr(
        "codenib.ls_index.lsp_indexer.GenericLSPIndexer.run_pipeline",
        fake_lsp_run_pipeline,
    )

    graph = PHPHybridIndexer(tmp_path, output_dir=tmp_path / "out").run_pipeline(
        skip_level="graph",
        target_dir="src",
    )

    assert graph is not None
    assert [call[0] for call in calls] == ["scip", "lsp"]
    assert calls[1][1]["target_dir"] == "src"


def test_php_hybrid_indexer_falls_back_to_lsp_when_scip_raises(tmp_path, monkeypatch):
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    calls = []

    def fake_scip_run_pipeline(self, **kwargs):
        calls.append(("scip", kwargs))
        raise RuntimeError("composer failed")

    def fake_lsp_run_pipeline(self, **kwargs):
        calls.append(("lsp", kwargs))
        graph = CodeGraph(str(tmp_path))
        graph.add_file_node("src/Fallback.php")
        return graph

    monkeypatch.setattr(SCIPPHPIndexer, "run_pipeline", fake_scip_run_pipeline)
    monkeypatch.setattr(
        "codenib.ls_index.lsp_indexer.GenericLSPIndexer.run_pipeline",
        fake_lsp_run_pipeline,
    )

    graph = PHPHybridIndexer(tmp_path, output_dir=tmp_path / "out").run_pipeline(
        target_dir="src",
    )

    assert graph is not None
    assert [call[0] for call in calls] == ["scip", "lsp"]
    assert calls[1][1]["target_dir"] == "src"


def test_scip_php_indexer_requires_composer_project_state(tmp_path):
    indexer = SCIPPHPIndexer(tmp_path, output_dir=tmp_path / "out")

    assert not indexer._project_prerequisites_ok()


def test_scip_php_indexer_generates_index_in_throwaway_worktree(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CODENIB_PHP_SCIP_CMD", "vendor/bin/scip-php")
    project = tmp_path / "project"
    project.mkdir()
    (project / "composer.json").write_text(
        '{"name":"codenib/php-fixture","require":{}}\n',
        encoding="utf-8",
    )
    (project / "src").mkdir()
    (project / "src/Invoice.php").write_text("<?php\n", encoding="utf-8")
    output_dir = tmp_path / "out"
    index_cwds = []

    monkeypatch.setattr(
        "codenib.scip_interface.scip_indexer_php.shutil.which",
        lambda command: (
            f"/usr/bin/{command}" if command in {"php", "composer", "git"} else None
        ),
    )

    def fake_run(command, **kwargs):
        cwd = kwargs.get("cwd")
        if command == ["vendor/bin/scip-php"]:
            index_cwds.append(cwd)
            (cwd / "index.scip").write_bytes(b"scip")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(
        "codenib.scip_interface.scip_indexer_php.subprocess.run",
        fake_run,
    )

    indexer = SCIPPHPIndexer(project, output_dir=output_dir)

    assert indexer.generate_index()
    assert indexer.index_file.read_bytes() == b"scip"
    assert index_cwds == [output_dir / "worktree"]
    assert (output_dir / "worktree/src/Invoice.php").exists()
    assert not (project / "vendor/bin/scip-php").exists()
    assert not (project / "index.scip").exists()


def test_scip_php_indexer_accepts_preinstalled_project_tool_without_composer(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CODENIB_PHP_SCIP_CMD", "vendor/bin/scip-php")
    monkeypatch.setattr(
        "codenib.scip_interface.scip_indexer_php.shutil.which",
        lambda command: "/usr/bin/php" if command == "php" else None,
    )
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "vendor/bin").mkdir(parents=True)
    (tmp_path / "vendor/bin/scip-php").write_text("#!/usr/bin/env php\n")
    (tmp_path / "vendor/davidrjenni/scip-php/vendor").mkdir(parents=True)
    (tmp_path / "vendor/davidrjenni/scip-php/vendor/autoload.php").write_text(
        "<?php\n",
        encoding="utf-8",
    )

    indexer = SCIPPHPIndexer(tmp_path, output_dir=tmp_path / "out")

    assert indexer._check_indexer_available()


def test_scip_php_indexer_can_use_docker_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("CODENIB_PHP_SCIP_CMD", "vendor/bin/scip-php")
    monkeypatch.setenv("CODENIB_PHP_COMPOSER_IMAGE", "composer:test")
    monkeypatch.setattr(
        "codenib.scip_interface.scip_indexer_php.shutil.which",
        lambda command: "/usr/bin/docker" if command == "docker" else None,
    )
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "composer.lock").write_text("{}", encoding="utf-8")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor/autoload.php").write_text("<?php\n", encoding="utf-8")
    (tmp_path / "vendor/bin").mkdir()
    (tmp_path / "vendor/bin/scip-php").write_text("#!/usr/bin/env php\n")

    indexer = SCIPPHPIndexer(tmp_path, output_dir=tmp_path / "out")

    assert indexer._check_indexer_available()
    command = indexer._build_index_command()
    assert command[:4] == ["docker", "run", "--rm", "--user"]
    assert command[-2:] == ["composer:test", "vendor/bin/scip-php"]
    assert f"{tmp_path}:/app" in command


def test_scip_php_indexer_docker_indexes_throwaway_worktree(tmp_path, monkeypatch):
    monkeypatch.setenv("CODENIB_PHP_SCIP_CMD", "vendor/bin/scip-php")
    monkeypatch.setenv("CODENIB_PHP_COMPOSER_IMAGE", "composer:test")
    project = tmp_path / "project"
    project.mkdir()
    (project / "composer.json").write_text("{}", encoding="utf-8")
    (project / "vendor/bin").mkdir(parents=True)
    (project / "vendor/bin/scip-php").write_text("#!/usr/bin/env php\n")
    (project / "vendor/davidrjenni/scip-php/vendor").mkdir(parents=True)
    (project / "vendor/davidrjenni/scip-php/vendor/autoload.php").write_text(
        "<?php\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    docker_commands = []

    monkeypatch.setattr(
        "codenib.scip_interface.scip_indexer_php.shutil.which",
        lambda command: (
            "/usr/bin/docker"
            if command == "docker"
            else ("/usr/bin/git" if command == "git" else None)
        ),
    )

    def fake_run(command, **kwargs):
        if command[:2] == ["docker", "run"]:
            docker_commands.append(command)
            (output_dir / "worktree/index.scip").write_bytes(b"scip")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(
        "codenib.scip_interface.scip_indexer_php.subprocess.run",
        fake_run,
    )

    indexer = SCIPPHPIndexer(project, output_dir=output_dir)

    assert indexer.generate_index()
    assert indexer.index_file.read_bytes() == b"scip"
    assert f"{output_dir / 'worktree'}:/app" in docker_commands[-1]
    assert f"{project}:/app" not in docker_commands[-1]
    assert not (project / "index.scip").exists()
