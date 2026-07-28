# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Pre-rename index artifacts must stay readable without a reindex."""

import io
import pickle

import pytest

from codenib import compat_pickle
from codenib.types import NodeInfo

_FORMER_ROOT = "code" + "miner"


def test_rewrite_module_maps_former_root():
    assert compat_pickle.rewrite_module(_FORMER_ROOT) == "codenib"
    assert (
        compat_pickle.rewrite_module(f"{_FORMER_ROOT}.index.embedding.vector_store")
        == "codenib.index.embedding.vector_store"
    )


def test_rewrite_module_leaves_other_modules_alone():
    # Note the third entry: a module merely *prefixed* by the former root must
    # not be rewritten, or the mapping would corrupt unrelated package names.
    for module in ("codenib.types", "builtins", f"{_FORMER_ROOT}als.x"):
        assert compat_pickle.rewrite_module(module) == module


def test_find_class_resolves_former_namespace():
    unpickler = compat_pickle.RenamingUnpickler(io.BytesIO(b""))

    assert unpickler.find_class(f"{_FORMER_ROOT}.types", "NodeInfo") is NodeInfo

    # The same lookup through a stock unpickler is exactly what fails on disk.
    with pytest.raises(ModuleNotFoundError):
        pickle.Unpickler(io.BytesIO(b"")).find_class(
            f"{_FORMER_ROOT}.types", "NodeInfo"
        )


def test_load_reads_current_namespace_pickles_unchanged():
    payload = {"a": 1, "b": [2, 3], "c": NodeInfo(node_name="f()", start_line=7)}

    restored = compat_pickle.load(io.BytesIO(pickle.dumps(payload)))

    assert restored["a"] == 1
    assert restored["b"] == [2, 3]
    assert restored["c"].node_name == "f()"
    assert restored["c"].start_line == 7
