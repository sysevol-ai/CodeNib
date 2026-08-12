# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic, bounded bundles for immutable materialized index views."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import stat
import struct
import sys
import tempfile
import unicodedata
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Mapping, TypeVar

from .._atomic_directory import (
    DirectoryOrphan,
    PublicationDirectoryReader,
    TreeFileRecord,
    _annotate_secondary_error,
    _directory_identity,
    _linux_mount_points,
    _OrderedAction,
    _path_is_mount_point,
    _run_callback_with_post_validations,
    _run_context_with_cleanup_actions,
    _TreeOwnership,
    _validate_directory_ownership_token,
    capture_directory_ownership,
    directory_ownership_entry_identities,
    directory_ownership_file_records,
    directory_ownership_inventory,
    directory_ownership_root_identity,
    discard_owned_directory,
    project_directory_ownership_subtree,
    publish_staged_directory,
)
from .models import (
    ArtifactMember,
    StorageIntegrityError,
    StorageValidationError,
    canonical_json,
    normalize_digest,
)

VIEW_BUNDLE_SCHEMA = "codenib.view-bundle.v1"
VIEW_BUNDLE_MANIFEST = "bundle.json"
VIEW_BUNDLE_PAYLOAD = "payload"
VIEW_BUNDLE_MEDIA_TYPE = "application/vnd.codenib.view-bundle.v1+zip"
logger = logging.getLogger(__name__)


def _record_publication_orphan(
    orphan: DirectoryOrphan | None,
    *,
    operation: str,
) -> None:
    if orphan is None:
        return
    logger.warning(
        "View-bundle %s retained an orphan for quiescent GC: "
        "path=%s digest=%s entries=%d bytes=%d verified=%s",
        operation,
        orphan.path,
        orphan.ownership_digest,
        orphan.entries,
        orphan.byte_count,
        orphan.verified_at_isolation,
    )


DEFAULT_MAX_BUNDLE_FILES = 100_000
DEFAULT_MAX_BUNDLE_BYTES = 64 * 1024 * 1024 * 1024
DEFAULT_MAX_BUNDLE_METADATA_BYTES = 16 * 1024 * 1024

_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_MEMBER_PATH_BYTES = 4096
_MAX_MEMBER_DEPTH = 256
_MAX_MEMBER_COMPONENT_BYTES = 255
_MAX_ZIP_MEMBER_NAME_BYTES = _MAX_MEMBER_PATH_BYTES + len("payload/")
_ZIP_GUARD_EXTRA = struct.pack("<HH16s", 0x434E, 16, b"CodeNibBundleV1!")
_MAX_ZIP_CENTRAL_EXTRA_BYTES = 28 + len(_ZIP_GUARD_EXTRA)
_MAX_ZIP_ENTRY_OVERHEAD = 2 * _MAX_ZIP_MEMBER_NAME_BYTES + 256
_MAX_ZIP_ENVELOPE_BYTES = 1024 * 1024
_ZIP64_LIMIT = (1 << 31) - 1
_ZIP_FILECOUNT_LIMIT = (1 << 16) - 1
_BUNDLE_STREAM_CLOSE_RECOVERY_LIMIT = 64
_BUNDLE_STREAM_REVOKE_RECOVERY_LIMIT = 64
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_VIEW_TYPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_WINDOWS_SUPERSCRIPT_DIGITS = str.maketrans({"¹": "1", "²": "2", "³": "3"})
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_SAFE_DIRECTORY_FDS = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "fchmod")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.scandir in os.supports_fd
)
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class ViewBundleRecord:
    """Identity and verified inventory of one view-bundle object."""

    path: Path
    view_type: str
    digest: str
    byte_size: int
    members: tuple[ArtifactMember, ...]


@dataclass(frozen=True, slots=True)
class _PlannedZipMember:
    """One fully laid-out canonical ZIP member."""

    archive_name: str
    encoded_name: bytes
    source_relative: PurePosixPath | None
    inline_payload: bytes | None
    mode: int
    byte_size: int
    crc32: int
    flags: int
    header_offset: int
    local_extra: bytes
    central_extra: bytes
    local_version: int
    central_version: int


@dataclass(frozen=True, slots=True)
class _PlannedViewBundleReceipt:
    """Private binding between public plan fields and reader authority."""

    publication: PublicationDirectoryReader = dataclass_field(
        repr=False,
        compare=False,
    )
    prefix: PurePosixPath
    outer_ownership: object
    source_ownership: object
    view_type: str
    digest: str
    byte_size: int
    members: tuple[ArtifactMember, ...]
    manifest_bytes: bytes
    zip_members: tuple[_PlannedZipMember, ...]
    central_offset: int
    central_size: int
    envelope_bytes: bytes


@dataclass(frozen=True, slots=True)
class PlannedViewBundle:
    """Canonical bundle bytes bound to one exact publication subtree.

    The ownership values are identity tokens, not path locators.  Bytes may be
    consumed only through :func:`consume_planned_view_bundle` while a matching
    :class:`PublicationDirectoryReader` callback is active.
    """

    view_type: str
    digest: str
    byte_size: int
    members: tuple[ArtifactMember, ...]
    source_ownership: object = dataclass_field(repr=False, compare=False)
    _receipt: _PlannedViewBundleReceipt = dataclass_field(
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class MaterializedViewBundle:
    """A verified view bundle published as a local materialized directory."""

    output_dir: Path
    payload_dir: Path
    ownership: object = dataclass_field(repr=False, compare=False)
    view_type: str
    digest: str
    byte_size: int
    members: tuple[ArtifactMember, ...]


@dataclass(frozen=True, slots=True)
class _ScannedFile:
    path: str
    byte_size: int
    mode: int
    signature: tuple[int, ...]
    digest: str | None


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    root: Path
    root_signature: tuple[int, ...]
    directories: Mapping[tuple[str, ...], tuple[int, ...]]
    files: tuple[_ScannedFile, ...]


@dataclass(frozen=True, slots=True)
class _ZipEnvelope:
    member_count: int
    central_size: int
    central_offset: int
    zip64: bool


@dataclass(frozen=True, slots=True)
class _MemberPathTrie:
    edges: Mapping[tuple[int, str], tuple[int, str]]
    directory_nodes: frozenset[int]


@dataclass(frozen=True, slots=True)
class _MaterializationOwnership:
    state: str
    root_identity: tuple[int, ...] | None
    manifest_digest: str | None
    members: tuple[ArtifactMember, ...]
    directory_modes: tuple[tuple[str, int], ...]


class _CanonicalZipInfo(zipfile.ZipInfo):
    """ZipInfo whose local ZIP64 field precedes the v1 guard field."""

    def FileHeader(self, zip64: bool | None = None) -> bytes:  # noqa: N802
        modified_time = (
            self.date_time[3] << 11 | self.date_time[4] << 5 | (self.date_time[5] // 2)
        )
        modified_date = (
            (self.date_time[0] - 1980) << 9 | self.date_time[1] << 5 | self.date_time[2]
        )
        if self.flag_bits & 0x08:
            crc = compressed_size = file_size = 0
        else:
            crc = self.CRC
            compressed_size = self.compress_size
            file_size = self.file_size
        if zip64 is None:
            zip64 = (
                file_size > zipfile.ZIP64_LIMIT or compressed_size > zipfile.ZIP64_LIMIT
            )
        extra = self.extra
        if zip64:
            extra = struct.pack("<HHQQ", 1, 16, file_size, compressed_size) + extra
            compressed_size = file_size = 0xFFFFFFFF
            self.extract_version = max(self.extract_version, 45)
            self.create_version = max(self.create_version, 45)
        encoded_name, flags = self._encodeFilenameFlags()
        header = struct.pack(
            "<4s2B4HL2L2H",
            b"PK\x03\x04",
            self.extract_version,
            self.reserved,
            flags,
            self.compress_type,
            modified_time,
            modified_date,
            crc,
            compressed_size,
            file_size,
            len(encoded_name),
            len(extra),
        )
        return header + encoded_name + extra


def build_view_bundle(
    source_dir: str | Path,
    destination: str | Path,
    *,
    view_type: str,
    max_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    forbidden_roots: Iterable[str | Path] = (),
) -> ViewBundleRecord:
    """Build one canonical ZIP_STORED bundle from a stable directory tree."""

    view_type = _validate_view_type(view_type)
    _validate_limits(max_files, max_bytes, max_metadata_bytes)
    forbidden_roots = tuple(forbidden_roots)
    source = _real_directory(source_dir, label="view bundle source")
    output = _output_path(destination)
    _reject_overlap(output, source, label="view bundle output and source")
    _reject_forbidden_roots(output, forbidden_roots, label="view bundle output")
    _validate_bundle_file_target(output)

    snapshot = _scan_source_tree(
        source,
        include_digests=True,
        max_files=max_files,
        max_bytes=max_bytes,
    )
    if not snapshot.files:
        raise StorageValidationError(
            "view bundle payload must contain at least one file"
        )

    members = tuple(
        ArtifactMember(
            path=item.path,
            digest=item.digest or "",
            byte_size=item.byte_size,
            mode=item.mode,
        )
        for item in snapshot.files
    )
    _validate_member_paths(members, max_files=max_files)
    manifest_bytes = _manifest_bytes(view_type, members)
    if len(manifest_bytes) > max_metadata_bytes:
        raise StorageValidationError(
            f"view bundle metadata exceeds {max_metadata_bytes} bytes"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output = _output_path(output)
    _reject_overlap(resolved_output, source, label="view bundle output and source")
    _reject_forbidden_roots(
        resolved_output,
        forbidden_roots,
        label="view bundle output",
    )
    _validate_bundle_file_target(resolved_output)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(resolved_output.parent),
        prefix=f".{resolved_output.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    temporary_identity: tuple[int, ...] | None = None
    try:
        with os.fdopen(descriptor, "w+b") as raw_archive:
            temporary_identity = _file_inode_identity(os.fstat(raw_archive.fileno()))
            with zipfile.ZipFile(
                raw_archive,
                mode="w",
                compression=zipfile.ZIP_STORED,
                allowZip64=True,
                strict_timestamps=True,
            ) as archive:
                archive.writestr(
                    _canonical_zip_info(VIEW_BUNDLE_MANIFEST, 0o644),
                    manifest_bytes,
                    compress_type=zipfile.ZIP_STORED,
                )
                with _open_source_root(snapshot) as root_descriptor:
                    pairs = zip(snapshot.files, members, strict=True)
                    for index, (scanned, member) in enumerate(pairs):
                        info = _canonical_zip_info(
                            f"{VIEW_BUNDLE_PAYLOAD}/{member.path}",
                            member.mode,
                        )
                        if index == len(members) - 1:
                            info.extra = _ZIP_GUARD_EXTRA
                        info.file_size = member.byte_size
                        with archive.open(
                            info,
                            mode="w",
                            force_zip64=member.byte_size * 1.05 > _ZIP64_LIMIT,
                        ) as target:
                            _copy_source_file(
                                snapshot,
                                root_descriptor,
                                scanned,
                                target,
                            )
            raw_archive.flush()
            os.fsync(raw_archive.fileno())
            archive_signature = _path_signature(os.fstat(raw_archive.fileno()))
            raw_archive.seek(0)
            archive_digest, archive_size = _consume_exact_file(
                raw_archive,
                archive_signature[3],
                label="staged view bundle archive",
            )
            raw_archive.seek(0)
            verified, _verified_manifest = _verify_archive_handle(
                raw_archive,
                temporary,
                expected_view_type=view_type,
                archive_digest=archive_digest,
                archive_size=archive_size,
                max_files=max_files,
                max_bytes=max_bytes,
                max_metadata_bytes=max_metadata_bytes,
            )
            _require_source_snapshot(
                snapshot,
                max_files=max_files,
                max_bytes=max_bytes,
            )
            _validate_bundle_file_target(resolved_output)
            _publish_open_bundle_file(
                raw_archive,
                temporary,
                resolved_output,
                expected_signature=archive_signature,
                expected_digest=archive_digest,
                expected_size=archive_size,
            )
            return ViewBundleRecord(
                path=resolved_output,
                view_type=verified.view_type,
                digest=verified.digest,
                byte_size=verified.byte_size,
                members=verified.members,
            )
    finally:
        if temporary_identity is not None:
            _remove_owned_temporary_file(temporary, temporary_identity)


def plan_view_bundle_reader(
    publication: PublicationDirectoryReader,
    prefix: str | PurePosixPath,
    expected_source_ownership: object,
    *,
    view_type: str,
    max_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
) -> PlannedViewBundle:
    """Plan exact canonical bundle bytes from one authenticated subtree.

    Planning performs the complete source read twice: once to compute the CRCs
    committed by ZIP headers and once to hash the resulting canonical archive.
    Both passes use the callback-scoped reader.  The full outer ownership and
    its exact ``prefix`` projection are checked before and after those reads.
    """

    if type(publication) is not PublicationDirectoryReader:
        raise TypeError("view bundle planning requires a publication reader")
    normalized_prefix = _publication_subtree_prefix(prefix)
    normalized_view_type = _validate_view_type(view_type)
    _validate_limits(max_files, max_bytes, max_metadata_bytes)
    _validate_expected_subtree_ownership(expected_source_ownership)

    outer_before = publication.capture_ownership()
    outer_snapshot = _canonical_ownership_snapshot(outer_before)
    _require_publication_subtree_projection(
        outer_before,
        normalized_prefix,
        expected_source_ownership,
    )

    def plan() -> PlannedViewBundle:
        source_inventory = tuple(
            directory_ownership_inventory(expected_source_ownership)  # type: ignore[arg-type]
        )
        source_directories = sum(
            1 for _path, kind in source_inventory if kind == "directory"
        )
        if source_directories > max_files:
            raise StorageValidationError(
                f"view bundle source exceeds {max_files + 1} directories"
            )
        if len(source_inventory) > 2 * max_files + 1:
            raise StorageValidationError("view bundle source has too many entries")
        records = tuple(
            directory_ownership_file_records(expected_source_ownership)  # type: ignore[arg-type]
        )
        if not records:
            raise StorageValidationError(
                "view bundle payload must contain at least one file"
            )
        if len(records) > max_files:
            raise StorageValidationError(
                f"view bundle payload exceeds {max_files} files"
            )
        members = tuple(
            ArtifactMember(
                path=record.path,
                digest=record.sha256,
                byte_size=record.size,
                mode=0o755 if record.mode & 0o111 else 0o644,
            )
            for record in records
        )
        _validate_member_paths(members, max_files=max_files)
        total_bytes = sum(member.byte_size for member in members)
        if total_bytes > max_bytes:
            raise StorageValidationError(
                f"view bundle exceeds {max_bytes} expanded bytes"
            )

        manifest_bytes = _manifest_bytes(normalized_view_type, members)
        if len(manifest_bytes) > max_metadata_bytes:
            raise StorageValidationError(
                f"view bundle metadata exceeds {max_metadata_bytes} bytes"
            )
        payload_crcs = tuple(
            _publication_file_crc32(
                publication,
                normalized_prefix / record.path,
                record,
            )
            for record in records
        )
        receipt = _plan_canonical_zip(
            publication=publication,
            prefix=normalized_prefix,
            outer_ownership=outer_before,
            source_ownership=expected_source_ownership,
            view_type=normalized_view_type,
            members=members,
            manifest_bytes=manifest_bytes,
            payload_crcs=payload_crcs,
        )
        validate_view_bundle_physical_size(
            receipt.byte_size,
            max_files=max_files,
            max_bytes=max_bytes,
            max_metadata_bytes=max_metadata_bytes,
        )
        planning_chunks = _CallbackScopedBundleChunks(
            publication,
            receipt,
            expected_digest=None,
        )

        def hash_planned_bytes() -> None:
            try:
                planning_chunks._drain()
            except BaseException as error:  # noqa: B036 - retain cleanup owner
                planning_chunks._remember_primary(error)
                raise

        _run_bundle_stream_callback(
            hash_planned_bytes,
            planning_chunks,
            (
                (
                    "planned view bundle hashing completeness validation also failed",
                    planning_chunks._require_complete,
                ),
            ),
        )
        receipt = _PlannedViewBundleReceipt(
            publication=receipt.publication,
            prefix=receipt.prefix,
            outer_ownership=receipt.outer_ownership,
            source_ownership=receipt.source_ownership,
            view_type=receipt.view_type,
            digest=planning_chunks._observed_digest(),
            byte_size=receipt.byte_size,
            members=receipt.members,
            manifest_bytes=receipt.manifest_bytes,
            zip_members=receipt.zip_members,
            central_offset=receipt.central_offset,
            central_size=receipt.central_size,
            envelope_bytes=receipt.envelope_bytes,
        )
        return PlannedViewBundle(
            view_type=receipt.view_type,
            digest=receipt.digest,
            byte_size=receipt.byte_size,
            members=receipt.members,
            source_ownership=receipt.source_ownership,
            _receipt=receipt,
        )

    def validate_after_namespace() -> None:
        outer_after = publication.capture_ownership()
        if _canonical_ownership_snapshot(outer_after) != outer_snapshot:
            raise RuntimeError("view bundle publication tree changed while planning")
        _require_publication_subtree_projection(
            outer_after,
            normalized_prefix,
            expected_source_ownership,
        )

    return _run_callback_with_post_validations(
        plan,
        (
            (
                "view bundle planning namespace validation also failed",
                validate_after_namespace,
            ),
            (
                "view bundle planning reader validity validation also failed",
                publication._require_valid,
            ),
        ),
    )


def consume_planned_view_bundle(
    publication: PublicationDirectoryReader,
    plan: PlannedViewBundle,
    consumer: Callable[[Iterable[bytes]], _T],
) -> _T:
    """Synchronously consume exact planned bytes through the source authority.

    The iterable is one-shot and inactive after ``consumer`` returns or raises.
    After a normal return this function synchronously drains any unread tail,
    which keeps object-store deduplication from skipping source authentication.
    A consumer failure is never followed by another producer read.  Cleanup,
    reader validity, and final namespace checks are always attempted while
    preserving the first callback failure as primary.
    """

    if type(publication) is not PublicationDirectoryReader:
        raise TypeError("planned view bundle consumption requires a publication reader")
    if type(plan) is not PlannedViewBundle:
        raise TypeError("planned view bundle must use the exact plan type")
    if not callable(consumer):
        raise TypeError("planned view bundle consumer must be callable")
    receipt = _require_planned_view_bundle(plan)
    if publication is not receipt.publication:
        raise RuntimeError(
            "planned view bundle must be consumed in its planning reader callback"
        )

    outer_before = publication.capture_ownership()
    outer_snapshot = _canonical_ownership_snapshot(outer_before)
    if outer_snapshot != _canonical_ownership_snapshot(receipt.outer_ownership):
        raise RuntimeError("planned view bundle outer ownership changed")
    _require_publication_subtree_projection(
        outer_before,
        receipt.prefix,
        receipt.source_ownership,
    )
    chunks = _CallbackScopedBundleChunks(
        publication,
        receipt,
        expected_digest=receipt.digest,
    )

    def consume() -> _T:
        try:
            result = consumer(chunks)
            chunks._drain()
            return result
        except BaseException as error:  # noqa: B036 - retain cleanup reachability
            chunks._remember_primary(error)
            raise

    def validate_after_namespace() -> None:
        outer_after = publication.capture_ownership()
        if _canonical_ownership_snapshot(outer_after) != outer_snapshot:
            raise RuntimeError("view bundle publication tree changed while consumed")
        _require_publication_subtree_projection(
            outer_after,
            receipt.prefix,
            receipt.source_ownership,
        )

    return _run_bundle_stream_callback(
        consume,
        chunks,
        (
            (
                "planned view bundle completeness validation also failed",
                chunks._require_complete,
            ),
            (
                "planned view bundle namespace validation also failed",
                validate_after_namespace,
            ),
            (
                "planned view bundle reader validity validation also failed",
                publication._require_valid,
            ),
        ),
    )


def verify_view_bundle(
    archive_path: str | Path,
    *,
    expected_view_type: str | None = None,
    expected_digest: str | None = None,
    expected_size: int | None = None,
    max_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
) -> ViewBundleRecord:
    """Verify canonical structure, inventory, bytes, and archive identity."""

    _validate_limits(max_files, max_bytes, max_metadata_bytes)
    expected_view = (
        _validate_view_type(expected_view_type)
        if expected_view_type is not None
        else None
    )
    expected_object_digest = (
        normalize_digest(expected_digest) if expected_digest is not None else None
    )
    expected_object_size = _validate_expected_size(expected_size)
    with _open_stable_regular_file(
        archive_path,
        label="view bundle archive",
    ) as (resolved, handle, signature):
        if expected_object_size is not None and signature[3] != expected_object_size:
            raise StorageIntegrityError(
                "view bundle archive size does not match the expected object"
            )
        validate_view_bundle_physical_size(
            signature[3],
            max_files=max_files,
            max_bytes=max_bytes,
            max_metadata_bytes=max_metadata_bytes,
        )
        observed_digest, observed_size = _consume_exact_file(
            handle,
            signature[3],
            label="view bundle archive",
        )
        if (
            expected_object_digest is not None
            and observed_digest != expected_object_digest
        ):
            raise StorageIntegrityError(
                "view bundle archive digest does not match the expected object"
            )
        handle.seek(0)
        record, _manifest = _verify_archive_handle(
            handle,
            resolved,
            expected_view_type=expected_view,
            archive_digest=observed_digest,
            archive_size=observed_size,
            max_files=max_files,
            max_bytes=max_bytes,
            max_metadata_bytes=max_metadata_bytes,
        )
        return record


def materialize_view_bundle(
    archive_path: str | Path,
    output_dir: str | Path,
    *,
    expected_view_type: str | None = None,
    expected_digest: str | None = None,
    expected_size: int | None = None,
    max_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    forbidden_roots: Iterable[str | Path] = (),
) -> MaterializedViewBundle:
    """Verify, extract, and atomically publish a bundle's payload directory."""

    _validate_limits(max_files, max_bytes, max_metadata_bytes)
    if not _SAFE_DIRECTORY_FDS:
        raise StorageValidationError(
            "secure view bundle materialization requires no-follow directory-fd "
            "support on this platform"
        )
    forbidden_roots = tuple(forbidden_roots)
    expected_view = (
        _validate_view_type(expected_view_type)
        if expected_view_type is not None
        else None
    )
    expected_object_digest = (
        normalize_digest(expected_digest) if expected_digest is not None else None
    )
    expected_object_size = _validate_expected_size(expected_size)
    archive = _regular_file_path(archive_path, label="view bundle archive")
    output = _output_path(output_dir)
    _reject_overlap(output, archive, label="view bundle archive and output")
    _reject_forbidden_roots(output, forbidden_roots, label="view bundle output")
    _validate_materialization_target(
        output,
        max_files=max_files,
        max_bytes=max_bytes,
        max_metadata_bytes=max_metadata_bytes,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    output = _output_path(output)
    _reject_overlap(output, archive, label="view bundle archive and output")
    _reject_forbidden_roots(output, forbidden_roots, label="view bundle output")
    expected_ownership = _validate_materialization_target(
        output,
        max_files=max_files,
        max_bytes=max_bytes,
        max_metadata_bytes=max_metadata_bytes,
    )
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}.materialize-",
            dir=str(output.parent),
        )
    )
    stage_root_identity = _directory_inode_identity(stage.lstat())
    published_ownership: list[object] = []
    try:
        with _open_extraction_root(stage, stage_root_identity) as stage_descriptor:
            with _open_stable_regular_file(
                archive,
                label="view bundle archive",
            ) as (resolved, handle, signature):
                if (
                    expected_object_size is not None
                    and signature[3] != expected_object_size
                ):
                    raise StorageIntegrityError(
                        "view bundle archive size does not match the expected object"
                    )
                validate_view_bundle_physical_size(
                    signature[3],
                    max_files=max_files,
                    max_bytes=max_bytes,
                    max_metadata_bytes=max_metadata_bytes,
                )
                observed_digest, observed_size = _consume_exact_file(
                    handle,
                    signature[3],
                    label="view bundle archive",
                )
                if (
                    expected_object_digest is not None
                    and observed_digest != expected_object_digest
                ):
                    raise StorageIntegrityError(
                        "view bundle archive digest does not match the expected object"
                    )
                handle.seek(0)
                record, _manifest_bytes_value = _verify_archive_handle(
                    handle,
                    resolved,
                    expected_view_type=expected_view,
                    archive_digest=observed_digest,
                    archive_size=observed_size,
                    max_files=max_files,
                    max_bytes=max_bytes,
                    max_metadata_bytes=max_metadata_bytes,
                    extraction_root_descriptor=stage_descriptor,
                )
            os.fsync(stage_descriptor)
            _require_owned_stage_path(stage, stage_root_identity)
            final_stage_identity = _directory_identity(os.fstat(stage_descriptor))
            expected_new_ownership = _validate_materialization_target(
                stage,
                max_files=max_files,
                max_bytes=max_bytes,
                max_metadata_bytes=max_metadata_bytes,
            )
            if (
                expected_new_ownership.root_identity != final_stage_identity
                or _directory_identity(os.fstat(stage_descriptor))
                != final_stage_identity
            ):
                raise StorageIntegrityError(
                    "view bundle materialization stage changed before publication"
                )

        def validate_moved_destination(
            moved: PublicationDirectoryReader,
        ) -> None:
            try:
                observed, _ownership = _validate_materialization_reader(
                    moved,
                    max_files=max_files,
                    max_bytes=max_bytes,
                    max_metadata_bytes=max_metadata_bytes,
                )
            except (OSError, StorageValidationError, StorageIntegrityError) as exc:
                raise StorageIntegrityError(
                    "moved destination failed publication-boundary validation"
                ) from exc
            if not _same_materialization_semantics(
                observed,
                expected_ownership,
            ):
                raise StorageIntegrityError(
                    "moved destination failed publication-boundary validation"
                )

        def validate_published_destination(
            published: PublicationDirectoryReader,
        ) -> None:
            try:
                observed, ownership = _validate_materialization_reader(
                    published,
                    max_files=max_files,
                    max_bytes=max_bytes,
                    max_metadata_bytes=max_metadata_bytes,
                )
            except (OSError, StorageValidationError, StorageIntegrityError) as exc:
                raise StorageIntegrityError(
                    "published stage failed publication-boundary validation"
                ) from exc
            if not _same_materialization_semantics(
                observed,
                expected_new_ownership,
            ):
                raise StorageIntegrityError(
                    "published stage failed publication-boundary validation"
                )
            published_ownership.append(ownership)

        publication_orphan = publish_staged_directory(
            stage,
            output,
            expected_destination_identity=expected_ownership.root_identity,
            expected_stage_identity=final_stage_identity,
            validate_moved_destination=validate_moved_destination,
            validate_published_destination=validate_published_destination,
        )
        _record_publication_orphan(publication_orphan, operation="publication")
        if len(published_ownership) != 1:
            raise StorageIntegrityError(
                "published materialized bundle has no authenticated ownership"
            )
        return MaterializedViewBundle(
            output_dir=output,
            payload_dir=output / VIEW_BUNDLE_PAYLOAD,
            ownership=published_ownership[0],
            view_type=record.view_type,
            digest=record.digest,
            byte_size=record.byte_size,
            members=record.members,
        )
    finally:
        _remove_owned_stage(stage, stage_root_identity)


def _validate_view_type(value: str) -> str:
    if not isinstance(value, str) or _VIEW_TYPE_RE.fullmatch(value) is None:
        raise StorageValidationError(
            "view type must be a portable identifier of at most 128 characters"
        )
    return value


def _validate_limits(max_files: int, max_bytes: int, max_metadata_bytes: int) -> None:
    for value, label in (
        (max_files, "maximum file count"),
        (max_bytes, "maximum payload bytes"),
        (max_metadata_bytes, "maximum metadata bytes"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise StorageValidationError(f"{label} must be a positive integer")


def _validate_expected_size(value: int | None) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise StorageValidationError(
            "expected archive size must be a nonnegative integer"
        )
    return value


def validate_view_bundle_physical_size(
    byte_size: int,
    *,
    max_files: int,
    max_bytes: int,
    max_metadata_bytes: int,
) -> None:
    """Reject an archive receipt that cannot fit configured bundle limits.

    Callers that start from catalog or object metadata must execute this gate
    before ``ObjectStore.verify``, ``open``, or ``materialize`` so an oversized
    physical object is rejected before backend byte access.
    """

    _validate_limits(max_files, max_bytes, max_metadata_bytes)
    if byte_size is None:
        raise StorageValidationError(
            "view bundle archive size must be a nonnegative integer"
        )
    normalized_size = _validate_expected_size(byte_size)
    if normalized_size is None:  # pragma: no cover - guarded above for type narrowing
        raise StorageValidationError(
            "view bundle archive size must be a nonnegative integer"
        )
    maximum = (
        max_bytes
        + max_metadata_bytes
        + (max_files + 1) * _MAX_ZIP_ENTRY_OVERHEAD
        + _MAX_ZIP_ENVELOPE_BYTES
    )
    if normalized_size > maximum:
        raise StorageValidationError(
            f"view bundle archive exceeds its {maximum}-byte physical limit"
        )


def _canonical_member_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise StorageValidationError("view bundle member has an invalid path")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StorageValidationError(
            "view bundle member path is not valid Unicode"
        ) from exc
    if len(encoded) > _MAX_MEMBER_PATH_BYTES:
        raise StorageValidationError("view bundle member path is too long")
    if value != unicodedata.normalize("NFC", value):
        raise StorageValidationError("view bundle member path is not NFC-normalized")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise StorageValidationError(f"view bundle member path is unsafe: {value!r}")
    if len(path.parts) > _MAX_MEMBER_DEPTH:
        raise StorageValidationError(
            f"view bundle member path exceeds {_MAX_MEMBER_DEPTH} components"
        )
    for part in path.parts:
        try:
            encoded_part = part.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise StorageValidationError(
                "view bundle member path is not valid Unicode"
            ) from exc
        if (
            len(encoded_part) > _MAX_MEMBER_COMPONENT_BYTES
            or part[-1:] in {" ", "."}
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
            or any(character in _WINDOWS_FORBIDDEN for character in part)
            or part.split(".", 1)[0].translate(_WINDOWS_SUPERSCRIPT_DIGITS).upper()
            in _WINDOWS_RESERVED_NAMES
        ):
            raise StorageValidationError(
                f"view bundle member path is not portable: {value!r}"
            )
    return value


def _validate_member_paths(
    members: Iterable[ArtifactMember],
    *,
    max_files: int,
) -> _MemberPathTrie:
    # A node-id trie keeps the work and retained keys linear in the number of
    # path components.  Materializing every tuple prefix is quadratic for deep
    # inventories even when every individual path satisfies its byte budget.
    edges: dict[tuple[int, str], tuple[int, str]] = {}
    child_counts = [0]
    terminals: dict[int, str] = {}
    previous: str | None = None
    for member in members:
        path = _canonical_member_path(member.path)
        if previous is not None and path <= previous:
            raise StorageValidationError(
                "view bundle inventory paths must be unique and sorted"
            )
        previous = path
        node = 0
        for part in path.split("/"):
            if node in terminals:
                raise StorageValidationError(
                    "view bundle contains a file/ancestor path collision: "
                    f"{terminals[node]!r} and {path!r}"
                )
            portable_part = unicodedata.normalize("NFC", part).casefold()
            edge_key = (node, portable_part)
            edge = edges.get(edge_key)
            if edge is None:
                child = len(child_counts)
                if child + 1 > 2 * max_files + 2:
                    raise StorageValidationError(
                        "view bundle path topology exceeds its node budget"
                    )
                child_counts.append(0)
                child_counts[node] += 1
                edges[edge_key] = (child, part)
            else:
                child, spelling = edge
                if spelling != part:
                    raise StorageValidationError(
                        "view bundle member path has a case-folding collision: "
                        f"component {spelling!r} and {part!r} in {path!r}"
                    )
            node = child
        if node in terminals:
            raise StorageValidationError(
                f"view bundle member path collides with {terminals[node]!r}: {path!r}"
            )
        if child_counts[node]:
            raise StorageValidationError(
                "view bundle contains a file/ancestor path collision: "
                f"{path!r} is an ancestor of another member"
            )
        terminals[node] = path

    directory_nodes = frozenset(
        node for node in range(len(child_counts)) if node not in terminals
    )
    if len(directory_nodes) > max_files + 1:
        raise StorageValidationError(
            "view bundle path topology exceeds its implicit-directory budget"
        )
    return _MemberPathTrie(
        edges=edges,
        directory_nodes=directory_nodes,
    )


def _trie_contains_directory(
    trie: _MemberPathTrie,
    parts: tuple[str, ...],
) -> bool:
    node = 0
    for part in parts:
        portable_part = unicodedata.normalize("NFC", part).casefold()
        edge = trie.edges.get((node, portable_part))
        if edge is None:
            return False
        node, spelling = edge
        if spelling != part:
            return False
    return node in trie.directory_nodes


def _publication_subtree_prefix(
    value: str | PurePosixPath,
) -> PurePosixPath:
    if isinstance(value, PurePosixPath):
        raw = value.as_posix()
    elif isinstance(value, str):
        raw = value
    else:
        raise TypeError("view bundle subtree prefix must be a relative path")
    normalized = PurePosixPath(raw)
    try:
        encoded = raw.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise StorageValidationError(
            "view bundle subtree prefix is not valid Unicode"
        ) from exc
    if (
        not raw
        or not normalized.parts
        or raw != normalized.as_posix()
        or normalized.is_absolute()
        or any(part in {"", ".", ".."} for part in normalized.parts)
        or "\\" in raw
        or "\x00" in raw
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
        or len(encoded) > _MAX_MEMBER_PATH_BYTES
        or len(normalized.parts) > _MAX_MEMBER_DEPTH
    ):
        raise StorageValidationError("view bundle subtree prefix is not canonical")
    return normalized


def _validate_expected_subtree_ownership(ownership: object) -> None:
    """Validate an opaque ownership token without invoking hostile scalars."""

    _canonical_ownership_snapshot(ownership)


def _exact_identity_tuple(value: object, *, label: str) -> tuple[int, ...]:
    if type(value) is not tuple or any(type(item) is not int for item in value):
        raise TypeError(f"view bundle {label} must contain exact integers")
    return value  # type: ignore[return-value]


def _canonical_ownership_snapshot(ownership: object) -> tuple[object, ...]:
    """Return only exact built-in fields after full token validation."""

    if type(ownership) is not _TreeOwnership:
        raise TypeError("view bundle subtree ownership must be an exact token")
    root_identity = _exact_identity_tuple(
        ownership.root_identity,
        label="ownership root identity",
    )
    root_version = _exact_identity_tuple(
        ownership.root_version_identity,
        label="ownership root version",
    )
    if (
        type(ownership.digest) is not str
        or type(ownership.entries) is not int
        or type(ownership.byte_count) is not int
        or type(ownership.metadata_bytes) is not int
        or type(ownership.inventory) is not tuple
        or type(ownership.file_records) is not tuple
        or type(ownership.entry_identities) is not tuple
    ):
        raise TypeError("view bundle ownership fields must use exact built-in types")

    inventory: list[tuple[str, str]] = []
    for item in ownership.inventory:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not str
        ):
            raise TypeError("view bundle ownership inventory is not exact")
        inventory.append((item[0], item[1]))

    records: list[tuple[str, int, int, str]] = []
    for record in ownership.file_records:
        if (
            type(record) is not TreeFileRecord
            or type(record.path) is not str
            or type(record.mode) is not int
            or type(record.size) is not int
            or type(record.sha256) is not str
        ):
            raise TypeError("view bundle ownership file records are not exact")
        records.append((record.path, record.mode, record.size, record.sha256))

    identities: list[tuple[str, str, tuple[int, ...]]] = []
    for item in ownership.entry_identities:
        if (
            type(item) is not tuple
            or len(item) != 3
            or type(item[0]) is not str
            or type(item[1]) is not str
        ):
            raise TypeError("view bundle ownership entry identities are not exact")
        identities.append(
            (
                item[0],
                item[1],
                _exact_identity_tuple(item[2], label="ownership entry identity"),
            )
        )

    _validate_directory_ownership_token(ownership)
    return (
        root_identity,
        root_version,
        ownership.digest,
        ownership.entries,
        ownership.byte_count,
        ownership.metadata_bytes,
        tuple(inventory),
        tuple(records),
        tuple(identities),
    )


def _require_publication_subtree_projection(
    outer_ownership: object,
    prefix: PurePosixPath,
    expected_source_ownership: object,
) -> None:
    """Match a public atomic projection to the exact expected subtree token."""

    expected_snapshot = _canonical_ownership_snapshot(expected_source_ownership)
    _canonical_ownership_snapshot(outer_ownership)
    projected = project_directory_ownership_subtree(  # type: ignore[arg-type]
        outer_ownership,
        prefix,
    )
    if _canonical_ownership_snapshot(projected) != expected_snapshot:
        raise RuntimeError("view bundle subtree root or contents differ from expected")


def _publication_file_crc32(
    publication: PublicationDirectoryReader,
    relative: PurePosixPath,
    expected: TreeFileRecord,
) -> int:
    observed_size = 0
    checksum = 0
    with publication.iter_authenticated_chunks(
        relative,
        max_bytes=expected.size,
        chunk_size=_COPY_CHUNK_BYTES,
    ) as chunks:
        for chunk in chunks:
            observed_size += len(chunk)
            checksum = zlib.crc32(chunk, checksum)
    if observed_size != expected.size:
        raise StorageIntegrityError(
            f"view bundle source size changed while planning: {expected.path}"
        )
    return checksum & 0xFFFFFFFF


def _zip_member_local_extra(byte_size: int, *, guarded: bool) -> bytes:
    zip64 = (
        struct.pack("<HHQQ", 1, 16, byte_size, byte_size)
        if byte_size * 1.05 > _ZIP64_LIMIT
        else b""
    )
    return zip64 + (_ZIP_GUARD_EXTRA if guarded else b"")


def _zip_member_central_extra(
    byte_size: int,
    header_offset: int,
    *,
    guarded: bool,
) -> bytes:
    values: list[int] = []
    if byte_size > _ZIP64_LIMIT:
        values.extend((byte_size, byte_size))
    if header_offset > _ZIP64_LIMIT:
        values.append(header_offset)
    zip64 = (
        struct.pack(f"<HH{len(values)}Q", 1, 8 * len(values), *values)
        if values
        else b""
    )
    return zip64 + (_ZIP_GUARD_EXTRA if guarded else b"")


def _zip_envelope_bytes(
    member_count: int,
    central_size: int,
    central_offset: int,
) -> bytes:
    requires_zip64 = (
        member_count > _ZIP_FILECOUNT_LIMIT
        or central_size > _ZIP64_LIMIT
        or central_offset > _ZIP64_LIMIT
    )
    result = bytearray()
    classic_count = member_count
    classic_size = central_size
    classic_offset = central_offset
    if requires_zip64:
        zip64_offset = central_offset + central_size
        result.extend(
            struct.pack(
                "<4sQ2H2L4Q",
                b"PK\x06\x06",
                44,
                45,
                45,
                0,
                0,
                member_count,
                member_count,
                central_size,
                central_offset,
            )
        )
        result.extend(struct.pack("<4sLQL", b"PK\x06\x07", 0, zip64_offset, 1))
        classic_count = min(member_count, 0xFFFF)
        classic_size = min(central_size, 0xFFFFFFFF)
        classic_offset = min(central_offset, 0xFFFFFFFF)
    result.extend(
        struct.pack(
            "<4s4H2LH",
            b"PK\x05\x06",
            0,
            0,
            classic_count,
            classic_count,
            classic_size,
            classic_offset,
            0,
        )
    )
    return bytes(result)


def _plan_canonical_zip(
    *,
    publication: PublicationDirectoryReader,
    prefix: PurePosixPath,
    outer_ownership: object,
    source_ownership: object,
    view_type: str,
    members: tuple[ArtifactMember, ...],
    manifest_bytes: bytes,
    payload_crcs: tuple[int, ...],
) -> _PlannedViewBundleReceipt:
    if len(payload_crcs) != len(members):
        raise RuntimeError("view bundle CRC inventory is incomplete")
    raw_members: list[
        tuple[str, PurePosixPath | None, bytes | None, int, int, int, bool]
    ] = [
        (
            VIEW_BUNDLE_MANIFEST,
            None,
            manifest_bytes,
            0o644,
            len(manifest_bytes),
            zlib.crc32(manifest_bytes) & 0xFFFFFFFF,
            False,
        )
    ]
    raw_members.extend(
        (
            f"{VIEW_BUNDLE_PAYLOAD}/{member.path}",
            PurePosixPath(member.path),
            None,
            member.mode,
            member.byte_size,
            checksum,
            index == len(members) - 1,
        )
        for index, (member, checksum) in enumerate(
            zip(members, payload_crcs, strict=True)
        )
    )

    header_offset = 0
    planned: list[_PlannedZipMember] = []
    for (
        archive_name,
        source_relative,
        inline_payload,
        mode,
        byte_size,
        checksum,
        guarded,
    ) in raw_members:
        encoded_name = _encoded_zip_name(archive_name)
        flags = 0 if archive_name.isascii() else 0x800
        local_extra = _zip_member_local_extra(byte_size, guarded=guarded)
        central_extra = _zip_member_central_extra(
            byte_size,
            header_offset,
            guarded=guarded,
        )
        local_version = 45 if byte_size * 1.05 > _ZIP64_LIMIT else 20
        central_version = (
            45
            if local_version == 45
            or central_extra != (_ZIP_GUARD_EXTRA if guarded else b"")
            else 20
        )
        planned.append(
            _PlannedZipMember(
                archive_name=archive_name,
                encoded_name=encoded_name,
                source_relative=source_relative,
                inline_payload=inline_payload,
                mode=mode,
                byte_size=byte_size,
                crc32=checksum,
                flags=flags,
                header_offset=header_offset,
                local_extra=local_extra,
                central_extra=central_extra,
                local_version=local_version,
                central_version=central_version,
            )
        )
        header_offset += 30 + len(encoded_name) + len(local_extra) + byte_size

    central_offset = header_offset
    central_size = sum(
        46 + len(member.encoded_name) + len(member.central_extra) for member in planned
    )
    envelope = _zip_envelope_bytes(len(planned), central_size, central_offset)
    return _PlannedViewBundleReceipt(
        publication=publication,
        prefix=prefix,
        outer_ownership=outer_ownership,
        source_ownership=source_ownership,
        view_type=view_type,
        digest="",
        byte_size=central_offset + central_size + len(envelope),
        members=members,
        manifest_bytes=manifest_bytes,
        zip_members=tuple(planned),
        central_offset=central_offset,
        central_size=central_size,
        envelope_bytes=envelope,
    )


def _local_zip_header(member: _PlannedZipMember) -> bytes:
    zip64 = member.local_version == 45
    stored_size = 0xFFFFFFFF if zip64 else member.byte_size
    return struct.pack(
        "<4s5H3L2H",
        b"PK\x03\x04",
        member.local_version,
        member.flags,
        zipfile.ZIP_STORED,
        0,
        33,
        member.crc32,
        stored_size,
        stored_size,
        len(member.encoded_name),
        len(member.local_extra),
    )


def _central_zip_header(member: _PlannedZipMember) -> bytes:
    zip64_size = member.byte_size > _ZIP64_LIMIT
    stored_size = 0xFFFFFFFF if zip64_size else member.byte_size
    stored_offset = (
        0xFFFFFFFF if member.header_offset > _ZIP64_LIMIT else member.header_offset
    )
    return struct.pack(
        "<4s4B4HL2L5H2L",
        b"PK\x01\x02",
        member.central_version,
        3,
        member.central_version,
        0,
        member.flags,
        zipfile.ZIP_STORED,
        0,
        33,
        member.crc32,
        stored_size,
        stored_size,
        len(member.encoded_name),
        len(member.central_extra),
        0,
        0,
        0,
        (stat.S_IFREG | member.mode) << 16,
        stored_offset,
    )


def _iter_planned_view_bundle_bytes(
    publication: PublicationDirectoryReader,
    receipt: _PlannedViewBundleReceipt,
) -> Iterator[bytes]:
    for member in receipt.zip_members:
        yield _local_zip_header(member)
        yield member.encoded_name
        if member.local_extra:
            yield member.local_extra
        if member.inline_payload is not None:
            if len(member.inline_payload) != member.byte_size:
                raise RuntimeError("planned inline bundle member changed")
            yield member.inline_payload
            continue
        if member.source_relative is None:
            raise RuntimeError("planned bundle member has no byte source")
        observed_size = 0
        checksum = 0
        source = receipt.prefix / member.source_relative
        with publication.iter_authenticated_chunks(
            source,
            max_bytes=member.byte_size,
            chunk_size=_COPY_CHUNK_BYTES,
        ) as chunks:
            for chunk in chunks:
                observed_size += len(chunk)
                checksum = zlib.crc32(chunk, checksum)
                yield chunk
        if observed_size != member.byte_size or checksum & 0xFFFFFFFF != member.crc32:
            raise StorageIntegrityError(
                f"planned view bundle source changed: {member.source_relative}"
            )

    for member in receipt.zip_members:
        yield _central_zip_header(member)
        yield member.encoded_name
        if member.central_extra:
            yield member.central_extra
    yield receipt.envelope_bytes


def _require_planned_view_bundle(
    plan: PlannedViewBundle,
) -> _PlannedViewBundleReceipt:
    if type(plan) is not PlannedViewBundle:
        raise TypeError("planned view bundle must use the exact plan type")
    if type(plan.members) is not tuple or not all(
        type(member) is ArtifactMember for member in plan.members
    ):
        raise TypeError("planned view bundle members must be artifact records")
    if type(plan.view_type) is not str or type(plan.digest) is not str:
        raise StorageValidationError("planned view bundle text fields are invalid")
    view_type = _validate_view_type(plan.view_type)
    digest = normalize_digest(plan.digest)
    if digest != plan.digest:
        raise StorageValidationError("planned view bundle digest is not canonical")
    if type(plan.byte_size) is not int:
        raise StorageValidationError("planned view bundle size is invalid")
    if plan.byte_size < 0:
        raise StorageValidationError("planned view bundle size is invalid")
    _validate_expected_subtree_ownership(plan.source_ownership)
    receipt = plan._receipt
    if type(receipt) is not _PlannedViewBundleReceipt:
        raise StorageIntegrityError("planned view bundle has no authority receipt")
    if type(receipt.publication) is not PublicationDirectoryReader:
        raise StorageIntegrityError("planned view bundle reader binding is invalid")
    if (
        receipt.view_type != view_type
        or receipt.digest != digest
        or receipt.byte_size != plan.byte_size
        or receipt.members is not plan.members
        or receipt.source_ownership is not plan.source_ownership
    ):
        raise StorageIntegrityError(
            "planned view bundle fields differ from their authority receipt"
        )
    _require_valid_planned_layout(receipt)
    return receipt


def _require_valid_planned_layout(receipt: _PlannedViewBundleReceipt) -> None:
    if (
        type(receipt.prefix) is not PurePosixPath
        or type(receipt.view_type) is not str
        or type(receipt.digest) is not str
        or type(receipt.byte_size) is not int
        or type(receipt.members) is not tuple
        or any(type(member) is not ArtifactMember for member in receipt.members)
        or type(receipt.manifest_bytes) is not bytes
        or type(receipt.zip_members) is not tuple
        or any(type(member) is not _PlannedZipMember for member in receipt.zip_members)
        or type(receipt.central_offset) is not int
        or type(receipt.central_size) is not int
        or type(receipt.envelope_bytes) is not bytes
    ):
        raise StorageIntegrityError("planned view bundle layout types are invalid")
    if not receipt.zip_members or len(receipt.zip_members) != len(receipt.members) + 1:
        raise StorageIntegrityError("planned view bundle ZIP inventory is incomplete")
    if receipt.manifest_bytes != _manifest_bytes(
        receipt.view_type,
        receipt.members,
    ):
        raise StorageIntegrityError("planned view bundle manifest binding is invalid")
    for member in receipt.zip_members:
        if (
            type(member.archive_name) is not str
            or type(member.encoded_name) is not bytes
            or (
                member.source_relative is not None
                and type(member.source_relative) is not PurePosixPath
            )
            or (
                member.inline_payload is not None
                and type(member.inline_payload) is not bytes
            )
            or any(
                type(value) is not int
                for value in (
                    member.mode,
                    member.byte_size,
                    member.crc32,
                    member.flags,
                    member.header_offset,
                    member.local_version,
                    member.central_version,
                )
            )
            or type(member.local_extra) is not bytes
            or type(member.central_extra) is not bytes
            or member.byte_size < 0
            or member.crc32 < 0
            or member.crc32 > 0xFFFFFFFF
        ):
            raise StorageIntegrityError("planned view bundle ZIP member is invalid")

    first = receipt.zip_members[0]
    if (
        first.archive_name != VIEW_BUNDLE_MANIFEST
        or first.source_relative is not None
        or first.inline_payload != receipt.manifest_bytes
        or first.header_offset != 0
        or first.mode != 0o644
    ):
        raise StorageIntegrityError("planned view bundle metadata layout is invalid")
    rebuilt = _plan_canonical_zip(
        publication=receipt.publication,
        prefix=receipt.prefix,
        outer_ownership=receipt.outer_ownership,
        source_ownership=receipt.source_ownership,
        view_type=receipt.view_type,
        members=receipt.members,
        manifest_bytes=receipt.manifest_bytes,
        payload_crcs=tuple(member.crc32 for member in receipt.zip_members[1:]),
    )
    if (
        receipt.zip_members != rebuilt.zip_members
        or receipt.central_offset != rebuilt.central_offset
        or receipt.central_size != rebuilt.central_size
        or receipt.envelope_bytes != rebuilt.envelope_bytes
        or receipt.byte_size != rebuilt.byte_size
    ):
        raise StorageIntegrityError(
            "planned view bundle canonical layout is inconsistent"
        )


class _CallbackScopedBundleChunks:
    """One-shot iterable revoked synchronously after its consumer callback."""

    __slots__ = (
        "_active",
        "_ambient_error",
        "_closed",
        "_complete",
        "_digest",
        "_error",
        "_expected_digest",
        "_iterator",
        "_primary_error",
        "_publication",
        "_receipt",
        "_size",
    )

    def __init__(
        self,
        publication: PublicationDirectoryReader,
        receipt: _PlannedViewBundleReceipt,
        *,
        expected_digest: str | None,
    ) -> None:
        self._active = True
        self._ambient_error = sys.exc_info()[1]
        self._closed = False
        self._complete = False
        self._digest = hashlib.sha256()
        self._error: BaseException | None = None
        self._expected_digest = expected_digest
        self._iterator: Iterator[bytes] | None = None
        self._primary_error: BaseException | None = None
        self._publication = publication
        self._receipt = receipt
        self._size = 0

    def __iter__(self) -> _CallbackScopedBundleChunks:
        self._require_active()
        return self

    def __next__(self) -> bytes:
        self._require_active()
        if self._closed:
            raise RuntimeError("planned view bundle stream is closed")
        if self._complete:
            raise StopIteration
        if self._iterator is None:
            self._iterator = _iter_planned_view_bundle_bytes(
                self._publication,
                self._receipt,
            )
        try:
            chunk = next(self._iterator)
        except StopIteration:
            self._complete = True
            raise
        except BaseException as error:  # noqa: B036 - remember suppressed failure
            if self._error is None:
                self._error = error
            raise
        if not isinstance(chunk, bytes) or not chunk:
            invalid_chunk_error = RuntimeError(
                "planned view bundle yielded an invalid chunk"
            )
            if self._error is None:
                self._error = invalid_chunk_error
            raise invalid_chunk_error
        self._digest.update(chunk)
        self._size += len(chunk)
        return chunk

    def _require_active(self) -> None:
        if not self._active:
            raise RuntimeError("planned view bundle stream is no longer active")

    def _close(self) -> None:
        if self._closed:
            return
        iterator = self._iterator
        if iterator is None:
            self._closed = True
            return
        deferred: BaseException | None = None
        for _attempt in range(_BUNDLE_STREAM_CLOSE_RECOVERY_LIMIT):
            try:
                terminal = _bundle_iterator_is_terminal(iterator)
            except BaseException as error:  # noqa: B036 - bounded reconciliation
                if deferred is None:
                    deferred = error
                continue
            if terminal:
                self._closed = True
                if deferred is not None:
                    raise deferred.with_traceback(deferred.__traceback__)
                return
            try:
                _close_bundle_iterator(iterator)
            except BaseException as error:  # noqa: B036 - reconcile before retry
                if deferred is None:
                    deferred = error

        try:
            terminal = _bundle_iterator_is_terminal(iterator)
        except BaseException as error:  # noqa: B036 - final bounded probe
            if deferred is None:
                deferred = error
        else:
            if terminal:
                self._closed = True
                if deferred is not None:
                    raise deferred.with_traceback(deferred.__traceback__)
                return

        cleanup_error = _BundleStreamCleanupError(self)
        primary = self._primary_error
        if primary is None:
            active = sys.exc_info()[1]
            if active is not None and active is not self._ambient_error:
                primary = active
        if primary is not None:
            _retain_bundle_stream_cleanup_owner(primary, self)
        if deferred is not None:
            raise cleanup_error from deferred
        raise cleanup_error

    def _deactivate(self) -> None:
        deferred: BaseException | None = None
        for _attempt in range(_BUNDLE_STREAM_REVOKE_RECOVERY_LIMIT):
            try:
                inactive = _bundle_stream_is_inactive(self)
            except BaseException as error:  # noqa: B036 - bounded reconciliation
                deferred = _retain_bundle_stream_error(
                    deferred,
                    "additional bundle stream revocation probe failed",
                    error,
                )
                continue
            if inactive:
                if deferred is not None:
                    raise deferred.with_traceback(deferred.__traceback__)
                return
            try:
                _revoke_bundle_stream(self)
            except BaseException as error:  # noqa: B036 - reconcile before retry
                deferred = _retain_bundle_stream_error(
                    deferred,
                    "additional bundle stream revocation failed",
                    error,
                )

        try:
            inactive = _bundle_stream_is_inactive(self)
        except BaseException as error:  # noqa: B036 - final bounded probe
            deferred = _retain_bundle_stream_error(
                deferred,
                "additional bundle stream revocation probe failed",
                error,
            )
        else:
            if inactive:
                if deferred is not None:
                    raise deferred.with_traceback(deferred.__traceback__)
                return

        cleanup_error = _BundleStreamRevocationError(self)
        primary = self._primary_error
        if primary is None:
            active = sys.exc_info()[1]
            if active is not None and active is not self._ambient_error:
                primary = active
        if primary is not None:
            _retain_bundle_stream_cleanup_owner(primary, self)
        if deferred is not None:
            raise cleanup_error from deferred
        raise cleanup_error

    def _remember_primary(self, error: BaseException) -> None:
        if self._primary_error is None:
            self._primary_error = error

    def _drain(self) -> None:
        for _chunk in self:
            pass

    def retry_cleanup(self) -> None:
        """Retry terminal close after a reported persistent cleanup failure."""

        with _run_context_with_cleanup_actions(
            (_bundle_stream_revoke_action(self, retry=True),)
        ):
            self._close()

    def _observed_digest(self) -> str:
        self._require_complete()
        return self._digest.hexdigest()

    def _require_complete(self) -> None:
        if self._error is not None:
            raise RuntimeError(
                "planned view bundle consumer suppressed a stream failure"
            ) from self._error
        if not self._complete:
            raise RuntimeError("planned view bundle consumer did not read through EOF")
        if self._size != self._receipt.byte_size:
            raise StorageIntegrityError(
                "consumed view bundle bytes differ from their plan"
            )
        if (
            self._expected_digest is not None
            and self._digest.hexdigest() != self._expected_digest
        ):
            raise StorageIntegrityError(
                "consumed view bundle bytes differ from their plan"
            )


class _BundleStreamCleanupError(RuntimeError):
    """Persistent stream close failure retaining its retry owner."""

    def __init__(self, cleanup_owner: _CallbackScopedBundleChunks) -> None:
        super().__init__("planned view bundle stream cleanup did not converge")
        self.cleanup_owner = cleanup_owner


class _BundleStreamRevocationError(RuntimeError):
    """Persistent stream revocation failure retaining its retry owner."""

    def __init__(self, cleanup_owner: _CallbackScopedBundleChunks) -> None:
        super().__init__("planned view bundle stream revocation did not converge")
        self.cleanup_owner = cleanup_owner


def _retain_bundle_stream_error(
    primary_error: BaseException | None,
    label: str,
    error: BaseException,
) -> BaseException:
    if primary_error is None:
        return error
    _annotate_secondary_error(primary_error, label, error)
    return primary_error


def _retain_bundle_stream_cleanup_owner(
    primary_error: BaseException,
    cleanup_owner: _CallbackScopedBundleChunks,
) -> None:
    owners = list(getattr(primary_error, "_codenib_bundle_stream_cleanup_owners", ()))
    if not any(owner is cleanup_owner for owner in owners):
        owners.append(cleanup_owner)
    primary_error._codenib_bundle_stream_cleanup_owners = tuple(owners)  # type: ignore[attr-defined]


def _bundle_iterator_is_terminal(iterator: Iterator[bytes]) -> bool:
    if inspect.isgenerator(iterator):
        return inspect.getgeneratorstate(iterator) == inspect.GEN_CLOSED
    return bool(getattr(iterator, "closed", False))


def _close_bundle_iterator(iterator: Iterator[bytes]) -> None:
    close = getattr(iterator, "close", None)
    if close is None:
        raise RuntimeError("planned view bundle iterator has no close operation")
    close()


def _bundle_stream_is_inactive(stream: _CallbackScopedBundleChunks) -> bool:
    return not stream._active


def _revoke_bundle_stream(stream: _CallbackScopedBundleChunks) -> None:
    stream._active = False


def _bundle_stream_is_closed(stream: _CallbackScopedBundleChunks) -> bool:
    return stream._closed


def _bundle_stream_close_action(
    stream: _CallbackScopedBundleChunks,
    *,
    planning: bool,
) -> _OrderedAction:
    scope = "planned view bundle hashing" if planning else "planned view bundle"
    return _OrderedAction(
        label=f"{scope} stream close also failed",
        action=stream._close,
        complete=lambda: _bundle_stream_is_closed(stream),
        retry_incomplete="cancellation",
    )


def _bundle_stream_revoke_action(
    stream: _CallbackScopedBundleChunks,
    *,
    planning: bool = False,
    retry: bool = False,
) -> _OrderedAction:
    if retry:
        label = "planned view bundle stream revocation retry also failed"
    elif planning:
        label = "planned view bundle hashing stream revocation also failed"
    else:
        label = "planned view bundle stream revocation also failed"
    return _OrderedAction(
        label=label,
        action=stream._deactivate,
        complete=lambda: _bundle_stream_is_inactive(stream),
        retry_incomplete="cancellation",
    )


def _run_bundle_stream_callback(
    callback: Callable[[], _T],
    stream: _CallbackScopedBundleChunks,
    post_validations: tuple[tuple[str, Callable[[], None]], ...],
) -> _T:
    planning = stream._expected_digest is None

    def run() -> _T:
        with _run_context_with_cleanup_actions(
            (
                _bundle_stream_close_action(stream, planning=planning),
                _bundle_stream_revoke_action(stream, planning=planning),
            )
        ):
            return callback()

    return _run_callback_with_post_validations(run, post_validations)


def _manifest_value(
    view_type: str,
    members: Iterable[ArtifactMember],
) -> dict[str, Any]:
    return {
        "schema": VIEW_BUNDLE_SCHEMA,
        "view_type": view_type,
        "files": [
            {
                "path": member.path,
                "size": member.byte_size,
                "sha256": member.digest,
                "mode": member.mode,
            }
            for member in members
        ],
    }


def _manifest_bytes(view_type: str, members: Iterable[ArtifactMember]) -> bytes:
    return canonical_json(_manifest_value(view_type, members)).encode("utf-8")


def _canonical_zip_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = _CanonicalZipInfo(filename=name, date_time=_FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.create_version = 20
    info.extract_version = 20
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.internal_attr = 0
    info.extra = b""
    info.comment = b""
    return info


def _read_zip_envelope(
    handle: BinaryIO,
    *,
    max_files: int,
    max_metadata_bytes: int,
) -> _ZipEnvelope:
    """Read the bounded EOCD records before ZipFile allocates its member list."""

    handle.seek(0, os.SEEK_END)
    archive_size = handle.tell()
    if archive_size < 22:
        raise StorageValidationError("view bundle is too small to be a ZIP")
    handle.seek(archive_size - 22)
    end_record = handle.read(22)
    try:
        (
            signature,
            disk_number,
            central_disk,
            disk_count,
            member_count,
            central_size,
            central_offset,
            comment_size,
        ) = struct.unpack("<4s4H2LH", end_record)
    except struct.error as exc:
        raise StorageValidationError(
            "view bundle has a truncated ZIP envelope"
        ) from exc
    if (
        signature != b"PK\x05\x06"
        or disk_number != 0
        or central_disk != 0
        or disk_count != member_count
        or comment_size != 0
    ):
        raise StorageValidationError("view bundle has a non-canonical ZIP envelope")

    # Prove the ordinary envelope first.  Bytes immediately before a classic
    # EOCD belong to the central directory and may coincidentally begin with a
    # ZIP64 locator signature.
    is_classic = (
        central_size != 0xFFFFFFFF
        and central_offset != 0xFFFFFFFF
        and central_offset + central_size == archive_size - 22
    )
    has_zip64 = False
    if not is_classic:
        locator_offset = archive_size - 22 - 20
        if locator_offset < 0:
            raise StorageValidationError(
                "view bundle has trailing or missing ZIP bytes"
            )
        handle.seek(locator_offset)
        locator = handle.read(20)
        if not locator.startswith(b"PK\x06\x07"):
            raise StorageValidationError(
                "view bundle has trailing or missing ZIP bytes"
            )
        has_zip64 = True
        try:
            locator_signature, locator_disk, zip64_offset, total_disks = struct.unpack(
                "<4sLQL", locator
            )
        except struct.error as exc:
            raise StorageValidationError(
                "view bundle has a truncated ZIP64 locator"
            ) from exc
        if (
            locator_signature != b"PK\x06\x07"
            or locator_disk != 0
            or total_disks != 1
            or zip64_offset + 56 != locator_offset
        ):
            raise StorageValidationError(
                "view bundle has a non-canonical ZIP64 locator"
            )
        handle.seek(zip64_offset)
        zip64_record = handle.read(56)
        try:
            (
                zip64_signature,
                record_size,
                create_version,
                extract_version,
                zip64_disk,
                zip64_central_disk,
                zip64_disk_count,
                zip64_member_count,
                zip64_central_size,
                zip64_central_offset,
            ) = struct.unpack("<4sQ2H2L4Q", zip64_record)
        except struct.error as exc:
            raise StorageValidationError(
                "view bundle has a truncated ZIP64 end record"
            ) from exc
        if (
            zip64_signature != b"PK\x06\x06"
            or record_size != 44
            or create_version != 45
            or extract_version != 45
            or zip64_disk != 0
            or zip64_central_disk != 0
            or zip64_disk_count != zip64_member_count
            or zip64_central_offset + zip64_central_size != zip64_offset
            or member_count != min(zip64_member_count, 0xFFFF)
            or central_size != min(zip64_central_size, 0xFFFFFFFF)
            or central_offset != min(zip64_central_offset, 0xFFFFFFFF)
        ):
            raise StorageValidationError(
                "view bundle has a non-canonical ZIP64 end record"
            )
        member_count = zip64_member_count
        central_size = zip64_central_size
        central_offset = zip64_central_offset
        if not (
            member_count > _ZIP_FILECOUNT_LIMIT
            or central_size > _ZIP64_LIMIT
            or central_offset > _ZIP64_LIMIT
        ):
            raise StorageValidationError("view bundle has a redundant ZIP64 envelope")

    if member_count < 1 or member_count > max_files + 1:
        raise StorageValidationError("view bundle has too many ZIP members")
    payload_count = member_count - 1
    maximum_central_size = (
        max_metadata_bytes
        + 46
        + len(VIEW_BUNDLE_MANIFEST)
        + _MAX_ZIP_CENTRAL_EXTRA_BYTES
        + payload_count
        * (46 + len(f"{VIEW_BUNDLE_PAYLOAD}/") + _MAX_ZIP_CENTRAL_EXTRA_BYTES)
    )
    if central_size > maximum_central_size:
        raise StorageValidationError("view bundle ZIP directory exceeds its budget")
    if central_size < len(_ZIP_GUARD_EXTRA):
        raise StorageValidationError(
            "view bundle ZIP directory is missing its terminal guard"
        )
    handle.seek(central_offset + central_size - len(_ZIP_GUARD_EXTRA))
    if handle.read(len(_ZIP_GUARD_EXTRA)) != _ZIP_GUARD_EXTRA:
        raise StorageValidationError(
            "view bundle ZIP directory has a non-canonical terminal guard"
        )
    return _ZipEnvelope(
        member_count=member_count,
        central_size=central_size,
        central_offset=central_offset,
        zip64=has_zip64,
    )


def _encoded_zip_name(name: str) -> bytes:
    return name.encode("ascii") if name.isascii() else name.encode("utf-8")


def _canonical_central_extra(
    info: zipfile.ZipInfo,
    *,
    guarded: bool,
) -> bytes:
    values: list[int] = []
    if info.file_size > _ZIP64_LIMIT or info.compress_size > _ZIP64_LIMIT:
        values.extend((info.file_size, info.compress_size))
    if info.header_offset > _ZIP64_LIMIT:
        values.append(info.header_offset)
    zip64_extra = (
        struct.pack(f"<HH{len(values)}Q", 1, 8 * len(values), *values)
        if values
        else b""
    )
    return zip64_extra + (_ZIP_GUARD_EXTRA if guarded else b"")


def _validate_canonical_zip_layout(
    handle: BinaryIO,
    infos: list[zipfile.ZipInfo],
    envelope: _ZipEnvelope,
) -> None:
    """Reject padding, alternate headers, descriptors, and noncanonical extras."""

    central_size = 0
    for index, info in enumerate(infos):
        encoded_name = _encoded_zip_name(info.filename)
        guarded = index == len(infos) - 1
        expected_central_extra = _canonical_central_extra(
            info,
            guarded=guarded,
        )
        central_size += 46 + len(encoded_name) + len(expected_central_extra)

        handle.seek(info.header_offset)
        local_header = handle.read(30)
        try:
            (
                signature,
                extract_version,
                flags,
                compression,
                modified_time,
                modified_date,
                crc,
                compressed_size,
                file_size,
                name_size,
                extra_size,
            ) = struct.unpack("<4s5H3L2H", local_header)
        except struct.error as exc:
            raise StorageValidationError(
                f"view bundle ZIP member has a truncated header: {info.filename}"
            ) from exc
        local_zip64 = info.file_size * 1.05 > _ZIP64_LIMIT
        expected_local_zip64_extra = (
            struct.pack("<HHQQ", 1, 16, info.file_size, info.compress_size)
            if local_zip64
            else b""
        )
        expected_local_extra = expected_local_zip64_extra + (
            _ZIP_GUARD_EXTRA if guarded else b""
        )
        expected_version = 45 if local_zip64 else 20
        expected_size = 0xFFFFFFFF if local_zip64 else info.file_size
        expected_compressed_size = 0xFFFFFFFF if local_zip64 else info.compress_size
        local_name = handle.read(name_size)
        local_extra = handle.read(extra_size)
        if (
            signature != b"PK\x03\x04"
            or extract_version != expected_version
            or flags != info.flag_bits
            or compression != zipfile.ZIP_STORED
            or modified_time != 0
            or modified_date != 33
            or crc != info.CRC
            or compressed_size != expected_compressed_size
            or file_size != expected_size
            or local_name != encoded_name
            or local_extra != expected_local_extra
        ):
            raise StorageValidationError(
                f"view bundle ZIP member header is not canonical: {info.filename}"
            )
        data_end = (
            info.header_offset
            + 30
            + len(local_name)
            + len(local_extra)
            + info.compress_size
        )
        next_offset = (
            infos[index + 1].header_offset
            if index + 1 < len(infos)
            else envelope.central_offset
        )
        if data_end != next_offset:
            raise StorageValidationError(
                f"view bundle ZIP member has non-canonical padding: {info.filename}"
            )
    if central_size != envelope.central_size:
        raise StorageValidationError(
            "view bundle ZIP central directory is not canonical"
        )


def _manifest_from_bytes(
    payload: bytes,
    *,
    max_files: int,
    max_bytes: int,
) -> tuple[str, tuple[ArtifactMember, ...]]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise StorageValidationError(
                    f"view bundle metadata contains duplicate key: {key!r}"
                )
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise StorageValidationError(
            f"view bundle metadata contains non-finite number: {value}"
        )

    def bounded_integer(value: str) -> int:
        digits = value[1:] if value.startswith("-") else value
        if len(digits) > 20:
            raise StorageValidationError(
                "view bundle metadata integer exceeds the 64-bit format bound"
            )
        return int(value)

    def invalid_float(_value: str) -> None:
        raise StorageValidationError("view bundle metadata does not allow floats")

    try:
        parsed = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=invalid_constant,
            parse_float=invalid_float,
            parse_int=bounded_integer,
        )
    except StorageValidationError:
        raise
    except UnicodeDecodeError as exc:
        raise StorageValidationError("view bundle metadata is not UTF-8") from exc
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise StorageValidationError("view bundle metadata is not valid JSON") from exc
    if not isinstance(parsed, dict) or set(parsed) != {"schema", "view_type", "files"}:
        raise StorageValidationError("view bundle metadata has an invalid shape")
    if parsed["schema"] != VIEW_BUNDLE_SCHEMA:
        raise StorageValidationError("view bundle metadata has an unsupported schema")
    view_type = _validate_view_type(parsed["view_type"])
    raw_files = parsed["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise StorageValidationError("view bundle inventory must be a nonempty list")
    if len(raw_files) > max_files:
        raise StorageValidationError(f"view bundle exceeds {max_files} files")

    members: list[ArtifactMember] = []
    total_bytes = 0
    for raw in raw_files:
        if not isinstance(raw, dict) or set(raw) != {"path", "size", "sha256", "mode"}:
            raise StorageValidationError(
                "view bundle inventory record has an invalid shape"
            )
        path = _canonical_member_path(raw["path"])
        size = raw["size"]
        mode = raw["mode"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise StorageValidationError("view bundle member size is invalid")
        if (
            isinstance(mode, bool)
            or not isinstance(mode, int)
            or mode not in {0o644, 0o755}
        ):
            raise StorageValidationError("view bundle member mode must be 0644 or 0755")
        member = ArtifactMember(
            path=path,
            digest=raw["sha256"],
            byte_size=size,
            mode=mode,
        )
        members.append(member)
        total_bytes += size
        if total_bytes > max_bytes:
            raise StorageValidationError(
                f"view bundle exceeds {max_bytes} expanded bytes"
            )
    result = tuple(members)
    _validate_member_paths(result, max_files=max_files)
    try:
        canonical = _manifest_bytes(view_type, result)
    except RecursionError as exc:
        raise StorageValidationError(
            "view bundle metadata is too deeply nested"
        ) from exc
    if payload != canonical:
        raise StorageValidationError("view bundle metadata is not canonical JSON")
    return view_type, result


def _verify_archive_handle(
    handle: BinaryIO,
    path: Path,
    *,
    expected_view_type: str | None,
    archive_digest: str,
    archive_size: int,
    max_files: int,
    max_bytes: int,
    max_metadata_bytes: int,
    extraction_root_descriptor: int | None = None,
) -> tuple[ViewBundleRecord, bytes]:
    handle.seek(0)
    envelope = _read_zip_envelope(
        handle,
        max_files=max_files,
        max_metadata_bytes=max_metadata_bytes,
    )
    try:
        with zipfile.ZipFile(handle, mode="r") as archive:
            if archive.comment:
                raise StorageValidationError("view bundle ZIP comment is not canonical")
            infos = archive.infolist()
            if (
                len(infos) != envelope.member_count
                or archive.start_dir != envelope.central_offset
            ):
                raise StorageValidationError(
                    "view bundle ZIP directory does not match its canonical envelope"
                )
            if len(infos) > max_files + 1:
                raise StorageValidationError("view bundle has too many ZIP members")
            if not infos or infos[0].filename != VIEW_BUNDLE_MANIFEST:
                raise StorageValidationError("view bundle must begin with bundle.json")
            if infos[0].header_offset != 0:
                raise StorageValidationError("view bundle must not have a ZIP preamble")
            _validate_preliminary_zip_members(infos)
            _validate_canonical_zip_layout(handle, infos, envelope)

            metadata_info = infos[0]
            if metadata_info.file_size > max_metadata_bytes:
                raise StorageValidationError(
                    f"view bundle metadata exceeds {max_metadata_bytes} bytes"
                )
            manifest_bytes = _read_zip_member(
                archive,
                metadata_info,
                max_bytes=max_metadata_bytes,
            )
            view_type, members = _manifest_from_bytes(
                manifest_bytes,
                max_files=max_files,
                max_bytes=max_bytes,
            )
            if expected_view_type is not None and view_type != expected_view_type:
                raise StorageIntegrityError(
                    "view bundle type does not match the expected view"
                )
            expected_names = [VIEW_BUNDLE_MANIFEST] + [
                f"{VIEW_BUNDLE_PAYLOAD}/{member.path}" for member in members
            ]
            names = [info.filename for info in infos]
            if names != expected_names:
                raise StorageValidationError(
                    "view bundle ZIP members do not exactly match its inventory"
                )
            _validate_canonical_zip_info(metadata_info, 0o644, guarded=False)

            if extraction_root_descriptor is not None:
                _write_extraction_bytes(
                    extraction_root_descriptor,
                    (VIEW_BUNDLE_MANIFEST,),
                    manifest_bytes,
                    0o644,
                )
            streamed_total = 0
            payload_pairs = zip(infos[1:], members, strict=True)
            for index, (info, member) in enumerate(payload_pairs):
                _validate_canonical_zip_info(
                    info,
                    member.mode,
                    guarded=index == len(members) - 1,
                )
                if info.file_size != member.byte_size:
                    raise StorageIntegrityError(
                        f"view bundle member size mismatch: {member.path}"
                    )
                target_parts = (
                    (VIEW_BUNDLE_PAYLOAD, *PurePosixPath(member.path).parts)
                    if extraction_root_descriptor is not None
                    else None
                )
                observed_digest, observed_size = _stream_zip_member(
                    archive,
                    info,
                    extraction_root_descriptor=extraction_root_descriptor,
                    target_parts=target_parts,
                    mode=member.mode,
                    max_member_bytes=member.byte_size,
                    max_total_bytes=max_bytes,
                    total_before=streamed_total,
                )
                streamed_total += observed_size
                if (
                    observed_size != member.byte_size
                    or observed_digest != member.digest
                ):
                    raise StorageIntegrityError(
                        f"view bundle member failed inventory verification: {member.path}"
                    )
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise StorageValidationError(f"view bundle is not a valid ZIP: {path}") from exc

    return (
        ViewBundleRecord(
            path=path,
            view_type=view_type,
            digest=archive_digest,
            byte_size=archive_size,
            members=members,
        ),
        manifest_bytes,
    )


def _validate_preliminary_zip_members(infos: list[zipfile.ZipInfo]) -> None:
    seen: set[str] = set()
    previous_offset = -1
    for info in infos:
        if info.orig_filename != info.filename or "\x00" in info.orig_filename:
            raise StorageValidationError("view bundle ZIP member contains a NUL path")
        name = info.filename
        if name in seen:
            raise StorageValidationError(f"duplicate view bundle ZIP member: {name}")
        seen.add(name)
        if info.header_offset <= previous_offset:
            raise StorageValidationError(
                "view bundle ZIP member order is not canonical"
            )
        previous_offset = info.header_offset
        if info.is_dir() or name.endswith("/"):
            raise StorageValidationError("view bundle ZIP must not contain directories")
        if name != VIEW_BUNDLE_MANIFEST:
            prefix = f"{VIEW_BUNDLE_PAYLOAD}/"
            if not name.startswith(prefix):
                raise StorageValidationError(
                    f"view bundle ZIP contains an extra member: {name!r}"
                )
            _canonical_member_path(name[len(prefix) :])
        if info.flag_bits & 0x1:
            raise StorageValidationError(f"view bundle ZIP member is encrypted: {name}")
        expected_flags = 0 if name.isascii() else 0x800
        if info.flag_bits != expected_flags:
            raise StorageValidationError(
                f"view bundle ZIP member has non-canonical flags: {name}"
            )
        if info.compress_type != zipfile.ZIP_STORED:
            raise StorageValidationError(
                f"view bundle ZIP member is compressed: {name}"
            )
        if info.compress_size != info.file_size:
            raise StorageValidationError(
                f"view bundle ZIP member stored size is inconsistent: {name}"
            )
        if info.comment:
            raise StorageValidationError(
                f"view bundle ZIP member has non-canonical metadata: {name}"
            )


def _validate_canonical_zip_info(
    info: zipfile.ZipInfo,
    mode: int,
    *,
    guarded: bool,
) -> None:
    expected_extra = _canonical_central_extra(info, guarded=guarded)
    local_zip64 = info.file_size * 1.05 > _ZIP64_LIMIT
    expected_version = (
        45
        if local_zip64 or expected_extra != (_ZIP_GUARD_EXTRA if guarded else b"")
        else 20
    )
    if info.date_time != _FIXED_ZIP_TIME:
        raise StorageValidationError(
            f"view bundle ZIP member has a non-canonical timestamp: {info.filename}"
        )
    if (
        info.create_system != 3
        or info.create_version != expected_version
        or info.extract_version != expected_version
        or info.reserved != 0
        or info.volume != 0
        or info.internal_attr != 0
        or info.external_attr != (stat.S_IFREG | mode) << 16
        or info.extra != expected_extra
    ):
        raise StorageValidationError(
            f"view bundle ZIP member has a non-canonical mode: {info.filename}"
        )


def _read_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    max_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    written = 0
    with archive.open(info, mode="r") as source:
        while chunk := source.read(_COPY_CHUNK_BYTES):
            written += len(chunk)
            if written > info.file_size or written > max_bytes:
                raise StorageValidationError(
                    f"view bundle ZIP member exceeded its byte limit: {info.filename}"
                )
            chunks.append(chunk)
    if written != info.file_size:
        raise StorageIntegrityError(
            f"view bundle ZIP member size changed while reading: {info.filename}"
        )
    return b"".join(chunks)


def _stream_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    extraction_root_descriptor: int | None,
    target_parts: tuple[str, ...] | None,
    mode: int,
    max_member_bytes: int,
    max_total_bytes: int,
    total_before: int,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    written = 0
    if extraction_root_descriptor is None:
        destination_context = _optional_binary_writer(None)
    else:
        if target_parts is None:
            raise StorageIntegrityError("view bundle extraction target is missing")
        destination_context = _open_extraction_file(
            extraction_root_descriptor,
            target_parts,
            mode,
        )
    with destination_context as destination:
        with archive.open(info, mode="r") as source:
            while chunk := source.read(_COPY_CHUNK_BYTES):
                written += len(chunk)
                if (
                    written > info.file_size
                    or written > max_member_bytes
                    or total_before + written > max_total_bytes
                ):
                    raise StorageValidationError(
                        f"view bundle ZIP member exceeded its byte limit: {info.filename}"
                    )
                digest.update(chunk)
                if destination is not None:
                    destination.write(chunk)
        if written != info.file_size:
            raise StorageIntegrityError(
                f"view bundle ZIP member size changed while reading: {info.filename}"
            )
        if destination is not None:
            _flush_extraction_file(destination, mode)
    return digest.hexdigest(), written


@contextmanager
def _optional_binary_writer(
    writer: BinaryIO | None,
) -> Iterator[BinaryIO | None]:
    yield writer


@contextmanager
def _open_extraction_root(
    stage: Path,
    expected_identity: tuple[int, ...],
) -> Iterator[int]:
    """Hold the original stage inode while every extraction write is relative."""

    metadata = stage.lstat()
    if (
        _is_link_or_reparse(metadata)
        or not stat.S_ISDIR(metadata.st_mode)
        or _directory_inode_identity(metadata) != expected_identity
    ):
        raise StorageIntegrityError("view bundle materialization stage changed")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(stage, flags)
    try:
        opened = os.fstat(descriptor)
        if _directory_inode_identity(opened) != expected_identity:
            raise StorageIntegrityError(
                "view bundle materialization stage changed while opening"
            )
        yield descriptor
        if _directory_inode_identity(os.fstat(descriptor)) != expected_identity:
            raise StorageIntegrityError(
                "view bundle materialization stage changed during extraction"
            )
    finally:
        os.close(descriptor)


@contextmanager
def _open_extraction_file(
    root_descriptor: int,
    parts: tuple[str, ...],
    mode: int,
) -> Iterator[BinaryIO]:
    """Create one file beneath an already-open stage without following paths."""

    if not parts or any(part in {"", ".", ".."} or "/" in part for part in parts):
        raise StorageValidationError("view bundle extraction path is unsafe")
    root_metadata = os.fstat(root_descriptor)
    root_device = root_metadata.st_dev
    descriptor = os.dup(root_descriptor)
    try:
        for part in parts[:-1]:
            created = False
            try:
                metadata = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    pass
                metadata = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if (
                _is_link_or_reparse(metadata)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_dev != root_device
            ):
                raise StorageIntegrityError(
                    "view bundle extraction directory changed or crosses a device"
                )
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            child = os.open(part, flags, dir_fd=descriptor)
            opened = os.fstat(child)
            if (
                _directory_inode_identity(opened) != _directory_inode_identity(metadata)
                or opened.st_dev != root_device
            ):
                os.close(child)
                raise StorageIntegrityError(
                    "view bundle extraction directory changed while opening"
                )
            if created:
                os.fsync(descriptor)
            os.close(descriptor)
            descriptor = child

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        flags |= getattr(os, "O_BINARY", 0)
        file_descriptor = os.open(
            parts[-1],
            flags,
            mode,
            dir_fd=descriptor,
        )
        try:
            opened_file = os.fstat(file_descriptor)
            if (
                _is_link_or_reparse(opened_file)
                or not stat.S_ISREG(opened_file.st_mode)
                or opened_file.st_dev != root_device
            ):
                raise StorageIntegrityError(
                    "view bundle extraction file changed while opening"
                )
            with os.fdopen(file_descriptor, "wb") as handle:
                file_descriptor = -1
                yield handle
            os.fsync(descriptor)
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _write_extraction_bytes(
    root_descriptor: int,
    parts: tuple[str, ...],
    payload: bytes,
    mode: int,
) -> None:
    with _open_extraction_file(root_descriptor, parts, mode) as handle:
        handle.write(payload)
        _flush_extraction_file(handle, mode)


def _flush_extraction_file(handle: BinaryIO, mode: int) -> None:
    os.fchmod(handle.fileno(), mode)
    handle.flush()
    os.fsync(handle.fileno())


def _scan_source_tree(
    root: Path,
    *,
    include_digests: bool,
    max_files: int,
    max_bytes: int,
    preserve_modes: bool = False,
    required_device: int | None = None,
    reject_mounts: bool = False,
) -> _SourceSnapshot:
    mount_points = _linux_mount_points() if reject_mounts else frozenset()
    if _SAFE_DIRECTORY_FDS:
        return _scan_source_tree_at(
            root,
            include_digests=include_digests,
            max_files=max_files,
            max_bytes=max_bytes,
            preserve_modes=preserve_modes,
            required_device=required_device,
            reject_mounts=reject_mounts,
            mount_points=mount_points,
        )
    return _scan_source_tree_by_path(
        root,
        include_digests=include_digests,
        max_files=max_files,
        max_bytes=max_bytes,
        preserve_modes=preserve_modes,
        required_device=required_device,
        reject_mounts=reject_mounts,
        mount_points=mount_points,
    )


def _scan_source_tree_at(
    root: Path,
    *,
    include_digests: bool,
    max_files: int,
    max_bytes: int,
    preserve_modes: bool,
    required_device: int | None,
    reject_mounts: bool,
    mount_points: frozenset[str],
) -> _SourceSnapshot:
    directories: dict[tuple[str, ...], tuple[int, ...]] = {}
    files: list[_ScannedFile] = []
    total_bytes = 0
    visited_entries = 0
    maximum_entries = 2 * max_files + 1
    with _open_directory_path(root, label="view bundle source") as root_descriptor:
        assert root_descriptor is not None
        root_signature = _path_signature(os.fstat(root_descriptor))
        if required_device is not None and root_signature[0] != required_device:
            raise StorageValidationError(
                "view bundle source crosses a filesystem boundary"
            )
        if reject_mounts and _path_is_mount_point(root, mount_points=mount_points):
            raise StorageValidationError("view bundle source contains a mount point")

        def visit(descriptor: int, parts: tuple[str, ...]) -> None:
            nonlocal total_bytes, visited_entries
            before = _path_signature(os.fstat(descriptor))
            directories[parts] = before
            if len(directories) > max_files + 1:
                raise StorageValidationError(
                    f"view bundle source exceeds {max_files + 1} directories"
                )
            try:
                names = _bounded_directory_names(
                    descriptor,
                    remaining=maximum_entries - visited_entries,
                    label=str(root.joinpath(*parts)),
                )
            except OSError as exc:
                raise StorageIntegrityError(
                    f"view bundle source directory could not be listed: {root}"
                ) from exc
            visited_entries += len(names)
            for name in names:
                relative_parts = (*parts, name)
                relative = _canonical_member_path(
                    PurePosixPath(*relative_parts).as_posix()
                )
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if required_device is not None and metadata.st_dev != required_device:
                    raise StorageValidationError(
                        f"view bundle source crosses a filesystem boundary: {relative}"
                    )
                if reject_mounts and _path_is_mount_point(
                    root.joinpath(*relative_parts),
                    mount_points=mount_points,
                ):
                    raise StorageValidationError(
                        f"view bundle source contains a mount point: {relative}"
                    )
                if _is_link_or_reparse(metadata):
                    raise StorageValidationError(
                        f"view bundle source contains a symbolic link: {relative}"
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    with _open_child_directory(
                        descriptor,
                        root.joinpath(*parts),
                        name,
                    ) as child:
                        visit(child, relative_parts)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise StorageValidationError(
                        f"view bundle source contains a special file: {relative}"
                    )
                next_total = total_bytes + metadata.st_size
                if len(files) >= max_files:
                    raise StorageValidationError(
                        f"view bundle exceeds {max_files} files"
                    )
                if next_total > max_bytes:
                    raise StorageValidationError(
                        f"view bundle exceeds {max_bytes} expanded bytes"
                    )
                file_signature = _path_signature(metadata)
                digest = (
                    _hash_regular_at(descriptor, name, file_signature, relative)
                    if include_digests
                    else None
                )
                mode = (
                    metadata.st_mode & 0o7777
                    if preserve_modes
                    else 0o755 if metadata.st_mode & 0o111 else 0o644
                )
                files.append(
                    _ScannedFile(
                        path=relative,
                        byte_size=metadata.st_size,
                        mode=mode,
                        signature=file_signature,
                        digest=digest,
                    )
                )
                total_bytes = next_total
            if _path_signature(os.fstat(descriptor)) != before:
                raise StorageIntegrityError(
                    f"view bundle source directory changed while scanning: {root}"
                )

        visit(root_descriptor, ())
    _require_path_signature(root, root_signature, label="view bundle source")
    files.sort(key=lambda item: item.path)
    return _SourceSnapshot(
        root=root,
        root_signature=root_signature,
        directories=directories,
        files=tuple(files),
    )


def _scan_source_tree_by_path(
    root: Path,
    *,
    include_digests: bool,
    max_files: int,
    max_bytes: int,
    preserve_modes: bool,
    required_device: int | None,
    reject_mounts: bool,
    mount_points: frozenset[str],
) -> _SourceSnapshot:
    directories: dict[tuple[str, ...], tuple[int, ...]] = {}
    files: list[_ScannedFile] = []
    total_bytes = 0
    visited_entries = 0
    maximum_entries = 2 * max_files + 1
    root_signature = _path_signature(root.lstat())
    if required_device is not None and root_signature[0] != required_device:
        raise StorageValidationError("view bundle source crosses a filesystem boundary")
    if reject_mounts and _path_is_mount_point(root, mount_points=mount_points):
        raise StorageValidationError("view bundle source contains a mount point")

    def visit(directory: Path, parts: tuple[str, ...]) -> None:
        nonlocal total_bytes, visited_entries
        before = _path_signature(directory.lstat())
        directories[parts] = before
        if len(directories) > max_files + 1:
            raise StorageValidationError(
                f"view bundle source exceeds {max_files + 1} directories"
            )
        try:
            names = _bounded_directory_names(
                directory,
                remaining=maximum_entries - visited_entries,
                label=str(directory),
            )
        except OSError as exc:
            raise StorageIntegrityError(
                f"view bundle source directory could not be listed: {directory}"
            ) from exc
        visited_entries += len(names)
        for name in names:
            candidate = directory / name
            relative_parts = (*parts, candidate.name)
            relative = _canonical_member_path(PurePosixPath(*relative_parts).as_posix())
            metadata = candidate.lstat()
            if required_device is not None and metadata.st_dev != required_device:
                raise StorageValidationError(
                    f"view bundle source crosses a filesystem boundary: {relative}"
                )
            if reject_mounts and _path_is_mount_point(
                candidate,
                mount_points=mount_points,
            ):
                raise StorageValidationError(
                    f"view bundle source contains a mount point: {relative}"
                )
            if _is_link_or_reparse(metadata):
                raise StorageValidationError(
                    f"view bundle source contains a symbolic link: {relative}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                visit(candidate, relative_parts)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise StorageValidationError(
                    f"view bundle source contains a special file: {relative}"
                )
            next_total = total_bytes + metadata.st_size
            if len(files) >= max_files:
                raise StorageValidationError(f"view bundle exceeds {max_files} files")
            if next_total > max_bytes:
                raise StorageValidationError(
                    f"view bundle exceeds {max_bytes} expanded bytes"
                )
            signature = _path_signature(metadata)
            digest = (
                _hash_regular_path(candidate, signature, relative)
                if include_digests
                else None
            )
            mode = (
                metadata.st_mode & 0o7777
                if preserve_modes
                else 0o755 if metadata.st_mode & 0o111 else 0o644
            )
            files.append(
                _ScannedFile(
                    path=relative,
                    byte_size=metadata.st_size,
                    mode=mode,
                    signature=signature,
                    digest=digest,
                )
            )
            total_bytes = next_total
        if _path_signature(directory.lstat()) != before:
            raise StorageIntegrityError(
                f"view bundle source directory changed while scanning: {directory}"
            )

    visit(root, ())
    _require_path_signature(root, root_signature, label="view bundle source")
    files.sort(key=lambda item: item.path)
    return _SourceSnapshot(
        root=root,
        root_signature=root_signature,
        directories=directories,
        files=tuple(files),
    )


def _bounded_directory_names(
    directory: int | Path,
    *,
    remaining: int,
    label: str,
) -> list[str]:
    """Collect one directory only while the global source budget remains."""

    if remaining < 0:
        raise StorageValidationError("view bundle source exceeds its entry budget")
    names: list[str] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if len(names) >= remaining:
                raise StorageValidationError(
                    f"view bundle source exceeds its entry budget while listing: {label}"
                )
            names.append(entry.name)
    names.sort()
    return names


@contextmanager
def _open_source_root(snapshot: _SourceSnapshot) -> Iterator[int | None]:
    if not _SAFE_DIRECTORY_FDS:
        _require_path_signature(
            snapshot.root,
            snapshot.root_signature,
            label="view bundle source",
        )
        yield None
        return
    with _open_directory_path(snapshot.root, label="view bundle source") as descriptor:
        assert descriptor is not None
        if _path_signature(os.fstat(descriptor)) != snapshot.root_signature:
            raise StorageIntegrityError("view bundle source changed before packing")
        yield descriptor


def _copy_source_file(
    snapshot: _SourceSnapshot,
    root_descriptor: int | None,
    scanned: _ScannedFile,
    target: BinaryIO,
) -> None:
    with _open_snapshot_file(snapshot, root_descriptor, scanned) as source:
        signature = _path_signature(os.fstat(source.fileno()))
        observed_digest, observed_size = _consume_exact_file(
            source,
            scanned.byte_size,
            label=f"view bundle source file {scanned.path!r}",
            target=target,
        )
        if _path_signature(os.fstat(source.fileno())) != signature:
            raise StorageIntegrityError(
                f"view bundle source changed while packing: {scanned.path}"
            )
    if observed_size != scanned.byte_size or observed_digest != scanned.digest:
        raise StorageIntegrityError(
            f"view bundle source changed between scan and pack: {scanned.path}"
        )


@contextmanager
def _open_snapshot_file(
    snapshot: _SourceSnapshot,
    root_descriptor: int | None,
    scanned: _ScannedFile,
) -> Iterator[BinaryIO]:
    parts = PurePosixPath(scanned.path).parts
    if root_descriptor is None:
        current = snapshot.root
        for length, part in enumerate(parts[:-1], 1):
            current = current / part
            _require_path_signature(
                current,
                snapshot.directories[parts[:length]],
                label="view bundle source directory",
            )
        path = snapshot.root.joinpath(*parts)
        with _open_stable_regular_file(path, label="view bundle source file") as (
            _resolved,
            handle,
            signature,
        ):
            if signature != scanned.signature:
                raise StorageIntegrityError(
                    f"view bundle source changed before packing: {scanned.path}"
                )
            yield handle
        return

    descriptor = os.dup(root_descriptor)
    try:
        for length, part in enumerate(parts[:-1], 1):
            expected = snapshot.directories[parts[:length]]
            metadata = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if _path_signature(metadata) != expected or _is_link_or_reparse(metadata):
                raise StorageIntegrityError(
                    f"view bundle source directory changed: {scanned.path}"
                )
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            child = os.open(part, flags, dir_fd=descriptor)
            if _path_signature(os.fstat(child)) != expected:
                os.close(child)
                raise StorageIntegrityError(
                    f"view bundle source directory changed: {scanned.path}"
                )
            os.close(descriptor)
            descriptor = child
        metadata = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        if _path_signature(metadata) != scanned.signature or _is_link_or_reparse(
            metadata
        ):
            raise StorageIntegrityError(
                f"view bundle source changed before packing: {scanned.path}"
            )
        flags = _regular_read_flags()
        file_descriptor = os.open(parts[-1], flags, dir_fd=descriptor)
        try:
            opened = os.fstat(file_descriptor)
            if (
                _is_link_or_reparse(opened)
                or not stat.S_ISREG(opened.st_mode)
                or _path_signature(opened) != scanned.signature
            ):
                raise StorageIntegrityError(
                    f"view bundle source changed before packing: {scanned.path}"
                )
            with os.fdopen(file_descriptor, "rb") as handle:
                file_descriptor = -1
                yield handle
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _require_source_snapshot(
    snapshot: _SourceSnapshot,
    *,
    max_files: int,
    max_bytes: int,
) -> None:
    observed = _scan_source_tree(
        snapshot.root,
        include_digests=False,
        max_files=max_files,
        max_bytes=max_bytes,
    )
    expected_files = [
        (item.path, item.byte_size, item.mode, item.signature)
        for item in snapshot.files
    ]
    observed_files = [
        (item.path, item.byte_size, item.mode, item.signature)
        for item in observed.files
    ]
    if (
        observed.root_signature != snapshot.root_signature
        or observed.directories != snapshot.directories
        or observed_files != expected_files
    ):
        raise StorageIntegrityError("view bundle source changed while packing")


@contextmanager
def _open_directory_path(path: Path, *, label: str) -> Iterator[int | None]:
    metadata = path.lstat()
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise StorageValidationError(f"{label} is not a real directory: {path}")
    if not _SAFE_DIRECTORY_FDS:
        yield None
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        if _path_signature(os.fstat(descriptor)) != _path_signature(metadata):
            raise StorageIntegrityError(f"{label} changed while opening: {path}")
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_child_directory(
    parent_descriptor: int,
    parent_path: Path,
    name: str,
) -> Iterator[int]:
    metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    path = parent_path / name
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise StorageValidationError(
            f"view bundle source contains a non-directory: {path}"
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        if _path_signature(os.fstat(descriptor)) != _path_signature(metadata):
            raise StorageIntegrityError(
                f"view bundle source directory changed while opening: {path}"
            )
        yield descriptor
    finally:
        os.close(descriptor)


def _hash_regular_at(
    parent_descriptor: int,
    name: str,
    expected_signature: tuple[int, ...],
    relative: str,
) -> str:
    flags = _regular_read_flags()
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if (
            _is_link_or_reparse(opened)
            or not stat.S_ISREG(opened.st_mode)
            or _path_signature(opened) != expected_signature
        ):
            raise StorageIntegrityError(
                f"view bundle source changed while opening: {relative}"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            digest, _size = _consume_exact_file(
                handle,
                expected_signature[3],
                label=f"view bundle source file {relative!r}",
            )
            if _path_signature(os.fstat(handle.fileno())) != expected_signature:
                raise StorageIntegrityError(
                    f"view bundle source changed while hashing: {relative}"
                )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return digest


def _hash_regular_path(
    path: Path,
    expected_signature: tuple[int, ...],
    relative: str,
) -> str:
    with _open_stable_regular_file(path, label="view bundle source file") as (
        _resolved,
        handle,
        signature,
    ):
        if signature != expected_signature:
            raise StorageIntegrityError(
                f"view bundle source changed while opening: {relative}"
            )
        digest, _size = _consume_exact_file(
            handle,
            expected_signature[3],
            label=f"view bundle source file {relative!r}",
        )
    return digest


def _regular_read_flags() -> int:
    flags = os.O_RDONLY
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_BINARY"):
        flags |= getattr(os, name, 0)
    return flags


def _consume_exact_file(
    handle: BinaryIO,
    expected_size: int,
    *,
    label: str,
    target: BinaryIO | None = None,
) -> tuple[str, int]:
    """Hash/copy exactly the snapshotted bytes and reject shrink or growth."""

    digest = hashlib.sha256()
    remaining = expected_size
    observed = 0
    while remaining:
        chunk = handle.read(min(_COPY_CHUNK_BYTES, remaining))
        if not chunk:
            raise StorageIntegrityError(
                f"{label} shrank while reading ({observed} of {expected_size} bytes)"
            )
        digest.update(chunk)
        observed += len(chunk)
        remaining -= len(chunk)
        if target is not None:
            target.write(chunk)
    if handle.read(1):
        raise StorageIntegrityError(f"{label} grew while reading")
    return digest.hexdigest(), observed


@contextmanager
def _open_stable_regular_file(
    path: str | Path,
    *,
    label: str,
) -> Iterator[tuple[Path, BinaryIO, tuple[int, ...]]]:
    resolved = _regular_file_path(path, label=label)
    metadata = resolved.lstat()
    signature = _path_signature(metadata)
    flags = _regular_read_flags()
    descriptor = os.open(resolved, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            _is_link_or_reparse(opened)
            or not stat.S_ISREG(opened.st_mode)
            or _path_signature(opened) != signature
        ):
            raise StorageIntegrityError(f"{label} changed while opening: {resolved}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            yield resolved, handle, signature
            if _path_signature(os.fstat(handle.fileno())) != signature:
                raise StorageIntegrityError(
                    f"{label} changed while reading: {resolved}"
                )
        _require_path_signature(resolved, signature, label=label)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _regular_file_path(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.name in {"", ".", ".."}:
        raise StorageValidationError(f"{label} has no safe final component")
    resolved = candidate.parent.resolve() / candidate.name
    try:
        metadata = resolved.lstat()
    except FileNotFoundError as exc:
        raise StorageValidationError(f"{label} does not exist: {resolved}") from exc
    if _is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise StorageValidationError(f"{label} is not a real file: {resolved}")
    return resolved


def _real_directory(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.name in {"", ".", ".."}:
        raise StorageValidationError(f"{label} has no safe final component")
    resolved = candidate.parent.resolve() / candidate.name
    try:
        metadata = resolved.lstat()
    except FileNotFoundError as exc:
        raise StorageValidationError(f"{label} does not exist: {resolved}") from exc
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise StorageValidationError(f"{label} is not a real directory: {resolved}")
    return resolved


def _output_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.name in {"", ".", ".."}:
        raise StorageValidationError("view bundle output has no safe final component")
    return candidate.parent.resolve(strict=False) / candidate.name


def _validate_bundle_file_target(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if _is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise StorageValidationError(
            f"view bundle output is not a regular file: {path}"
        )


def _file_inode_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        getattr(metadata, "st_file_attributes", 0),
    )


def _file_boundary_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Identity stable across rename but sensitive to content/mode mutation."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        getattr(metadata, "st_file_attributes", 0),
    )


def _require_open_bundle_path(
    handle: BinaryIO,
    path: Path,
    *,
    expected_signature: tuple[int, ...],
    expected_digest: str,
    expected_size: int,
    label: str,
) -> None:
    opened_signature = _path_signature(os.fstat(handle.fileno()))
    if opened_signature != expected_signature:
        raise StorageIntegrityError(f"{label} changed through its open descriptor")
    with _open_stable_regular_file(path, label=label) as (
        _resolved,
        observed_handle,
        observed_signature,
    ):
        if observed_signature != expected_signature:
            raise StorageIntegrityError(f"{label} path no longer names the open file")
        digest, size = _consume_exact_file(
            observed_handle,
            expected_size,
            label=label,
        )
    if digest != expected_digest or size != expected_size:
        raise StorageIntegrityError(f"{label} failed its publication digest check")


def _reserve_file_slot(parent: Path, name: str, *, suffix: str) -> Path:
    descriptor, reserved_name = tempfile.mkstemp(
        prefix=f".{name}.{suffix}-",
        dir=str(parent),
    )
    reserved = Path(reserved_name)
    try:
        os.close(descriptor)
        descriptor = -1
        reserved.unlink()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return reserved


def _quarantine_bundle_file(destination: Path) -> Path | None:
    try:
        destination.lstat()
    except FileNotFoundError:
        return None
    quarantine = _reserve_file_slot(
        destination.parent,
        destination.name,
        suffix="quarantine",
    )
    os.replace(destination, quarantine)
    return quarantine


def _restore_previous_bundle_file(
    backup: Path,
    destination: Path,
    *,
    previous_descriptor: int,
    previous_signature: tuple[int, ...],
    destination_was_missing: bool,
) -> None:
    # Never overwrite an object that appeared while the destination name was
    # vacant.  Moving it aside preserves both the unexpected object and the
    # previous output even if the subsequent restore fails.
    _quarantine_bundle_file(destination)
    os.replace(backup, destination)
    restored = destination.lstat()
    opened = os.fstat(previous_descriptor)
    if (
        _is_link_or_reparse(restored)
        or not stat.S_ISREG(restored.st_mode)
        or _file_boundary_identity(restored) != _file_boundary_identity(opened)
        or _file_boundary_identity(opened) != previous_signature
    ):
        raise RuntimeError(
            f"previous view bundle output changed during rollback: {destination}"
        )
    if destination_was_missing:
        destination.unlink()


def _recover_bundle_file_publication(
    backup: Path,
    destination: Path,
    *,
    previous_descriptor: int,
    previous_signature: tuple[int, ...],
    destination_was_missing: bool,
) -> Path | None:
    try:
        quarantine = _quarantine_bundle_file(destination)
    except OSError as exc:
        raise RuntimeError(
            "view bundle publication failed; suspect output remains at "
            f"{destination} and previous output remains at {backup}"
        ) from exc
    try:
        _restore_previous_bundle_file(
            backup,
            destination,
            previous_descriptor=previous_descriptor,
            previous_signature=previous_signature,
            destination_was_missing=destination_was_missing,
        )
    except Exception as exc:
        raise RuntimeError(
            "view bundle publication failed and previous output could not be "
            f"restored from {backup}; suspect output is at {quarantine}"
        ) from exc
    return quarantine


def _is_expected_previous_bundle_file(
    moved: os.stat_result,
    opened: os.stat_result,
    expected_identity: tuple[int, ...],
) -> bool:
    return (
        not _is_link_or_reparse(moved)
        and stat.S_ISREG(moved.st_mode)
        and _file_boundary_identity(moved) == _file_boundary_identity(opened)
        and _file_boundary_identity(opened) == expected_identity
    )


def _retain_previous_bundle_file(
    backup: Path,
    *,
    previous_descriptor: int,
    previous_signature: tuple[int, ...],
) -> None:
    """Validate and retain the initial previous-file backup as an orphan.

    The initial destination-to-``.previous-*`` rename is the only handoff.
    Successful publication never reserves another name, renames this path, or
    unlinks it.  A later ownership-aware GC pass may reclaim the orphan after
    independently validating it.
    """

    try:
        moved = backup.lstat()
        opened = os.fstat(previous_descriptor)
    except OSError as exc:
        raise StorageIntegrityError(
            "view bundle publication committed; cleanup incomplete; previous "
            f"output remains at {backup}"
        ) from exc
    if not _is_expected_previous_bundle_file(moved, opened, previous_signature):
        raise StorageIntegrityError(
            "view bundle publication committed; cleanup incomplete; untrusted "
            f"previous output is preserved at {backup}"
        )
    # There is deliberately no path operation after validation.  Even if
    # another process replaces the backup name now, publication cannot delete
    # either the verified previous inode or the replacement.


def _publish_open_bundle_file(
    handle: BinaryIO,
    temporary: Path,
    destination: Path,
    *,
    expected_signature: tuple[int, ...],
    expected_digest: str,
    expected_size: int,
) -> None:
    """Publish the exact open archive with rollback and suspect quarantine."""

    destination_was_missing = False
    previous_descriptor = -1
    backup: Path | None = None
    try:
        try:
            previous_metadata = destination.lstat()
        except FileNotFoundError:
            destination_was_missing = True
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            try:
                previous_descriptor = os.open(destination, flags, 0o600)
            except FileExistsError as exc:
                raise StorageIntegrityError(
                    "view bundle output appeared during publication"
                ) from exc
            previous_metadata = os.fstat(previous_descriptor)
        else:
            if _is_link_or_reparse(previous_metadata) or not stat.S_ISREG(
                previous_metadata.st_mode
            ):
                raise StorageValidationError(
                    f"view bundle output is not a regular file: {destination}"
                )
            previous_descriptor = os.open(destination, _regular_read_flags())
            opened_previous = os.fstat(previous_descriptor)
            if _path_signature(opened_previous) != _path_signature(previous_metadata):
                raise StorageIntegrityError(
                    "view bundle output changed while opening for publication"
                )
            previous_metadata = opened_previous
        previous_signature = _file_boundary_identity(previous_metadata)

        backup = _reserve_file_slot(
            destination.parent,
            destination.name,
            suffix="previous",
        )
        try:
            os.replace(destination, backup)
        except BaseException:
            if destination_was_missing:
                try:
                    current = destination.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if _file_inode_identity(current) == _file_inode_identity(
                        os.fstat(previous_descriptor)
                    ):
                        destination.unlink()
            raise
        moved = backup.lstat()
        opened_previous = os.fstat(previous_descriptor)
        if (
            _is_link_or_reparse(moved)
            or not stat.S_ISREG(moved.st_mode)
            or _path_signature(moved) != _path_signature(opened_previous)
        ):
            try:
                _restore_previous_bundle_file(
                    backup,
                    destination,
                    previous_descriptor=previous_descriptor,
                    previous_signature=previous_signature,
                    destination_was_missing=destination_was_missing,
                )
            except Exception as exc:
                raise RuntimeError(
                    "view bundle output changed at the publication boundary; "
                    "the moved object was preserved"
                ) from exc
            raise StorageIntegrityError(
                "view bundle output changed at the publication boundary"
            )
        previous_signature = _file_boundary_identity(opened_previous)

        published = False
        try:
            _require_open_bundle_path(
                handle,
                temporary,
                expected_signature=expected_signature,
                expected_digest=expected_digest,
                expected_size=expected_size,
                label="staged view bundle archive",
            )
            # The old destination has already moved to ``backup``.  A hard
            # link publishes without clobbering any object that appeared in
            # the vacant name, and binds the published name to one inode.  The
            # still-open descriptor and digest are checked immediately below.
            os.link(temporary, destination, follow_symlinks=False)
            published = True
            destination_signature = _path_signature(os.fstat(handle.fileno()))
            _require_open_bundle_path(
                handle,
                destination,
                expected_signature=destination_signature,
                expected_digest=expected_digest,
                expected_size=expected_size,
                label="published view bundle archive",
            )
            if _file_inode_identity(os.fstat(handle.fileno()))[
                :3
            ] != _file_inode_identity_from_signature(expected_signature):
                raise StorageIntegrityError(
                    "staged view bundle archive changed during publication"
                )
            moved = backup.lstat()
            if (
                _file_boundary_identity(moved)
                != _file_boundary_identity(os.fstat(previous_descriptor))
                or _file_boundary_identity(moved) != previous_signature
            ):
                raise StorageIntegrityError(
                    "previous view bundle output changed during publication"
                )
        except BaseException as publication_error:
            if published:
                quarantine = _recover_bundle_file_publication(
                    backup,
                    destination,
                    previous_descriptor=previous_descriptor,
                    previous_signature=previous_signature,
                    destination_was_missing=destination_was_missing,
                )
                quarantine_message = (
                    f" at {quarantine}"
                    if quarantine is not None
                    else " because it vanished"
                )
                raise StorageIntegrityError(
                    "published view bundle archive failed identity validation; "
                    f"suspect output was quarantined{quarantine_message}"
                ) from publication_error
            _restore_previous_bundle_file(
                backup,
                destination,
                previous_descriptor=previous_descriptor,
                previous_signature=previous_signature,
                destination_was_missing=destination_was_missing,
            )
            raise
        _retain_previous_bundle_file(
            backup,
            previous_descriptor=previous_descriptor,
            previous_signature=previous_signature,
        )
    finally:
        if previous_descriptor >= 0:
            os.close(previous_descriptor)


def _file_inode_identity_from_signature(signature: tuple[int, ...]) -> tuple[int, ...]:
    return (signature[0], signature[1], stat.S_IFMT(signature[2]))


def _remove_owned_temporary_file(
    path: Path,
    expected_identity: tuple[int, ...],
) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if (
        _is_link_or_reparse(metadata)
        or _file_inode_identity(metadata) != expected_identity
    ):
        return
    cleanup = _reserve_file_slot(path.parent, path.name, suffix="cleanup")
    try:
        os.replace(path, cleanup)
    except OSError:
        return
    try:
        moved = cleanup.lstat()
    except FileNotFoundError:
        return
    if _is_link_or_reparse(moved) or _file_inode_identity(moved) != expected_identity:
        try:
            os.replace(cleanup, path)
        except OSError:
            pass
        return
    try:
        cleanup.unlink()
    except OSError:
        pass


def _same_materialization_semantics(
    left: _MaterializationOwnership,
    right: _MaterializationOwnership,
) -> bool:
    """Compare bundle semantics without reinterpreting filesystem identities.

    PublicationDirectoryReader is already bound to the exact outer ownership
    token.  Its portable root identity intentionally differs in shape from the
    legacy path validator's platform-specific identity, so semantic callbacks
    compare only the authenticated bundle state here.
    """

    return (
        left.state,
        left.manifest_digest,
        left.members,
        left.directory_modes,
    ) == (
        right.state,
        right.manifest_digest,
        right.members,
        right.directory_modes,
    )


def _materialization_entry_policy(
    *,
    max_files: int,
    max_bytes: int,
    max_metadata_bytes: int,
) -> Callable[[str, str, int, int], None]:
    payload_files = 0
    payload_bytes = 0
    payload_directories = 0

    def validate(path: str, kind: str, mode: int, size: int) -> None:
        nonlocal payload_files, payload_bytes, payload_directories
        if path == VIEW_BUNDLE_MANIFEST:
            if kind != "file" or mode != 0o644 or size > max_metadata_bytes:
                raise StorageValidationError(
                    "refusing to replace a non-CodeNib directory"
                )
            return
        if path == VIEW_BUNDLE_PAYLOAD:
            if kind != "directory":
                raise StorageValidationError(
                    "refusing to replace a non-CodeNib directory"
                )
            payload_directories += 1
            return
        prefix = f"{VIEW_BUNDLE_PAYLOAD}/"
        if not path.startswith(prefix):
            raise StorageValidationError("refusing to replace a non-CodeNib directory")
        relative = path[len(prefix) :]
        _canonical_member_path(relative)
        if kind == "directory":
            payload_directories += 1
            if payload_directories > max_files + 1:
                raise StorageValidationError(
                    f"view bundle exceeds {max_files + 1} directories"
                )
            return
        if kind != "file" or mode not in {0o644, 0o755}:
            raise StorageValidationError("refusing to replace a non-CodeNib directory")
        payload_files += 1
        payload_bytes += size
        if payload_files > max_files or payload_bytes > max_bytes:
            raise StorageValidationError("refusing to replace a non-CodeNib directory")

    return validate


def _validate_materialization_reader(
    reader: PublicationDirectoryReader,
    *,
    max_files: int,
    max_bytes: int,
    max_metadata_bytes: int,
) -> tuple[_MaterializationOwnership, object]:
    """Validate materialized bundle semantics through one pinned authority."""

    ownership = reader.capture_ownership(
        allow_empty_root=True,
        entry_policy=_materialization_entry_policy(
            max_files=max_files,
            max_bytes=max_bytes,
            max_metadata_bytes=max_metadata_bytes,
        ),
    )
    inventory = directory_ownership_inventory(ownership)
    root_identity = directory_ownership_root_identity(ownership)
    if not inventory:
        return (
            _MaterializationOwnership(
                "empty",
                root_identity,
                None,
                (),
                (),
            ),
            ownership,
        )

    inventory_by_path = dict(inventory)
    if (
        inventory_by_path.get(VIEW_BUNDLE_MANIFEST) != "file"
        or inventory_by_path.get(VIEW_BUNDLE_PAYLOAD) != "directory"
        or any(
            path not in {VIEW_BUNDLE_MANIFEST, VIEW_BUNDLE_PAYLOAD}
            and not path.startswith(f"{VIEW_BUNDLE_PAYLOAD}/")
            for path in inventory_by_path
        )
    ):
        raise StorageValidationError("refusing to replace a non-CodeNib directory")

    records = {
        record.path: record for record in directory_ownership_file_records(ownership)
    }
    manifest_record = records.get(VIEW_BUNDLE_MANIFEST)
    if (
        manifest_record is None
        or manifest_record.mode != 0o644
        or manifest_record.size > max_metadata_bytes
    ):
        raise StorageValidationError("refusing to replace a non-CodeNib directory")
    metadata_bytes = reader.read_bytes(
        VIEW_BUNDLE_MANIFEST,
        max_bytes=max_metadata_bytes,
    )
    _view_type, members = _manifest_from_bytes(
        metadata_bytes,
        max_files=max_files,
        max_bytes=max_bytes,
    )

    prefix = f"{VIEW_BUNDLE_PAYLOAD}/"
    observed_files = tuple(
        (
            record.path[len(prefix) :],
            record.size,
            record.mode,
            record.sha256,
        )
        for record in directory_ownership_file_records(ownership)
        if record.path.startswith(prefix)
    )
    expected_files = tuple(
        (member.path, member.byte_size, member.mode, member.digest)
        for member in members
    )
    if observed_files != expected_files:
        raise StorageValidationError("refusing to replace a non-CodeNib directory")

    path_trie = _validate_member_paths(members, max_files=max_files)
    directory_modes: list[tuple[str, int]] = []
    for path, kind, identity in directory_ownership_entry_identities(ownership):
        if kind != "directory" or not (
            path == VIEW_BUNDLE_PAYLOAD or path.startswith(prefix)
        ):
            continue
        relative = "" if path == VIEW_BUNDLE_PAYLOAD else path[len(prefix) :]
        parts = () if not relative else tuple(PurePosixPath(relative).parts)
        if not _trie_contains_directory(path_trie, parts):
            raise StorageValidationError("refusing to replace a non-CodeNib directory")
        directory_modes.append((relative, stat.S_IMODE(identity[2])))
    if len(directory_modes) != len(path_trie.directory_nodes):
        raise StorageValidationError("refusing to replace a non-CodeNib directory")
    return (
        _MaterializationOwnership(
            "bundle",
            root_identity,
            manifest_record.sha256,
            members,
            tuple(sorted(directory_modes)),
        ),
        ownership,
    )


def _validate_materialization_target(
    path: Path,
    *,
    max_files: int,
    max_bytes: int,
    max_metadata_bytes: int,
) -> _MaterializationOwnership:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return _MaterializationOwnership("missing", None, None, (), ())
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise StorageValidationError(
            f"view bundle output is not a real directory: {path}"
        )
    if _path_is_mount_point(path):
        raise StorageValidationError(f"refusing to replace a mounted directory: {path}")
    root_device = metadata.st_dev
    entry_names: set[str] = set()
    try:
        with os.scandir(path) as entries:
            for entry_count, entry in enumerate(entries, 1):
                if entry_count > 2:
                    raise StorageValidationError(
                        f"refusing to replace a non-CodeNib directory: {path}"
                    )
                entry_names.add(entry.name)
    except OSError as exc:
        raise StorageValidationError(
            f"refusing to replace an unreadable directory: {path}"
        ) from exc
    if not entry_names:
        final_metadata = path.lstat()
        if _is_link_or_reparse(final_metadata) or _directory_identity(
            final_metadata
        ) != _directory_identity(metadata):
            raise StorageValidationError(
                f"refusing to replace a changed directory: {path}"
            )
        return _MaterializationOwnership(
            "empty",
            _directory_identity(final_metadata),
            None,
            (),
            (),
        )
    if entry_names != {
        VIEW_BUNDLE_MANIFEST,
        VIEW_BUNDLE_PAYLOAD,
    }:
        raise StorageValidationError(
            f"refusing to replace a non-CodeNib directory: {path}"
        )
    manifest = path / VIEW_BUNDLE_MANIFEST
    payload = path / VIEW_BUNDLE_PAYLOAD
    try:
        manifest_metadata = manifest.lstat()
        payload_metadata = payload.lstat()
    except FileNotFoundError as exc:
        raise StorageValidationError(
            f"refusing to replace a non-CodeNib directory: {path}"
        ) from exc
    if (
        _is_link_or_reparse(manifest_metadata)
        or not stat.S_ISREG(manifest_metadata.st_mode)
        or manifest_metadata.st_dev != root_device
        or _path_is_mount_point(manifest)
        or manifest_metadata.st_mode & 0o7777 != 0o644
        or manifest_metadata.st_size > max_metadata_bytes
        or _is_link_or_reparse(payload_metadata)
        or not stat.S_ISDIR(payload_metadata.st_mode)
        or payload_metadata.st_dev != root_device
        or _path_is_mount_point(payload)
    ):
        raise StorageValidationError(
            f"refusing to replace a non-CodeNib directory: {path}"
        )
    try:
        with _open_stable_regular_file(
            manifest,
            label="materialized view bundle metadata",
        ) as (_resolved, handle, manifest_signature):
            if manifest_signature[3] > max_metadata_bytes:
                raise StorageValidationError(
                    f"refusing to replace a non-CodeNib directory: {path}"
                )
            _manifest_digest, _manifest_size = _consume_exact_file(
                handle,
                manifest_signature[3],
                label="materialized view bundle metadata",
            )
            handle.seek(0)
            metadata_bytes = handle.read(manifest_signature[3])
        _view_type, members = _manifest_from_bytes(
            metadata_bytes,
            max_files=max_files,
            max_bytes=max_bytes,
        )
    except (OSError, StorageValidationError, StorageIntegrityError) as exc:
        raise StorageValidationError(
            f"refusing to replace a non-CodeNib directory: {path}"
        ) from exc

    try:
        snapshot = _scan_source_tree(
            payload,
            include_digests=True,
            max_files=max_files,
            max_bytes=max_bytes,
            preserve_modes=True,
            required_device=root_device,
            reject_mounts=True,
        )
    except (OSError, StorageValidationError, StorageIntegrityError) as exc:
        raise StorageValidationError(
            f"refusing to replace a non-CodeNib directory: {path}"
        ) from exc
    expected_files = [
        (member.path, member.byte_size, member.mode, member.digest)
        for member in members
    ]
    observed_files = [
        (item.path, item.byte_size, item.mode, item.digest) for item in snapshot.files
    ]
    path_trie = _validate_member_paths(members, max_files=max_files)
    directories_match = len(snapshot.directories) == len(
        path_trie.directory_nodes
    ) and all(
        _trie_contains_directory(path_trie, parts) for parts in snapshot.directories
    )
    if observed_files != expected_files or not directories_match:
        raise StorageValidationError(
            f"refusing to replace a non-CodeNib directory: {path}"
        )
    try:
        final_metadata = path.lstat()
    except FileNotFoundError as exc:
        raise StorageValidationError(
            f"refusing to replace a changed CodeNib directory: {path}"
        ) from exc
    if _is_link_or_reparse(final_metadata) or _directory_identity(
        final_metadata
    ) != _directory_identity(metadata):
        raise StorageValidationError(
            f"refusing to replace a changed CodeNib directory: {path}"
        )
    return _MaterializationOwnership(
        "bundle",
        _directory_identity(final_metadata),
        _manifest_digest,
        members,
        tuple(
            sorted(
                (
                    PurePosixPath(*parts).as_posix() if parts else "",
                    signature[2] & 0o7777,
                )
                for parts, signature in snapshot.directories.items()
            )
        ),
    )


def _directory_inode_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        getattr(metadata, "st_file_attributes", 0),
    )


def _require_owned_stage_path(
    stage: Path,
    expected_identity: tuple[int, ...],
) -> None:
    try:
        metadata = stage.lstat()
    except FileNotFoundError as exc:
        raise StorageIntegrityError(
            "view bundle materialization stage disappeared"
        ) from exc
    if (
        _is_link_or_reparse(metadata)
        or not stat.S_ISDIR(metadata.st_mode)
        or _directory_inode_identity(metadata) != expected_identity
    ):
        raise StorageIntegrityError(
            "view bundle materialization stage changed during extraction"
        )


def _remove_owned_stage(stage: Path, expected_identity: tuple[int, ...]) -> None:
    """Isolate only the original temporary root for cooperative orphan GC."""

    try:
        metadata = stage.lstat()
    except FileNotFoundError:
        return
    if (
        _is_link_or_reparse(metadata)
        or not stat.S_ISDIR(metadata.st_mode)
        or _directory_inode_identity(metadata) != expected_identity
    ):
        return
    try:
        ownership = capture_directory_ownership(stage)
    except (OSError, RuntimeError, ValueError):
        return
    if directory_ownership_root_identity(ownership) != expected_identity:
        return
    _record_publication_orphan(
        discard_owned_directory(stage, ownership),
        operation="discard",
    )


def _reject_forbidden_roots(
    candidate: Path,
    roots: Iterable[str | Path],
    *,
    label: str,
) -> None:
    for root in roots:
        forbidden = Path(root).expanduser().resolve(strict=False)
        _reject_overlap(candidate, forbidden, label=f"{label} and forbidden root")


def _reject_overlap(left: Path, right: Path, *, label: str) -> None:
    left_value = os.path.normcase(os.path.abspath(left))
    right_value = os.path.normcase(os.path.abspath(right))
    try:
        common = os.path.commonpath((left_value, right_value))
    except ValueError:
        return
    if common in {left_value, right_value}:
        raise StorageValidationError(f"{label} overlap: {left}")


def _path_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _require_path_signature(
    path: Path,
    expected: tuple[int, ...],
    *,
    label: str,
) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise StorageIntegrityError(f"{label} disappeared: {path}") from exc
    if _is_link_or_reparse(metadata) or _path_signature(metadata) != expected:
        raise StorageIntegrityError(f"{label} changed: {path}")


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


__all__ = [
    "DEFAULT_MAX_BUNDLE_BYTES",
    "DEFAULT_MAX_BUNDLE_FILES",
    "DEFAULT_MAX_BUNDLE_METADATA_BYTES",
    "MaterializedViewBundle",
    "PlannedViewBundle",
    "VIEW_BUNDLE_MANIFEST",
    "VIEW_BUNDLE_MEDIA_TYPE",
    "VIEW_BUNDLE_PAYLOAD",
    "VIEW_BUNDLE_SCHEMA",
    "ViewBundleRecord",
    "build_view_bundle",
    "consume_planned_view_bundle",
    "materialize_view_bundle",
    "plan_view_bundle_reader",
    "validate_view_bundle_physical_size",
    "verify_view_bundle",
]
