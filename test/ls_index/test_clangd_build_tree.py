# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""C/C++ compilation databases are generated outside the user's checkout."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from codenib.ls_index import clangd_indexer as clangd_indexer_module
from codenib.ls_index.clangd_build_tree import (
    copy_source_tree,
    isolated_build_environment,
    rewrite_compile_database,
    rewrite_compile_entry,
    without_git_repository_variables,
)
from codenib.ls_index.clangd_indexer import ClangdIndexer
from codenib.ls_index.index_quality import compilation_database_entry_count


def _tree(root: Path) -> dict:
    """Every entry under root with its metadata, to prove nothing changed."""

    entries = {}
    for directory, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(directory) / name
            metadata = path.lstat()
            content = (
                path.read_bytes() if path.is_file() and not path.is_symlink() else None
            )
            entries[str(path.relative_to(root))] = (
                metadata.st_mode,
                metadata.st_size,
                metadata.st_mtime_ns,
                content,
            )
    return entries


# ----------------------------------------------------------------------------
# The copy
# ----------------------------------------------------------------------------


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "src").mkdir()
    (root / "src" / "main.c").write_text('#include "util.h"\nint main(void){}\n')
    (root / "src" / "util.h").write_text("int util(void);\n")
    (root / "configure").write_text("#!/bin/sh\n")
    (root / "configure").chmod(0o755)
    (root / ".codenib_cache").mkdir()
    (root / ".codenib_cache" / "graph.pkl").write_bytes(b"index")
    submodule = root / "modules" / "lib"
    submodule.mkdir(parents=True)
    (submodule / ".git").write_text("gitdir: ../../.git/modules/lib\n")
    (submodule / "lib.c").write_text("int lib(void) { return 0; }\n")
    (root / "src" / "link.h").symlink_to("util.h")
    (root / "src" / "abs.h").symlink_to(root / "src" / "util.h")
    (root / "outside.h").symlink_to("/usr/include/stdio.h")
    os.utime(root / "src" / "main.c", ns=(1_600_000_000_000_000_000,) * 2)
    return root


def test_copy_excludes_git_and_codenib_state(checkout, tmp_path):
    copy = tmp_path / "state" / "source"
    before = _tree(checkout)

    report = copy_source_tree(checkout, copy)

    assert not (copy / ".git").exists()
    assert not (copy / "modules" / "lib" / ".git").exists()
    assert not (copy / ".codenib_cache").exists()
    assert (copy / "modules" / "lib" / "lib.c").read_text().startswith("int lib")
    assert report.files == 4 and report.links == 3
    assert _tree(checkout) == before


def test_copy_makes_independent_files_with_original_mtimes(checkout, tmp_path):
    copy = tmp_path / "state" / "source"
    copy_source_tree(checkout, copy)

    for relative in ("src/main.c", "src/util.h", "configure", "modules/lib/lib.c"):
        original, copied = (checkout / relative).stat(), (copy / relative).stat()
        assert copied.st_ino != original.st_ino
        assert copied.st_nlink == 1 and original.st_nlink == 1
        assert copied.st_mtime_ns == original.st_mtime_ns
        assert copied.st_mode == original.st_mode
    (copy / "src" / "util.h").write_text("changed by the build\n")
    assert (checkout / "src" / "util.h").read_text() == "int util(void);\n"


def test_copy_redirects_absolute_links_into_the_checkout(checkout, tmp_path):
    copy = tmp_path / "state" / "source"
    copy_source_tree(checkout, copy)

    assert os.readlink(copy / "src" / "link.h") == "util.h"
    assert os.readlink(copy / "src" / "abs.h") == str(copy / "src" / "util.h")
    assert os.readlink(copy / "outside.h") == "/usr/include/stdio.h"


def test_copy_starts_fresh_each_time(checkout, tmp_path):
    copy = tmp_path / "state" / "source"
    copy_source_tree(checkout, copy)
    (copy / "src" / "main.o").write_bytes(b"object")
    (checkout / "src" / "util.h").unlink()

    copy_source_tree(checkout, copy)

    assert not (copy / "src" / "main.o").exists()
    assert not (copy / "src" / "util.h").exists()


def test_copy_inside_the_checkout_skips_itself(checkout):
    copy = checkout / ".state" / "source"
    copy_source_tree(checkout, copy, exclude=(checkout / ".state",))
    copy_source_tree(checkout, copy, exclude=(checkout / ".state",))
    assert not (copy / ".state").exists()
    assert (copy / "src" / "main.c").exists()


def test_copy_refuses_a_destination_containing_the_checkout(checkout):
    with pytest.raises(ValueError):
        copy_source_tree(checkout, checkout.parent)
    assert (checkout / "src" / "main.c").exists()


# ----------------------------------------------------------------------------
# Git isolation
# ----------------------------------------------------------------------------


def test_isolated_environment_drops_repository_variables(tmp_path):
    base = {
        "PATH": "/usr/bin",
        "GIT_DIR": "/user/repo/.git",
        "GIT_WORK_TREE": "/user/repo",
        "GIT_INDEX_FILE": "/user/repo/.git/index",
        "GIT_CEILING_DIRECTORIES": "/elsewhere",
        "GIT_CONFIG_COUNT": "0",
    }
    env = isolated_build_environment(tmp_path / "state" / "source", base)
    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env
    assert "GIT_INDEX_FILE" not in env
    assert env["GIT_CEILING_DIRECTORIES"] == (
        f"{tmp_path / 'state'}{os.pathsep}/elsewhere"
    )
    assert env["PATH"] == "/usr/bin" and env["GIT_CONFIG_COUNT"] == "0"
    assert "GIT_DIR" not in without_git_repository_variables(base)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_in_the_copy_cannot_reach_an_enclosing_repository(tmp_path):
    outer = tmp_path / "outer"
    copy = outer / "state" / "source"
    (copy / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    plain = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=copy / "src",
        capture_output=True,
        text=True,
    )
    assert plain.returncode == 0  # without isolation git finds the outer repo

    isolated = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=copy / "src",
        capture_output=True,
        text=True,
        env=isolated_build_environment(copy, {**os.environ, "GIT_DIR": str(outer)}),
    )
    assert isolated.returncode != 0
    assert "not a git repository" in isolated.stderr


# ----------------------------------------------------------------------------
# Compile database rewrite
# ----------------------------------------------------------------------------


@pytest.fixture
def trees(tmp_path):
    checkout = tmp_path / "checkout"
    copy = tmp_path / "state" / "source"
    for root in (checkout, copy):
        (root / "src").mkdir(parents=True)
        (root / "include").mkdir()
        (root / "src" / "main.c").write_text("int main(void) { return 0; }\n")
        (root / "include" / "api.h").write_text("int api(void);\n")
        (root / "include" / "prefix.h").write_text("#define P 1\n")
    # Products of the build that exist only in the copy.
    (copy / "src" / "release.h").write_text("#define R 1\n")
    (copy / "src" / "lexer.c").write_text("int lex(void) { return 0; }\n")
    (copy / "gen").mkdir()
    (copy / "gen" / "config.h").write_text("#define C 1\n")
    return checkout, copy


def _entry(copy: Path, arguments, file="main.c", directory="src", **extra):
    return {
        "arguments": arguments,
        "directory": str(copy / directory),
        "file": file,
        **extra,
    }


def test_rewrite_moves_sources_to_the_checkout_and_keeps_copy_fallbacks(trees):
    checkout, copy = trees
    entry = _entry(
        copy,
        [
            "/usr/bin/cc",
            "-I../include",
            "-I",
            "../gen",
            "-isystem",
            f"{copy}/include",
            "-iquote../include",
            "--include-directory=../include",
            "-I/usr/include/extra",
            "-include",
            "../include/prefix.h",
            "-imacros",
            "../gen/config.h",
            "-c",
            "-o",
            "main.o",
            "-MF",
            "main.d",
            "main.c",
        ],
        output="main.o",
    )

    rewritten = rewrite_compile_entry(entry, copy_root=copy, checkout_root=checkout)

    assert rewritten["file"] == str(checkout / "src" / "main.c")
    assert rewritten["directory"] == str(checkout / "src")
    assert rewritten["output"] == str(copy / "src" / "main.o")
    assert rewritten["arguments"] == [
        "/usr/bin/cc",
        # The compiler searched the main file's directory first for quoted
        # includes; that directory in the copy holds generated headers.
        "-iquote",
        str(copy / "src"),
        f"-I{checkout}/include",
        f"-I{copy}/include",
        "-I",
        str(copy / "gen"),  # generated directory: copy only
        "-isystem",
        str(checkout / "include"),
        "-isystem",
        str(copy / "include"),
        f"-iquote{checkout}/include",
        f"-iquote{copy}/include",
        f"--include-directory={checkout}/include",
        f"--include-directory={copy}/include",
        "-I/usr/include/extra",
        "-include",
        str(checkout / "include" / "prefix.h"),
        "-imacros",
        str(copy / "gen" / "config.h"),
        "-c",
        "-o",
        str(copy / "src" / "main.o"),
        "-MF",
        str(copy / "src" / "main.d"),
        str(checkout / "src" / "main.c"),
    ]


def test_generated_sources_keep_their_copy_paths(trees):
    checkout, copy = trees
    entry = _entry(copy, ["cc", "-I../include", "-c", "lexer.c"], file="lexer.c")

    rewritten = rewrite_compile_entry(entry, copy_root=copy, checkout_root=checkout)

    assert rewritten["file"] == str(copy / "src" / "lexer.c")
    assert rewritten["directory"] == str(copy / "src")
    assert rewritten["arguments"] == [
        "cc",
        f"-I{checkout}/include",
        f"-I{copy}/include",
        "-c",
        "lexer.c",
    ]


def test_command_strings_and_absolute_sources(trees):
    checkout, copy = trees
    entry = {
        "directory": str(copy),
        "file": str(copy / "src" / "main.c"),
        "command": f"cc -I- -Iinclude -c {copy}/src/main.c -o 'out dir/main.o'",
    }

    rewritten = rewrite_compile_entry(entry, copy_root=copy, checkout_root=checkout)

    assert "command" not in rewritten
    assert rewritten["arguments"] == [
        "cc",
        "-iquote",
        str(copy / "src"),
        "-I-",
        f"-I{checkout}/include",
        f"-I{copy}/include",
        "-c",
        str(checkout / "src" / "main.c"),
        "-o",
        str(copy / "out dir" / "main.o"),
    ]
    assert rewritten["directory"] == str(checkout)


def test_rewrite_database_counts_moved_entries(trees, tmp_path):
    checkout, copy = trees
    raw = tmp_path / "raw.json"
    raw.write_text(
        json.dumps(
            [
                _entry(copy, ["cc", "-c", "main.c"]),
                _entry(copy, ["cc", "-c", "lexer.c"], file="lexer.c"),
            ]
        )
    )
    out = tmp_path / "out.json"

    moved = rewrite_compile_database(raw, out, copy_root=copy, checkout_root=checkout)

    assert moved == 1
    assert [e["file"] for e in json.loads(out.read_text())] == [
        str(checkout / "src" / "main.c"),
        str(copy / "src" / "lexer.c"),
    ]


# ----------------------------------------------------------------------------
# The indexer
# ----------------------------------------------------------------------------


@pytest.fixture
def make_project(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "Makefile").write_text("all:\n\t$(CC) -c src/*.c\n")
    (root / ".gitmodules").write_text(
        '[submodule "modules/dep"]\n\tpath = modules/dep\n\turl = x\n'
    )
    (root / "modules" / "dep").mkdir(parents=True)
    for index in range(10):
        (root / "src" / f"unit_{index}.c").write_text("int v;\n")
    return root


def test_bear_runs_in_an_isolated_copy_and_never_writes_the_checkout(
    make_project, tmp_path, monkeypatch, caplog
):
    state = tmp_path / "state"
    indexer = ClangdIndexer(make_project, output_dir=state)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs.get("cwd"), kwargs.get("env")))
        cwd = Path(kwargs["cwd"])
        if cmd[0] == "bear":
            (cwd / "src" / "unit_0.o").write_bytes(b"object")
            target = Path(cmd[cmd.index("--output") + 1])
            target.write_text(
                json.dumps(
                    [
                        {
                            "directory": str(cwd),
                            "file": f"src/unit_{index}.c",
                            "arguments": ["cc", "-Isrc", "-c", f"src/unit_{index}.c"],
                        }
                        for index in range(10)
                    ]
                )
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(clangd_indexer_module, "_bear_major_version", lambda: 3)
    monkeypatch.setattr(clangd_indexer_module.shutil, "which", lambda name: name)
    monkeypatch.setattr(clangd_indexer_module.subprocess, "run", fake_run)
    before = _tree(make_project)
    caplog.set_level("WARNING")
    clangd_indexer_module.logger.addHandler(caplog.handler)
    try:
        compdb = indexer._auto_generate_compdb()
    finally:
        clangd_indexer_module.logger.removeHandler(caplog.handler)

    assert _tree(make_project) == before
    copy = indexer.build_copy_dir
    assert (copy / "src" / "unit_0.o").exists()
    assert all(Path(cwd) == copy for _cmd, cwd, _env in calls)
    assert all(
        env["GIT_CEILING_DIRECTORIES"].startswith(str(copy.parent))
        for _cmd, _cwd, env in calls
    )
    assert not any(cmd[0] == "git" for cmd, _cwd, _env in calls)
    assert "modules/dep" in caplog.text
    entries = json.loads(compdb.read_text())
    assert compilation_database_entry_count(compdb) == 10
    assert {e["file"] for e in entries} == {
        str(make_project / "src" / f"unit_{index}.c") for index in range(10)
    }
    assert json.loads(indexer.bear_compdb_path.read_text()) == entries


def test_reused_bear_database_without_its_copy_is_regenerated(make_project, tmp_path):
    indexer = ClangdIndexer(make_project, output_dir=tmp_path / "state")
    indexer.bear_compdb_path.write_text("[]")
    assert indexer._compdb_lost_its_build_copy(indexer.bear_compdb_path)
    indexer.build_copy_dir.mkdir(parents=True)
    assert not indexer._compdb_lost_its_build_copy(indexer.bear_compdb_path)
    assert not indexer._compdb_lost_its_build_copy(tmp_path / "other.json")


def test_cmake_configures_into_state_not_the_checkout(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "CMakeLists.txt").write_text("project(x C)\n")
    indexer = ClangdIndexer(project, output_dir=tmp_path / "state")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(cmd=list(cmd), cwd=kwargs.get("cwd"), env=kwargs.get("env"))
        build = Path(cmd[cmd.index("-B") + 1])
        (build / "compile_commands.json").write_text("[]")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(clangd_indexer_module.shutil, "which", lambda name: name)
    monkeypatch.setattr(clangd_indexer_module.subprocess, "run", fake_run)
    monkeypatch.setenv("GIT_DIR", str(project / ".git"))

    compdb = indexer._auto_generate_compdb_cmake()

    build = tmp_path / "state" / "clangd-build" / "cmake"
    assert seen["cmd"][:5] == ["cmake", "-S", str(project), "-B", str(build)]
    assert Path(seen["cwd"]) == build
    assert "GIT_DIR" not in seen["env"]
    assert compdb == build / "compile_commands.json"
    assert not (project / "build").exists()
    assert build / "compile_commands.json" in indexer._state_compdb_candidates()


@pytest.mark.skipif(shutil.which("cmake") is None, reason="cmake not installed")
def test_real_cmake_keeps_generated_headers_in_state(tmp_path):
    project = tmp_path / "repo"
    (project / "src").mkdir(parents=True)
    (project / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.10)\n"
        "project(fixture C)\n"
        "configure_file(version.h.in ${CMAKE_BINARY_DIR}/version.h)\n"
        "include_directories(${CMAKE_BINARY_DIR})\n"
        "add_library(fixture src/lib.c)\n"
    )
    (project / "version.h.in").write_text("#define VERSION 1\n")
    (project / "src" / "lib.c").write_text('#include "version.h"\nint v = VERSION;\n')
    before = _tree(project)
    indexer = ClangdIndexer(project, output_dir=tmp_path / "state")

    compdb = indexer._auto_generate_compdb_cmake()

    assert _tree(project) == before
    assert compdb is not None and indexer._is_preferred_compdb(compdb)
    [entry] = json.loads(compdb.read_text())
    assert Path(entry["file"]).resolve() == (project / "src" / "lib.c").resolve()
    assert (indexer.cmake_build_dir / "version.h").exists()
