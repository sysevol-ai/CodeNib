# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path

from codeminer.ls_index.clangd_indexer import ClangdIndexer
from codeminer.ls_index.index_quality import (
    IndexQualityPolicy,
    assess_index_quality,
    compilation_database_entry_count,
    prepare_compilation_database_for_indexing,
)


class _RawGraph:
    def __init__(self, vertices: int, edges: int):
        self.vs = [{} for _ in range(vertices)]
        self.es = [{} for _ in range(edges)]


class _Graph:
    def __init__(self, vertices: int, edges: int, range_files: list[str] | None = None):
        self.graph = _RawGraph(vertices, edges)
        files = range_files or ["src/main.c"]
        self._file_nodes = {path: [0] for path in files}
        self._file_edge_anchors = {path: [0] for path in files}
        self._unified_to_names = {"main": ["symbol-main"]}


def _write_compdb(path: Path, sources: list[Path], *, command: str = "cc -c") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "directory": str(source.parent),
                    "file": str(source),
                    "command": f"{command} {source.name}",
                }
                for source in sources
            ]
        ),
        encoding="utf-8",
    )


def test_prepare_compdb_neutralizes_warning_as_error_flags(tmp_path):
    source = tmp_path / "source.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    original = tmp_path / "compile_commands.json"
    original.write_text(
        json.dumps(
            [
                {
                    "directory": str(tmp_path),
                    "file": str(source),
                    "arguments": ["cc", "-Wall", "-Werror", "-Werror=return-type"],
                }
            ]
        ),
        encoding="utf-8",
    )

    prepared, rewrite_count = prepare_compilation_database_for_indexing(
        original, tmp_path / "out" / "compile_commands.index.json"
    )

    payload = json.loads(prepared.read_text(encoding="utf-8"))
    assert rewrite_count == 2
    assert payload[0]["arguments"] == [
        "cc",
        "-Wall",
        "-Wno-error",
        "-Wno-error=return-type",
    ]
    assert "-Werror" in original.read_text(encoding="utf-8")


def test_quality_rejects_low_source_coverage_and_graph_shrink(tmp_path):
    sources = []
    for index in range(100):
        source = tmp_path / "src" / f"unit_{index}.c"
        source.parent.mkdir(exist_ok=True)
        source.write_text("int value;\n", encoding="utf-8")
        sources.append(source)
    compdb = tmp_path / "compile_commands.json"
    _write_compdb(compdb, sources[:2])

    report = assess_index_quality(
        _Graph(vertices=100, edges=200),
        project_root=tmp_path,
        language="cpp",
        compdb_path=compdb,
        baseline_graph=_Graph(vertices=1000, edges=2000),
        policy=IndexQualityPolicy(min_source_coverage=0.1),
    )

    assert report["passed"] is False
    assert report["compile_db"]["entry_count"] == 2
    assert report["compile_db"]["source_coverage"] == 0.02
    assert report["graph_ratios"] == {"vertex_ratio": 0.1, "edge_ratio": 0.1}
    assert set(report["failure_names"]) == {
        "source_coverage",
        "graph_compdb_coverage",
        "baseline_vertex_ratio",
        "baseline_edge_ratio",
    }


def test_quality_accepts_graph_covering_compilation_database(tmp_path):
    sources = []
    for index in range(4):
        source = tmp_path / "src" / f"unit_{index}.c"
        source.parent.mkdir(exist_ok=True)
        source.write_text("int value;\n", encoding="utf-8")
        sources.append(source)
    compdb = tmp_path / "compile_commands.json"
    _write_compdb(compdb, sources)
    graph = _Graph(
        vertices=40,
        edges=80,
        range_files=[f"src/unit_{index}.c" for index in range(4)],
    )

    report = assess_index_quality(
        graph,
        project_root=tmp_path,
        language="cpp",
        compdb_path=compdb,
    )

    assert report["passed"] is True
    assert report["compile_db"]["graph_compdb_coverage"] == 1.0


def test_quality_accepts_header_centric_graph_aligned_with_compdb(tmp_path):
    sources = []
    for index in range(4):
        source = tmp_path / "src" / f"unit_{index}.cc"
        source.parent.mkdir(exist_ok=True)
        source.write_text("int value;\n", encoding="utf-8")
        sources.append(source)
    header = tmp_path / "include" / "api.h"
    header.parent.mkdir()
    header.write_text("int api();\n", encoding="utf-8")
    compdb = tmp_path / "compile_commands.json"
    _write_compdb(compdb, sources)
    graph = _Graph(
        vertices=40,
        edges=80,
        range_files=["src/unit_0.cc", "include/api.h"],
    )

    report = assess_index_quality(
        graph,
        project_root=tmp_path,
        language="cpp",
        compdb_path=compdb,
    )

    assert report["passed"] is True
    assert report["compile_db"]["translation_unit_count"] == 4
    assert report["compile_db"]["graph_translation_unit_count"] == 1
    assert report["compile_db"]["graph_compdb_coverage"] == 1.0


def test_subdirectory_bear_accepts_compdb_even_when_build_fails(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    subdir = project / "ports" / "unix"
    subdir.mkdir(parents=True)
    (subdir / "Makefile").write_text(
        "CFLAGS += $(CFLAGS_EXTRA)\nall:\n\t$(CC) $(CFLAGS) source.c\n",
        encoding="utf-8",
    )
    sources = []
    for index in range(10):
        source = subdir / f"unit_{index}.c"
        source.write_text("int value;\n", encoding="utf-8")
        sources.append(source)

    indexer = ClangdIndexer(project, output_dir=tmp_path / "output")
    commands = []

    def fake_run(command):
        commands.append(command)
        if command[0] == "bear":
            _write_compdb(project / "compile_commands.json", sources)
            return False
        return True

    monkeypatch.setattr(indexer, "_run_build_command", fake_run)

    compdb = indexer._try_subdirectory_make()

    assert compdb is not None
    assert compilation_database_entry_count(compdb) == 10
    bear_commands = [command for command in commands if command[0] == "bear"]
    assert len(bear_commands) == 1
    assert "CFLAGS_EXTRA=-Wno-error" in bear_commands[0]


def test_make_warning_override_preserves_project_extra_flags(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "Makefile").write_text(
        "CFLAGS_EXTRA ?= -DPROJECT_INDEX\n"
        "CFLAGS_EXTRA += -Igenerated\n"
        "CFLAGS += $(CFLAGS_EXTRA)\n",
        encoding="utf-8",
    )

    indexer = ClangdIndexer(project, output_dir=tmp_path / "output")

    assert indexer._indexing_make_overrides(project) == [
        "CFLAGS_EXTRA=-DPROJECT_INDEX -Igenerated -Wno-error"
    ]


def test_compdb_selection_prefers_resolved_coverage_over_raw_entry_count(tmp_path):
    project = tmp_path / "repo"
    source_dir = project / "src"
    source_dir.mkdir(parents=True)
    sources = []
    for index in range(10):
        source = source_dir / f"unit_{index}.c"
        source.write_text("int value;\n", encoding="utf-8")
        sources.append(source)

    larger_invalid = tmp_path / "larger-invalid.json"
    _write_compdb(
        larger_invalid,
        [project / "missing" / f"unit_{index}.c" for index in range(11)],
    )
    resolved = tmp_path / "resolved.json"
    _write_compdb(resolved, sources)
    indexer = ClangdIndexer(project, output_dir=tmp_path / "output")

    assert indexer._better_compdb(larger_invalid, resolved) == resolved


def test_pipeline_rejects_nonempty_but_low_coverage_graph(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    source_dir = project / "src"
    source_dir.mkdir(parents=True)
    sources = []
    for index in range(100):
        source = source_dir / f"unit_{index}.c"
        source.write_text("int value;\n", encoding="utf-8")
        sources.append(source)
    _write_compdb(project / "compile_commands.json", sources[:2])

    indexer = ClangdIndexer(project, output_dir=tmp_path / "output")
    monkeypatch.setattr(indexer, "generate_index", lambda **kwargs: True)

    def fake_process(output_file):
        Path(output_file).write_text("rejected graph", encoding="utf-8")
        return _Graph(vertices=100, edges=200)

    monkeypatch.setattr(
        indexer,
        "process_index",
        fake_process,
    )

    graph = indexer.run_pipeline(
        report_profile=False,
        quality_policy=IndexQualityPolicy(min_source_coverage=0.1),
    )

    assert graph is None
    assert indexer.compdb_path.name == "compile_commands.json"
    assert indexer.index_quality_report["failure_names"] == [
        "source_coverage",
        "graph_compdb_coverage",
    ]
    report_path = indexer.output_dir / "index_quality.json"
    assert json.loads(report_path.read_text(encoding="utf-8"))["passed"] is False
    assert not indexer.graph_file.exists()
    assert indexer.graph_file.with_name("graph.rejected.pkl").exists()
