# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Incremental overlay and clean-rebuild convergence gates for semantic facts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Sequence

from .model import FactBatch


def _canonical_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("file path must be a repository-relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("file path must be a repository-relative POSIX path")
    return value


@dataclass(frozen=True, slots=True)
class FactOverlayChange:
    generation: int
    upserted: tuple[str, ...]
    deleted: tuple[str, ...]


class FactOverlay:
    """Atomic in-memory model of file-unit replacement semantics."""

    def __init__(self, batches: Sequence[FactBatch] = ()) -> None:
        self._generation = 0
        self._batches = _batch_map(batches, label="initial overlay")

    @property
    def generation(self) -> int:
        return self._generation

    def snapshot(self) -> tuple[FactBatch, ...]:
        return tuple(self._batches[path] for path in sorted(self._batches))

    def apply(
        self,
        *,
        upserts: Sequence[FactBatch] = (),
        deletes: Iterable[str] = (),
    ) -> FactOverlayChange:
        incoming = _batch_map(upserts, label="overlay upsert")
        deleted = {_canonical_path(path) for path in deletes}
        overlap = sorted(set(incoming) & deleted)
        if overlap:
            raise ValueError(
                f"one overlay generation cannot upsert and delete the same files: {overlap}"
            )

        updated = dict(self._batches)
        for path in deleted:
            updated.pop(path, None)
        updated.update(incoming)
        self._batches = updated
        self._generation += 1
        return FactOverlayChange(
            generation=self._generation,
            upserted=tuple(sorted(incoming)),
            deleted=tuple(sorted(deleted)),
        )


@dataclass(frozen=True, slots=True)
class ChangedFactFile:
    file_path: str
    incremental_digest: str
    clean_rebuild_digest: str
    incremental_content_digest: str
    clean_rebuild_content_digest: str

    def to_dict(self) -> dict[str, str]:
        return {
            "file_path": self.file_path,
            "incremental_digest": self.incremental_digest,
            "clean_rebuild_digest": self.clean_rebuild_digest,
            "incremental_content_digest": self.incremental_content_digest,
            "clean_rebuild_content_digest": self.clean_rebuild_content_digest,
        }


@dataclass(frozen=True, slots=True)
class FactConvergenceReport:
    missing_files: tuple[str, ...]
    extra_files: tuple[str, ...]
    changed_files: tuple[ChangedFactFile, ...]
    strict: bool = False

    @property
    def converged(self) -> bool:
        return not (self.missing_files or self.extra_files or self.changed_files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "converged": self.converged,
            "strict": self.strict,
            "missing_files": list(self.missing_files),
            "extra_files": list(self.extra_files),
            "changed_files": [item.to_dict() for item in self.changed_files],
        }

    def require_converged(self) -> None:
        if self.converged:
            return
        raise FactConvergenceError(self)


class FactConvergenceError(AssertionError):
    def __init__(self, report: FactConvergenceReport) -> None:
        self.report = report
        super().__init__(
            "incremental facts did not converge with a clean rebuild: "
            f"missing={list(report.missing_files)}, "
            f"extra={list(report.extra_files)}, "
            f"changed={[item.file_path for item in report.changed_files]}"
        )


def _batch_map(
    batches: Sequence[FactBatch],
    *,
    label: str,
) -> dict[str, FactBatch]:
    result: dict[str, FactBatch] = {}
    for batch in batches:
        if not isinstance(batch, FactBatch):
            raise TypeError(f"{label} contains a non-FactBatch value")
        if batch.file_path in result:
            raise ValueError(f"{label} contains duplicate file {batch.file_path!r}")
        result[batch.file_path] = batch
    return result


def compare_fact_snapshots(
    incremental: Sequence[FactBatch],
    clean_rebuild: Sequence[FactBatch],
    *,
    strict: bool = False,
) -> FactConvergenceReport:
    """Compare file facts after an update sequence with a clean rebuild.

    Semantic mode ignores provider/profile route identities while retaining
    source content identity, completeness, capabilities, diagnostics, ranges,
    edges, provenance, confidence, and resolver labels. Strict mode compares
    the complete batch, including provider and profile digests.
    """

    actual = _batch_map(incremental, label="incremental snapshot")
    expected = _batch_map(clean_rebuild, label="clean rebuild snapshot")
    missing = tuple(sorted(set(expected) - set(actual)))
    extra = tuple(sorted(set(actual) - set(expected)))
    changed = []
    for file_path in sorted(set(actual) & set(expected)):
        incremental_batch = actual[file_path]
        clean_batch = expected[file_path]
        incremental_digest = (
            incremental_batch.digest()
            if strict
            else incremental_batch.semantic_digest()
        )
        clean_digest = clean_batch.digest() if strict else clean_batch.semantic_digest()
        if incremental_digest == clean_digest:
            continue
        changed.append(
            ChangedFactFile(
                file_path=file_path,
                incremental_digest=incremental_digest,
                clean_rebuild_digest=clean_digest,
                incremental_content_digest=incremental_batch.content_digest,
                clean_rebuild_content_digest=clean_batch.content_digest,
            )
        )
    return FactConvergenceReport(missing, extra, tuple(changed), strict=strict)


__all__ = [
    "ChangedFactFile",
    "FactConvergenceError",
    "FactConvergenceReport",
    "FactOverlay",
    "FactOverlayChange",
    "compare_fact_snapshots",
]
