# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-platform URI to relative path decoding tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codenib.eval.agent_runner.live_lsp_provider import (
    _uri_to_relpath as live_uri_to_relpath,
)
from codenib.graph.incremental.lsp_client import uri_to_relpath
from codenib.ls_index.lsp_graph_decode import _uri_to_relpath as decode_uri_to_relpath


def test_uri_to_relpath_posix():
    root = "/workspace/repo"
    uri = "file:///workspace/repo/codenib/graph.py"
    assert uri_to_relpath(uri, root) == "codenib/graph.py"
    assert decode_uri_to_relpath(uri, Path(root)) == "codenib/graph.py"
    assert live_uri_to_relpath(uri, Path(root)) == "codenib/graph.py"


@pytest.mark.skipif(os.name != "nt", reason="Windows drive-root conversion")
def test_uri_to_relpath_windows():
    root = "C:/Users/project"
    uri = "file:///C:/Users/project/codenib/graph.py"
    assert uri_to_relpath(uri, root) == "codenib/graph.py"
    assert decode_uri_to_relpath(uri, Path(root)) == "codenib/graph.py"
    assert live_uri_to_relpath(uri, Path(root)) == "codenib/graph.py"


def test_uri_to_relpath_encoded_spaces():
    root = "/workspace/repo"
    uri = "file:///workspace/repo/path%20with%20spaces/module.py"
    assert uri_to_relpath(uri, root) == "path with spaces/module.py"
    assert decode_uri_to_relpath(uri, Path(root)) == "path with spaces/module.py"
    assert live_uri_to_relpath(uri, Path(root)) == "path with spaces/module.py"


@pytest.mark.parametrize(
    "relative_path",
    [
        "module.py",
        "path with spaces/module.py",
        "name%20literal.py",
        "name%2Fliteral.py",
        "name%25literal.py",
        "name%2520literal.py",
        "hash#literal.py",
        "café.py",
    ],
)
def test_uri_to_relpath_round_trip(tmp_path: Path, relative_path: str) -> None:
    source = tmp_path / relative_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")

    uri = source.as_uri()

    assert uri_to_relpath(uri, str(tmp_path)) == relative_path
    assert decode_uri_to_relpath(uri, tmp_path) == relative_path
    assert live_uri_to_relpath(uri, tmp_path) == relative_path


def test_uri_to_relpath_non_file_and_empty():
    assert uri_to_relpath("http://example.com/foo.py", "/workspace/repo") is None
    assert (
        decode_uri_to_relpath("http://example.com/foo.py", Path("/workspace/repo"))
        is None
    )
    assert (
        live_uri_to_relpath("http://example.com/foo.py", Path("/workspace/repo"))
        is None
    )

    assert uri_to_relpath("", "/workspace/repo") is None
    assert decode_uri_to_relpath("", Path("/workspace/repo")) is None
    assert live_uri_to_relpath("", Path("/workspace/repo")) is None


def test_uri_to_relpath_keeps_a_symlinked_directory_outside_the_root(tmp_path):
    root = tmp_path / "repo"
    outside = tmp_path / "shared"
    outside.mkdir()
    (outside / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    root.mkdir()
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    uri = (root / "linked" / "module.py").as_uri()

    assert uri_to_relpath(uri, str(root)) == "linked/module.py"


def test_uri_to_relpath_matches_a_symlinked_root(tmp_path):
    real = tmp_path / "real"
    (real / "pkg").mkdir(parents=True)
    (real / "pkg" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    uri = (real / "pkg" / "module.py").as_uri()

    assert uri_to_relpath(uri, str(alias)) == "pkg/module.py"


def test_uri_to_relpath_normalizes_parent_segments(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    uri = f"{tmp_path.as_uri()}/other/../pkg/module.py"

    assert uri_to_relpath(uri, str(tmp_path)) == "pkg/module.py"
