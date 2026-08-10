# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded, traversal-safe extraction for context artifact archives."""

from __future__ import annotations

import os
import stat
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

from .._atomic_directory import PublicationDirectoryReader, lexical_directory_path
from .._captured_directory import (
    OwnedDirectoryStage,
    _create_sealable_memfd,
    _seal_snapshot_descriptor,
    _SnapshotUnavailable,
    _write_all,
)
from .context import CONTEXT_ARTIFACT_MANIFEST
from .runtime import VerifiedContextArtifact, verify_context_artifact_reader

DEFAULT_MAX_ARCHIVE_FILES = 100_000
DEFAULT_MAX_EXPANDED_BYTES = 64 * 1024 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_ARCHIVE_PATH_BYTES = 4_096
_MAX_ARCHIVE_PATH_COMPONENTS = 256
_MAX_ARCHIVE_ENVELOPE_BYTES = 16 * 1024 * 1024
_MAX_ARCHIVE_MEMBER_OVERHEAD = 4_096


def _read_archive(descriptor: int, size: int) -> bytes:
    return os.read(descriptor, size)


def _create_archive_snapshot() -> int:
    try:
        return _create_sealable_memfd()
    except _SnapshotUnavailable as exc:
        raise ValueError(
            "secure context archive parsing requires immutable sealed snapshots"
        ) from exc


def _write_snapshot(descriptor: int, block: bytes) -> None:
    _write_all(descriptor, block)


def _seal_archive_snapshot(descriptor: int) -> None:
    try:
        _seal_snapshot_descriptor(descriptor)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            "context artifact archive snapshot could not be sealed"
        ) from exc


def _stable_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
        getattr(metadata, "st_file_attributes", 0),
    )


def _directory_binding_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Identify one retained lexical ancestor without mutable directory times."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        getattr(metadata, "st_file_attributes", 0) & 0x400,
    )


@contextmanager
def _authenticated_archive_snapshot(
    value: str | Path,
    *,
    max_files: int,
    max_bytes: int,
) -> Iterator[tuple[Path, BinaryIO]]:
    """Copy one lexical no-follow regular file into a verified snapshot."""

    if os.name != "posix" or not (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
    ):
        raise ValueError(
            "secure context archive reads require no-follow directory handles"
        )
    candidate = Path(value).expanduser()
    lexical = Path(os.path.abspath(os.fspath(candidate)))
    encoded = os.fsencode(lexical)
    if (
        lexical.name in {"", ".", ".."}
        or len(encoded) > _MAX_ARCHIVE_PATH_BYTES
        or len(lexical.parts) > _MAX_ARCHIVE_PATH_COMPONENTS
    ):
        raise ValueError("context artifact archive path is invalid or too long")
    physical_limit = (
        max_bytes
        + max_files * _MAX_ARCHIVE_MEMBER_OVERHEAD
        + _MAX_ARCHIVE_ENVELOPE_BYTES
    )
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    file_flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptors: list[int] = []
    bindings: list[tuple[int, str, int, tuple[int, ...]]] = []
    source = -1
    snapshot_descriptor = -1
    snapshot: BinaryIO | None = None
    try:
        descriptor = os.open(lexical.anchor, directory_flags)
        descriptors.append(descriptor)
        root = os.fstat(descriptor)
        if not root.st_dev or not root.st_ino or not stat.S_ISDIR(root.st_mode):
            raise ValueError("context artifact archive root has no stable identity")
        for part in lexical.parts[1:-1]:
            before = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if (
                not stat.S_ISDIR(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or not before.st_dev
                or not before.st_ino
            ):
                raise ValueError(
                    "context artifact archive parent is not a real directory"
                )
            child = os.open(part, directory_flags, dir_fd=descriptor)
            opened = os.fstat(child)
            if _directory_binding_identity(opened) != _directory_binding_identity(
                before
            ):
                os.close(child)
                raise ValueError("context artifact archive parent changed")
            bindings.append(
                (descriptor, part, child, _directory_binding_identity(opened))
            )
            descriptors.append(child)
            descriptor = child

        before = os.stat(
            lexical.name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or not before.st_dev
            or not before.st_ino
            or before.st_size < 0
            or before.st_size > physical_limit
        ):
            raise ValueError(
                f"context artifact archive is not a bounded regular file: {lexical}"
            )
        source = os.open(lexical.name, file_flags, dir_fd=descriptor)
        opened = os.fstat(source)
        expected = _stable_identity(before)
        if _stable_identity(opened) != expected:
            raise ValueError("context artifact archive changed while opening")

        snapshot_descriptor = _create_archive_snapshot()
        remaining = opened.st_size
        while remaining:
            block = _read_archive(source, min(remaining, _COPY_CHUNK_BYTES))
            if not block:
                raise ValueError("context artifact archive was truncated")
            _write_snapshot(snapshot_descriptor, block)
            remaining -= len(block)
        if _read_archive(source, 1):
            raise ValueError("context artifact archive grew while reading")

        def verify_binding() -> None:
            if (
                _stable_identity(os.fstat(source)) != expected
                or _stable_identity(
                    os.stat(
                        lexical.name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                )
                != expected
            ):
                raise ValueError("context artifact archive changed while reading")
            for parent, name, child, identity in bindings:
                if (
                    _directory_binding_identity(os.fstat(child)) != identity
                    or _directory_binding_identity(
                        os.stat(name, dir_fd=parent, follow_symlinks=False)
                    )
                    != identity
                ):
                    raise ValueError("context artifact archive parent changed")

        verify_binding()
        _seal_archive_snapshot(snapshot_descriptor)
        snapshot = os.fdopen(snapshot_descriptor, "rb")
        snapshot_descriptor = -1
        yield lexical, snapshot
        verify_binding()
    except FileNotFoundError as exc:
        raise ValueError(f"context artifact archive does not exist: {lexical}") from exc
    except OSError as exc:
        raise ValueError(
            f"context artifact archive could not be opened safely: {lexical}"
        ) from exc
    finally:
        if snapshot is not None:
            snapshot.close()
        if snapshot_descriptor >= 0:
            os.close(snapshot_descriptor)
        if source >= 0:
            os.close(source)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _member_path(value: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("context artifact archive contains an invalid path")
    path = PurePosixPath(value)
    normalized = path.as_posix().rstrip("/")
    if (
        path.is_absolute()
        or not normalized
        or value.rstrip("/") != normalized
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"context artifact archive path is unsafe: {value!r}")
    return PurePosixPath(normalized)


def _member_kind(info: zipfile.ZipInfo) -> int:
    return stat.S_IFMT(info.external_attr >> 16)


def _validated_members(
    archive: zipfile.ZipFile,
    *,
    max_files: int,
    max_bytes: int,
) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    entries = archive.infolist()
    max_entries = max_files * 2 + 64
    if len(entries) > max_entries:
        raise ValueError(f"context artifact archive exceeds {max_entries} entries")
    raw: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    metadata_paths: list[PurePosixPath] = []
    for info in entries:
        path = _member_path(info.filename)
        kind = _member_kind(info)
        if kind == stat.S_IFLNK:
            raise ValueError(
                f"context artifact archive contains a symbolic link: {path}"
            )
        if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise ValueError(
                f"context artifact archive contains a special file: {path}"
            )
        if info.flag_bits & 0x1:
            raise ValueError(f"context artifact archive member is encrypted: {path}")
        raw.append((info, path))
        if not info.is_dir() and path.name == CONTEXT_ARTIFACT_MANIFEST:
            metadata_paths.append(path)

    if len(metadata_paths) != 1:
        raise ValueError(
            "context artifact archive must contain exactly one metadata file"
        )
    prefix = metadata_paths[0].parent
    prefix_parts = () if str(prefix) == "." else prefix.parts

    result: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    seen: set[str] = set()
    file_count = 0
    total_bytes = 0
    for info, path in raw:
        if prefix_parts:
            if path.parts[: len(prefix_parts)] != prefix_parts:
                raise ValueError(
                    "context artifact archive contains files outside its root"
                )
            stripped_parts = path.parts[len(prefix_parts) :]
            if not stripped_parts:
                continue
            path = PurePosixPath(*stripped_parts)
        relative = path.as_posix()
        if relative in seen:
            raise ValueError(f"duplicate context artifact archive path: {relative}")
        seen.add(relative)
        if not info.is_dir():
            file_count += 1
            total_bytes += info.file_size
            if file_count > max_files:
                raise ValueError(f"context artifact archive exceeds {max_files} files")
            if total_bytes > max_bytes:
                raise ValueError(
                    f"context artifact archive exceeds {max_bytes} expanded bytes"
                )
        result.append((info, path))
    return result


def _extract_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    relative: PurePosixPath,
    stage: OwnedDirectoryStage,
) -> None:
    if info.is_dir():
        return

    def chunks():
        written = 0
        with archive.open(info, "r") as source:
            while chunk := source.read(_COPY_CHUNK_BYTES):
                written += len(chunk)
                if written > info.file_size:
                    raise ValueError(
                        "context artifact archive member exceeded declared size: "
                        f"{relative}"
                    )
                yield chunk
        if written != info.file_size:
            raise ValueError(
                f"context artifact archive member size mismatch: {relative}"
            )

    stage.write_file(relative, chunks(), max_bytes=info.file_size)


def extract_context_artifact_archive(
    archive_path: str | Path,
    output_dir: str | Path,
    *,
    expected_repository: str | None = None,
    expected_commit: str | None = None,
    max_files: int = DEFAULT_MAX_ARCHIVE_FILES,
    max_bytes: int = DEFAULT_MAX_EXPANDED_BYTES,
) -> VerifiedContextArtifact:
    """Extract and verify an artifact ZIP before publishing it."""

    if (
        isinstance(max_files, bool)
        or not isinstance(max_files, int)
        or max_files <= 0
        or isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes <= 0
    ):
        raise ValueError("context artifact archive limits must be positive integers")
    output = lexical_directory_path(Path(output_dir))
    try:
        stage = OwnedDirectoryStage.prepare(
            output,
            required_destination_file=CONTEXT_ARTIFACT_MANIFEST,
            allow_empty_destination=True,
        )
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            "refusing to replace a non-empty directory that is not a "
            f"CodeNib context artifact: {output}"
        ) from exc

    published: list[VerifiedContextArtifact] = []
    try:
        with _authenticated_archive_snapshot(
            archive_path,
            max_files=max_files,
            max_bytes=max_bytes,
        ) as (archive_display, snapshot):
            with zipfile.ZipFile(snapshot) as archive:
                members = _validated_members(
                    archive,
                    max_files=max_files,
                    max_bytes=max_bytes,
                )
                for info, relative in members:
                    _extract_member(archive, info, relative, stage)

        def validate_staged_artifact(candidate: PublicationDirectoryReader) -> None:
            verify_context_artifact_reader(
                candidate,
                expected_repository=expected_repository,
                expected_commit=expected_commit,
                max_files=max_files,
                max_bytes=max_bytes,
            )

        def validate_published_artifact(
            candidate: PublicationDirectoryReader,
        ) -> None:
            verified = verify_context_artifact_reader(
                candidate,
                expected_repository=expected_repository,
                expected_commit=expected_commit,
                max_files=max_files,
                max_bytes=max_bytes,
            )
            published.append(
                replace(
                    verified,
                    root=output,
                    metadata_path=output / CONTEXT_ARTIFACT_MANIFEST,
                    manifest_path=output / verified.manifest_path.name,
                )
            )

        stage.publish(
            validate_staged_directory=validate_staged_artifact,
            validate_published_destination=validate_published_artifact,
        )
    except zipfile.BadZipFile as exc:
        stage.discard()
        raise ValueError(
            f"context artifact archive is not a valid ZIP: {archive_display}"
        ) from exc
    except BaseException:
        stage.discard()
        raise

    if len(published) != 1:
        raise RuntimeError("published context artifact was not authority-verified")
    return published[0]


__all__ = [
    "DEFAULT_MAX_ARCHIVE_FILES",
    "DEFAULT_MAX_EXPANDED_BYTES",
    "extract_context_artifact_archive",
]
