# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""One BM25 portable-artifact publication harness for H1."""

from __future__ import annotations

import os
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from codenib._atomic_directory import (
    PublicationDirectoryReader,
    directory_ownership_file_records,
    lexical_directory_path,
    reopen_authenticated_directory,
)
from codenib.artifacts.archive import (
    DEFAULT_MAX_ARCHIVE_BYTES,
    extract_context_artifact_archive,
)
from codenib.artifacts.context import CONTEXT_ARTIFACT_MANIFEST
from codenib.artifacts.runtime import VerifiedContextArtifact, verify_context_artifact

from .cas import BlobInfo, LocalCAS
from .catalog import SQLiteCatalog
from .contracts import Generation, ResolvedSnapshot, Snapshot, StorageIntegrityError

_COPY_BYTES = 1024 * 1024
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class PublicationResult:
    repository: str
    ref_name: str
    ref_revision: int
    snapshot_id: str
    generation_id: str
    archive_digest: str
    archive_size: int


def _records(artifact: VerifiedContextArtifact) -> tuple[object, ...]:
    return directory_ownership_file_records(artifact.ownership)  # type: ignore[arg-type]


def _metadata_digest(artifact: VerifiedContextArtifact) -> str:
    for record in _records(artifact):
        if record.path == CONTEXT_ARTIFACT_MANIFEST:
            return record.sha256
    raise StorageIntegrityError("portable artifact metadata record is missing")


def _zip_info(path: str, *, size: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=_ZIP_TIME)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.file_size = size
    return info


def _write_deterministic_archive(
    artifact: VerifiedContextArtifact,
    destination: Path,
) -> None:
    """Write the exact verified tree as a deterministic ZIP_STORED file."""

    records = _records(artifact)
    if not records:
        raise StorageIntegrityError("portable artifact contains no files")
    total_size = sum(record.size for record in records)
    if total_size > DEFAULT_MAX_ARCHIVE_BYTES:
        raise ValueError(
            "H1 portable artifact exceeds its "
            f"{DEFAULT_MAX_ARCHIVE_BYTES}-byte archive limit"
        )

    with destination.open("w+b") as raw_archive:
        with zipfile.ZipFile(
            raw_archive,
            mode="w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
            strict_timestamps=True,
        ) as archive:
            archive.comment = b""

            def write(publication: PublicationDirectoryReader) -> None:
                for record in records:
                    relative = PurePosixPath(record.path)
                    with publication.open_authenticated_file(
                        relative,
                        max_bytes=record.size,
                    ) as source:
                        with archive.open(
                            _zip_info(record.path, size=record.size),
                            mode="w",
                            force_zip64=record.size >= zipfile.ZIP64_LIMIT,
                        ) as target:
                            written = 0
                            for block in source.iter_bytes(chunk_size=_COPY_BYTES):
                                target.write(block)
                                written += len(block)
                    if written != record.size or source.record != record:
                        raise StorageIntegrityError(
                            f"portable artifact changed while packing: {record.path}"
                        )
                if publication.capture_ownership() != artifact.ownership:
                    raise StorageIntegrityError(
                        "portable artifact changed while packing its archive"
                    )

            reopen_authenticated_directory(
                artifact.root,
                artifact.ownership,  # type: ignore[arg-type]
                write,
            )
        raw_archive.flush()
        os.fsync(raw_archive.fileno())
    if destination.stat().st_size > DEFAULT_MAX_ARCHIVE_BYTES:
        raise ValueError(
            "H1 portable artifact archive exceeds its "
            f"{DEFAULT_MAX_ARCHIVE_BYTES}-byte limit"
        )


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


class IndexRepository:
    """Compose the one H1 SQLite catalog and local CAS implementation.

    Constructor composition is the experimental harness seam; H1 deliberately
    has no backend registry or public protocol hierarchy.
    """

    def __init__(
        self,
        *,
        root: Path,
        catalog: SQLiteCatalog,
        objects: LocalCAS,
    ) -> None:
        self.root = root
        self.catalog = catalog
        self.objects = objects

    @classmethod
    def open(cls, root: str | os.PathLike[str]) -> "IndexRepository":
        candidate = Path(root).expanduser().resolve(strict=False)
        candidate.mkdir(parents=True, exist_ok=True)
        if not candidate.is_dir():
            raise ValueError("H1 repository root must be a directory")
        # Validate/provision the data plane before creating catalog state.
        objects = LocalCAS(candidate / "objects")
        catalog = SQLiteCatalog(candidate / "catalog.sqlite3")
        return cls(root=candidate, catalog=catalog, objects=objects)

    def _preflight_archive(
        self,
        blob: BlobInfo,
        artifact: VerifiedContextArtifact,
    ) -> None:
        archive = self.objects.verified_path(
            blob.digest,
            expected_size=blob.byte_size,
        )
        with tempfile.TemporaryDirectory(prefix="codenib-h1-preflight-") as temporary:
            extracted = extract_context_artifact_archive(
                archive,
                Path(temporary) / "artifact",
                expected_repository=artifact.repository,
                expected_commit=artifact.commit,
                max_archive_bytes=DEFAULT_MAX_ARCHIVE_BYTES,
            )
            if (
                extracted.source_fingerprint != artifact.source_fingerprint
                or extracted.views != artifact.views
                or _metadata_digest(extracted) != _metadata_digest(artifact)
            ):
                raise StorageIntegrityError(
                    "CAS archive preflight differs from the portable artifact"
                )

    def publish_bm25(
        self,
        artifact_root: str | os.PathLike[str],
        *,
        ref_name: str = "main",
        expected_revision: int = 0,
    ) -> PublicationResult:
        """Store one verified BM25 artifact, then atomically move its ref."""

        artifact = verify_context_artifact(artifact_root)
        if artifact.views != ("bm25",):
            raise ValueError("H1 accepts exactly one portable BM25 view")
        with tempfile.TemporaryDirectory(prefix="codenib-h1-pack-") as temporary:
            archive = Path(temporary) / "context.zip"
            _write_deterministic_archive(artifact, archive)
            blob = self.objects.put_file(archive)
        self._preflight_archive(blob, artifact)

        generation = Generation.create(
            repository=artifact.repository,
            commit=artifact.commit,
            source_fingerprint=artifact.source_fingerprint,
            metadata_digest=_metadata_digest(artifact),
            archive_digest=blob.digest,
            archive_size=blob.byte_size,
            file_count=artifact.file_count,
            byte_count=artifact.byte_count,
        )
        snapshot = Snapshot.create(generation)
        head = self.catalog.publish_snapshot(
            snapshot,
            ref_name=ref_name,
            expected_revision=expected_revision,
        )
        return PublicationResult(
            repository=head.repository,
            ref_name=head.ref_name,
            ref_revision=head.revision,
            snapshot_id=head.snapshot_id,
            generation_id=generation.generation_id,
            archive_digest=blob.digest,
            archive_size=blob.byte_size,
        )

    def resolve_ref(
        self,
        repository: str,
        ref_name: str = "main",
    ) -> ResolvedSnapshot:
        """Resolve catalog metadata once; materialization verifies CAS closure."""

        return self.catalog.resolve_ref(repository, ref_name)

    def materialize_snapshot(
        self,
        snapshot_id: str,
        destination: str | os.PathLike[str],
    ) -> VerifiedContextArtifact:
        """Export a pinned snapshot as an ordinary portable context artifact."""

        closure = self.catalog.get_snapshot(snapshot_id)
        generation = closure.snapshot.generations[0]
        output = lexical_directory_path(Path(destination))
        if _paths_overlap(self.root, output):
            raise ValueError("materialized artifact must be outside the H1 store")
        if output.exists() or output.is_symlink():
            raise FileExistsError(
                f"snapshot materialization destination already exists: {output}"
            )
        archive = self.objects.verified_path(
            generation.archive_digest,
            expected_size=generation.archive_size,
        )
        artifact = extract_context_artifact_archive(
            archive,
            output,
            expected_repository=closure.snapshot.repository,
            expected_commit=closure.snapshot.commit,
            max_archive_bytes=DEFAULT_MAX_ARCHIVE_BYTES,
        )
        if (
            artifact.source_fingerprint != closure.snapshot.source_fingerprint
            or artifact.views != (generation.view_type,)
            or _metadata_digest(artifact) != generation.metadata_digest
            or artifact.file_count != generation.file_count
            or artifact.byte_count != generation.byte_count
        ):
            raise StorageIntegrityError(
                "materialized artifact differs from its generation identity"
            )
        return artifact


__all__ = [
    "IndexRepository",
    "PublicationResult",
    "_write_deterministic_archive",
]
