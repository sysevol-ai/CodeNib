# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CodeNib's clangd artifacts belong in CodeNib state, not the checkout."""

import io
import json
from pathlib import Path

import pytest

from codenib.ls_index import clangd_indexer as clangd_indexer_module
from codenib.ls_index.clangd_indexer import BEAR_COMPDB_FILENAME, ClangdIndexer
from codenib.ls_index.index_quality import compilation_database_entry_count


def _write_compdb(path: Path, sources: list[Path]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "directory": str(source.parent),
                    "file": str(source),
                    "command": f"cc -c {source.name}",
                }
                for source in sources
            ]
        ),
        encoding="utf-8",
    )


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "Makefile").write_text("all:\n\t$(CC) -c src/*.c\n", encoding="utf-8")
    for index in range(10):
        (root / "src" / f"unit_{index}.c").write_text("int v;\n", encoding="utf-8")
    return root


def _sources(root: Path) -> list[Path]:
    return sorted((root / "src").glob("*.c"))


def _fake_bear(indexer, commands, *, major=3, write=None):
    """Record build commands; a bear run writes where its output flag says."""

    def default_write(path):
        _write_compdb(path, _sources(indexer.project_root))

    write = write or default_write

    def run(command):
        commands.append(command)
        if command[0] == "bear":
            flag = "-o" if major == 2 else "--output"
            write(Path(command[command.index(flag) + 1]))
        return True

    return run


@pytest.mark.parametrize("major,flag", [(3, "--output"), (2, "-o"), (None, "--output")])
def test_bear_writes_its_database_into_state(
    project, tmp_path, monkeypatch, major, flag
):
    indexer = ClangdIndexer(project, output_dir=tmp_path / "state")
    commands = []
    monkeypatch.setattr(clangd_indexer_module, "_bear_major_version", lambda: major)
    monkeypatch.setattr(
        indexer, "_run_build_command", _fake_bear(indexer, commands, major=major or 3)
    )

    compdb = indexer._bear_make()

    bear = [command for command in commands if command[0] == "bear"]
    assert bear and bear[0][:4] == ["bear", flag, str(indexer.bear_compdb_path), "--"]
    assert bear[0][4:6] == ["make", "-k"]
    assert indexer.bear_compdb_path == tmp_path / "state" / BEAR_COMPDB_FILENAME
    assert compdb is not None and compdb.parent == tmp_path / "state"
    assert compilation_database_entry_count(compdb) == 10
    assert not (project / "compile_commands.json").exists()
    assert not (project / "build").exists()


def test_bear_keeps_compilation_databases_that_predate_the_run(
    project, tmp_path, monkeypatch
):
    root_db = project / "compile_commands.json"
    build_db = project / "build" / "compile_commands.json"
    _write_compdb(root_db, _sources(project)[:1])
    _write_compdb(build_db, _sources(project)[:2])
    before = {path: path.read_bytes() for path in (root_db, build_db)}
    indexer = ClangdIndexer(project, output_dir=tmp_path / "state")
    commands = []
    monkeypatch.setattr(clangd_indexer_module, "_bear_major_version", lambda: 3)
    monkeypatch.setattr(indexer, "_run_build_command", _fake_bear(indexer, commands))

    compdb = indexer._bear_make()

    assert {path: path.read_bytes() for path in (root_db, build_db)} == before
    assert compilation_database_entry_count(compdb) == 10


def test_a_database_the_build_itself_writes_is_still_a_candidate(
    project, tmp_path, monkeypatch
):
    build_db = project / "build" / "compile_commands.json"
    indexer = ClangdIndexer(project, output_dir=tmp_path / "state")
    monkeypatch.setattr(clangd_indexer_module, "_bear_major_version", lambda: 3)
    monkeypatch.setattr(
        indexer,
        "_run_build_command",
        _fake_bear(
            indexer, [], write=lambda _path: _write_compdb(build_db, _sources(project))
        ),
    )

    compdb = indexer._bear_make()

    assert compdb is not None and compdb.parent == tmp_path / "state"
    assert compilation_database_entry_count(compdb) == 10


def test_next_run_reuses_the_state_database(project, tmp_path, monkeypatch):
    state = tmp_path / "state"
    _write_compdb(state / BEAR_COMPDB_FILENAME, _sources(project))
    indexer = ClangdIndexer(project, output_dir=state)
    # The build copy that generated it (and holds its generated headers).
    indexer.build_copy_dir.mkdir(parents=True)
    monkeypatch.setattr(
        indexer,
        "_auto_generate_compdb",
        lambda: pytest.fail("a preferred state database must be reused"),
    )
    seen = {}

    def generate_index(**kwargs):
        seen.update(kwargs)
        return False

    monkeypatch.setattr(indexer, "generate_index", generate_index)

    assert indexer.run_pipeline(report_profile=False) is None
    # Same clangd input path as when bear wrote into the checkout, so clangd's
    # shards stay at <state>/.cache/clangd/index across runs.
    assert seen["compdb_path"] == str(state / "compile_commands.json")


class _FakeClangd:
    """Stands in for clangd: writes shards next to its compilation database."""

    def __init__(self, cmd, **_kwargs):
        compile_dir = next(
            arg.split("=", 1)[1]
            for arg in cmd
            if arg.startswith("--compile-commands-dir=")
        )
        self.index_dir = Path(compile_dir) / ".cache" / "clangd" / "index"
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _run_fake_clangd(monkeypatch, indexer, *, writes):
    def wait_for_indexing(process, *_args, **_kwargs):
        if writes:
            process.index_dir.mkdir(parents=True, exist_ok=True)
            (process.index_dir / "unit_0.c.0123456789ABCDEF.idx").write_bytes(b"x")

    monkeypatch.setattr(clangd_indexer_module.subprocess, "Popen", _FakeClangd)
    monkeypatch.setattr(clangd_indexer_module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(indexer, "_wait_for_indexing", wait_for_indexing)


def _editor_index(project: Path) -> Path:
    editor = project / ".cache" / "clangd" / "index"
    editor.mkdir(parents=True)
    shard = editor / "unit_0.c.FEDCBA9876543210.idx"
    shard.write_bytes(b"editor shard")
    return shard


def test_clangd_run_keeps_the_editors_project_local_index(
    project, tmp_path, monkeypatch
):
    shard = _editor_index(project)
    state = tmp_path / "state"
    compdb = state / "compile_commands.json"
    _write_compdb(compdb, _sources(project))
    indexer = ClangdIndexer(project, output_dir=state)
    _run_fake_clangd(monkeypatch, indexer, writes=True)

    assert indexer._run_clangd_indexer(compdb) is True

    assert shard.read_bytes() == b"editor shard"
    assert indexer.idx_directory == state / ".cache" / "clangd" / "index"


def test_clangd_run_without_output_does_not_adopt_the_editors_index(
    project, tmp_path, monkeypatch
):
    shard = _editor_index(project)
    state = tmp_path / "state"
    compdb = state / "compile_commands.json"
    _write_compdb(compdb, _sources(project))
    indexer = ClangdIndexer(project, output_dir=state)
    _run_fake_clangd(monkeypatch, indexer, writes=False)

    assert indexer._run_clangd_indexer(compdb) is False
    assert shard.exists()


def test_clangd_run_clears_its_own_previous_index(project, tmp_path, monkeypatch):
    state = tmp_path / "state"
    stale = state / ".cache" / "clangd" / "index"
    stale.mkdir(parents=True)
    (stale / "deleted.c.0000000000000000.idx").write_bytes(b"stale")
    compdb = state / "compile_commands.json"
    _write_compdb(compdb, _sources(project))
    indexer = ClangdIndexer(project, output_dir=state)
    indexer.idx_directory = stale
    _run_fake_clangd(monkeypatch, indexer, writes=True)

    assert indexer._run_clangd_indexer(compdb) is True
    assert sorted(p.name for p in stale.glob("*.idx")) == [
        "unit_0.c.0123456789ABCDEF.idx"
    ]


def test_incremental_patcher_finds_the_state_database(project, tmp_path, monkeypatch):
    from codenib.graph.code_graph import CodeGraph
    from codenib.graph.incremental import patcher_cpp

    monkeypatch.setenv("CODENIB_TEMP_DIR", str(tmp_path / "state-root"))
    bear_db = ClangdIndexer(project).bear_compdb_path
    _write_compdb(bear_db, _sources(project))
    monkeypatch.setattr(patcher_cpp.shutil, "which", lambda _name: "/usr/bin/clangd")
    monkeypatch.setattr(
        ClangdIndexer,
        "_auto_generate_compdb",
        lambda self: pytest.fail("the earlier bear database must be reused"),
    )
    used = []

    def generate_index(self, compdb_path=None, **_kwargs):
        used.append(compdb_path)
        return False

    monkeypatch.setattr(ClangdIndexer, "generate_index", generate_index)

    patcher = patcher_cpp.PatcherCpp(str(project), CodeGraph(str(project)))
    assert patcher._reindex_changed_files(["src/unit_0.c"]) is None
    # clangd reads <dir>/compile_commands.json only, so the patcher hands it
    # a prepared copy of the state database under that name.
    prepared = bear_db.parent / "compile_commands.json"
    assert used == [str(prepared)]
    assert json.loads(prepared.read_text()) == json.loads(bear_db.read_text())
    assert not (project / "compile_commands.json").exists()


def test_incremental_patcher_feeds_clangd_a_generated_database(
    project, tmp_path, monkeypatch
):
    from codenib.graph.code_graph import CodeGraph
    from codenib.graph.incremental import patcher_cpp

    monkeypatch.setenv("CODENIB_TEMP_DIR", str(tmp_path / "state-root"))
    state = ClangdIndexer(project).output_dir
    snapshot = state / "compile_commands.root-parallel.json"

    def auto_generate(self):
        _write_compdb(snapshot, _sources(project))
        return snapshot

    used = []
    monkeypatch.setattr(patcher_cpp.shutil, "which", lambda _name: "/usr/bin/clangd")
    monkeypatch.setattr(ClangdIndexer, "_auto_generate_compdb", auto_generate)
    monkeypatch.setattr(
        ClangdIndexer,
        "generate_index",
        lambda self, compdb_path=None, **_kwargs: used.append(compdb_path) or False,
    )

    patcher = patcher_cpp.PatcherCpp(str(project), CodeGraph(str(project)))
    assert patcher._reindex_changed_files(["src/unit_0.c"]) is None
    assert used == [str(state / "compile_commands.json")]
    assert compilation_database_entry_count(state / "compile_commands.json") == 10
