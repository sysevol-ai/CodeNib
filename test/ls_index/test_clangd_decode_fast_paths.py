# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Fast paths in the clangd decoder must not change decoded values."""

import gc
import os
from pathlib import Path

import pytest

from codenib.ls_index import clangd_decode
from codenib.ls_index.clangd_decode import ClangdGraphDecoder, _gc_paused

posix_only = pytest.mark.skipif(os.sep != "/", reason="POSIX path semantics")


def _reference_relative(root: Path, uri: str) -> str:
    abs_path = uri[7:] if uri.startswith("file://") else uri
    try:
        return str(Path(abs_path).relative_to(root))
    except ValueError:
        return abs_path


@posix_only
@pytest.mark.parametrize(
    "suffix",
    [
        "/src/server.c",
        "/a.h",
        "/x",
        "/deps/lua/src/lua.h",
        "/src//double.c",
        "/src/./dot.c",
        "/./lead.c",
        "/src/trail/",
        "/src/trail/.",
        "/.",
        "",
        "/",
        "/../outside.c",
        "/src/../up.c",
        "/.hidden/file.c",
        "/src/.../dots.c",
        "/src/name with space.c",
        "-sibling/file.c",
        "_sibling/file.c",
    ],
)
@pytest.mark.parametrize("scheme", ["file://", ""])
def test_relative_path_matches_pathlib(tmp_path, suffix, scheme):
    root = (tmp_path / "repo").resolve()
    decoder = ClangdGraphDecoder(str(tmp_path / "index"), str(root))
    uri = f"{scheme}{root}{suffix}"
    assert decoder._file_uri_to_relative(uri) == _reference_relative(root, uri)


@posix_only
@pytest.mark.parametrize("uri", ["file:///usr/include/stdio.h", "/usr/include/x.h"])
def test_paths_outside_the_root_stay_absolute(tmp_path, uri):
    decoder = ClangdGraphDecoder(str(tmp_path / "index"), str(tmp_path))
    assert decoder._file_uri_to_relative(uri) == _reference_relative(
        decoder.project_root, uri
    )


def test_filesystem_root_project(tmp_path):
    decoder = ClangdGraphDecoder(str(tmp_path / "index"), "/")
    uri = "file:///usr/include/stdio.h"
    assert decoder._file_uri_to_relative(uri) == _reference_relative(Path("/"), uri)


def test_non_posix_platform_uses_pathlib(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setattr(clangd_decode.os, "sep", "\\")
    decoder = ClangdGraphDecoder(str(tmp_path / "index"), str(root))
    assert decoder._project_root_prefix is None
    uri = (root / "src" / "a.c").as_uri()
    assert decoder._file_uri_to_relative(uri) == _reference_relative(root, uri)


def test_gc_pause_restores_collection_and_nests():
    assert gc.isenabled()
    with _gc_paused():
        assert not gc.isenabled()
        with _gc_paused():
            assert not gc.isenabled()
        assert not gc.isenabled()
    assert gc.isenabled()

    with pytest.raises(RuntimeError):
        with _gc_paused():
            raise RuntimeError("boom")
    assert gc.isenabled()


def test_gc_pause_leaves_a_disabled_collector_disabled():
    gc.disable()
    try:
        with _gc_paused():
            pass
        assert not gc.isenabled()
    finally:
        gc.enable()


def test_decode_reenables_collection(tmp_path, monkeypatch):
    decoder = ClangdGraphDecoder(str(tmp_path / "index"), str(tmp_path))
    observed = []
    monkeypatch.setattr(
        decoder, "_collect_all_idx", lambda: observed.append(gc.isenabled())
    )
    decoder.decode()
    assert observed == [False]
    assert gc.isenabled()
