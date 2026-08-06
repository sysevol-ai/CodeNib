# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded, traversal-safe extraction for context artifact archives."""

from __future__ import annotations

import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .._atomic_directory import publish_staged_directory
from .context import CONTEXT_ARTIFACT_MANIFEST
from .runtime import VerifiedContextArtifact, verify_context_artifact

DEFAULT_MAX_ARCHIVE_FILES = 100_000
DEFAULT_MAX_EXPANDED_BYTES = 64 * 1024 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024


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
    root: Path,
) -> None:
    output = root.joinpath(*relative.parts)
    if info.is_dir():
        output.mkdir(parents=True, exist_ok=True)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with archive.open(info, "r") as source, output.open("xb") as destination:
        while chunk := source.read(_COPY_CHUNK_BYTES):
            written += len(chunk)
            if written > info.file_size:
                raise ValueError(
                    f"context artifact archive member exceeded declared size: {relative}"
                )
            destination.write(chunk)
    if written != info.file_size:
        raise ValueError(f"context artifact archive member size mismatch: {relative}")


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

    archive_candidate = Path(archive_path).expanduser()
    if archive_candidate.is_symlink():
        raise ValueError(
            f"context artifact archive must not be a symbolic link: {archive_candidate}"
        )
    archive_path = archive_candidate.resolve()
    if not archive_path.is_file():
        raise ValueError(f"context artifact archive does not exist: {archive_path}")
    output_candidate = Path(output_dir).expanduser()
    if output_candidate.is_symlink():
        raise ValueError(
            f"context artifact output must not be a symbolic link: {output_candidate}"
        )
    output = output_candidate.resolve()
    if output.exists() and not output.is_dir():
        raise ValueError(f"context artifact output is not a directory: {output}")
    if output.exists() and any(output.iterdir()):
        if not (output / CONTEXT_ARTIFACT_MANIFEST).is_file():
            raise ValueError(
                "refusing to replace a non-empty directory that is not a "
                f"CodeNib context artifact: {output}"
            )

    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}.extract-",
            dir=str(output.parent),
        )
    ).resolve()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = _validated_members(
                archive,
                max_files=max_files,
                max_bytes=max_bytes,
            )
            for info, relative in members:
                _extract_member(archive, info, relative, stage)
        verify_context_artifact(
            stage,
            expected_repository=expected_repository,
            expected_commit=expected_commit,
            max_files=max_files,
            max_bytes=max_bytes,
        )
        publish_staged_directory(stage, output)
    except zipfile.BadZipFile as exc:
        shutil.rmtree(stage, ignore_errors=True)
        raise ValueError(
            f"context artifact archive is not a valid ZIP: {archive_path}"
        ) from exc
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    return verify_context_artifact(
        output,
        expected_repository=expected_repository,
        expected_commit=expected_commit,
        max_files=max_files,
        max_bytes=max_bytes,
    )


__all__ = [
    "DEFAULT_MAX_ARCHIVE_FILES",
    "DEFAULT_MAX_EXPANDED_BYTES",
    "extract_context_artifact_archive",
]
