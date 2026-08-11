# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for file-unit overlay and clean-rebuild convergence gates."""

from __future__ import annotations

from dataclasses import replace

import pytest

from codenib.facts import (
    CAPABILITY_DEFINITIONS,
    COMPLETENESS_PARTIAL,
    FactBatch,
    FactConvergenceError,
    FactOverlay,
    SourceRange,
    SymbolFact,
    compare_fact_snapshots,
    sha256_digest,
)
from codenib.types import NODE_TYPE_FUNCTION


def _batch(file_path: str, revision: str) -> FactBatch:
    symbol = SymbolFact(
        symbol_id=f"{file_path}:run",
        moniker=f"{file_path}:run()",
        display_name="run",
        kind=NODE_TYPE_FUNCTION,
        definition_range=SourceRange(1, 0, 2, 0),
    )
    return FactBatch(
        file_path=file_path,
        language="python",
        content_digest=sha256_digest(f"{file_path}:{revision}"),
        profile_digest=sha256_digest("incremental-profile"),
        provider="incremental",
        completeness=COMPLETENESS_PARTIAL,
        capabilities=(CAPABILITY_DEFINITIONS,),
        symbols=(symbol,),
    )


def test_file_overlay_update_delete_sequence_converges_with_clean_rebuild() -> None:
    first_a = _batch("src/a.py", "v1")
    first_b = _batch("src/b.py", "v1")
    second_a = _batch("src/a.py", "v2")
    added_c = _batch("src/c.py", "v1")
    overlay = FactOverlay((first_a, first_b))

    change = overlay.apply(upserts=(second_a, added_c), deletes=("src/b.py",))
    clean = (
        replace(
            second_a,
            provider="clean-rebuild",
            profile_digest=sha256_digest("clean-profile"),
        ),
        replace(
            added_c,
            provider="clean-rebuild",
            profile_digest=sha256_digest("clean-profile"),
        ),
    )
    report = compare_fact_snapshots(overlay.snapshot(), clean)

    assert change.generation == 1
    assert change.upserted == ("src/a.py", "src/c.py")
    assert change.deleted == ("src/b.py",)
    assert overlay.generation == 1
    assert report.converged is True
    assert report.to_dict() == {
        "converged": True,
        "strict": False,
        "missing_files": [],
        "extra_files": [],
        "changed_files": [],
    }
    report.require_converged()

    strict = compare_fact_snapshots(overlay.snapshot(), clean, strict=True)
    assert strict.converged is False
    assert [item.file_path for item in strict.changed_files] == [
        "src/a.py",
        "src/c.py",
    ]


def test_convergence_report_separates_missing_extra_and_changed_files() -> None:
    incremental = (
        _batch("src/changed.py", "stale"),
        _batch("src/extra.py", "v1"),
    )
    clean = (
        _batch("src/changed.py", "current"),
        _batch("src/missing.py", "v1"),
    )

    report = compare_fact_snapshots(incremental, clean)

    assert report.converged is False
    assert report.missing_files == ("src/missing.py",)
    assert report.extra_files == ("src/extra.py",)
    assert [item.file_path for item in report.changed_files] == ["src/changed.py"]
    changed = report.changed_files[0]
    assert changed.incremental_content_digest != changed.clean_rebuild_content_digest
    with pytest.raises(FactConvergenceError, match="did not converge") as error:
        report.require_converged()
    assert error.value.report is report


def test_overlay_rejects_ambiguous_generation_atomically() -> None:
    original = _batch("src/a.py", "v1")
    replacement = _batch("src/a.py", "v2")
    overlay = FactOverlay((original,))

    with pytest.raises(ValueError, match="upsert and delete the same files"):
        overlay.apply(upserts=(replacement,), deletes=("src/a.py",))

    assert overlay.generation == 0
    assert overlay.snapshot() == (original,)


def test_snapshot_rejects_duplicate_file_units() -> None:
    batch = _batch("src/a.py", "v1")
    with pytest.raises(ValueError, match="duplicate file"):
        compare_fact_snapshots((batch, batch), (batch,))
