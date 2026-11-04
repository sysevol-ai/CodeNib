# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-platform URI to relative path decoding tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Allow decoding tests to run in lightweight environments without C-extension igraph
if "igraph" not in sys.modules:
    sys.modules["igraph"] = MagicMock()

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


def test_uri_to_relpath_windows():
    root = "C:/Users/project"
    uri = "file:///C:/Users/project/codenib/graph.py"
    if os.name == "nt":
        assert uri_to_relpath(uri, root) == "codenib/graph.py"
        assert decode_uri_to_relpath(uri, Path(root)) == "codenib/graph.py"
        assert live_uri_to_relpath(uri, Path(root)) == "codenib/graph.py"
    else:
        assert uri_to_relpath(uri, root) is not None


def test_uri_to_relpath_encoded_spaces():
    root = "/workspace/repo"
    uri = "file:///workspace/repo/path%20with%20spaces/module.py"
    assert uri_to_relpath(uri, root) == "path with spaces/module.py"
    assert decode_uri_to_relpath(uri, Path(root)) == "path with spaces/module.py"
    assert live_uri_to_relpath(uri, Path(root)) == "path with spaces/module.py"


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
