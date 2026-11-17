# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The per-file record that lets an older graph name what changed since."""

from __future__ import annotations

import pytest

from codenib.compiler.index_builders import IndexBuilderRegistry
from codenib.compiler.index_compiler import IndexCompiler, IndexCompilerConfig
from codenib.compiler.source_inventory import (
    SOURCE_INVENTORY_DIR,
    SOURCE_INVENTORY_METADATA_KEY,
    load_source_inventory,
    source_delta,
    write_source_inventory,
)

from .test_index_compiler import _mock_builder

FINGERPRINT = "v2:sha256:" + "a" * 64


def test_inventory_round_trips_only_through_its_receipt(tmp_path):
    files = {"pkg/a.py": "1" * 64, "pkg/b.py": "2" * 64}
    receipt = write_source_inventory(
        tmp_path, source_fingerprint=FINGERPRINT, file_digests=files
    )

    assert receipt["file_count"] == 2
    assert (
        load_source_inventory(tmp_path, receipt, source_fingerprint=FINGERPRINT)
        == files
    )
    with pytest.raises(ValueError):
        load_source_inventory(tmp_path, None, source_fingerprint=FINGERPRINT)
    with pytest.raises(ValueError):
        load_source_inventory(tmp_path, receipt, source_fingerprint="v2:other")
    with pytest.raises(ValueError):
        load_source_inventory(
            tmp_path,
            {**receipt, "sha256": "../../etc/passwd"},
            source_fingerprint=FINGERPRINT,
        )

    stored = tmp_path / SOURCE_INVENTORY_DIR / f"{receipt['sha256']}.json"
    stored.write_bytes(stored.read_bytes().replace(b"pkg/a.py", b"pkg/z.py"))
    with pytest.raises(ValueError):
        load_source_inventory(tmp_path, receipt, source_fingerprint=FINGERPRINT)


def test_delta_names_modified_added_and_deleted_paths():
    delta = source_delta(
        {"same.py": "1", "edited.py": "2", "removed.py": "3"},
        {"same.py": "1", "edited.py": "9", "created.py": "4"},
    )

    assert delta.modified == ("edited.py",)
    assert delta.added == ("created.py",)
    assert delta.deleted == ("removed.py",)


def test_compiler_keeps_the_graph_inventory_after_the_source_moves_on(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    (repo / "kept.py").write_text("def kept():\n    return 0\n")
    (repo / "edited.py").write_text("def before():\n    return 1\n")
    cache = tmp_path / "indexes"
    registry = IndexBuilderRegistry()
    registry.register("bm25", _mock_builder("bm25"))
    registry.register("symbol_graph", _mock_builder("symbol_graph"))
    compiler = IndexCompiler(
        registry,
        IndexCompilerConfig(index_types=["bm25", "symbol_graph"], languages=["python"]),
    )

    built = compiler.compile_repo(str(repo), cache_dir=str(cache))
    graph = built.indexes["symbol_graph"]
    receipt = graph.metadata[SOURCE_INVENTORY_METADATA_KEY]
    indexed = load_source_inventory(
        cache, receipt, source_fingerprint=graph.source_fingerprint
    )
    assert sorted(indexed) == ["edited.py", "kept.py"]
    assert built.lagging_index_entry("symbol_graph") is None

    (repo / "edited.py").write_text("def after():\n    return 2\n")
    (repo / "created.py").write_text("def created():\n    return 3\n")
    refreshed = compiler.update_repo(
        str(repo), index_types=["bm25"], cache_dir=str(cache)
    )

    stale = refreshed.indexes["symbol_graph"]
    assert not refreshed.index_is_current("symbol_graph")
    assert refreshed.lagging_index_entry("symbol_graph") is stale
    assert stale.metadata[SOURCE_INVENTORY_METADATA_KEY] == receipt
    # The inventory still describes the graph's source, not the new one.
    assert (
        load_source_inventory(
            cache, receipt, source_fingerprint=stale.source_fingerprint
        )
        == indexed
    )
    assert refreshed.source_fingerprint != stale.source_fingerprint
