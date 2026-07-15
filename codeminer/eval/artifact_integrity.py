"""Content selection, checksums, and deterministic evaluation archives."""

from __future__ import annotations

import fnmatch
import gzip
import hashlib
import os
import re
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from .artifact_manifest import (
    CHECKSUM_FILE,
    ArtifactBundleError,
    ArtifactIntegrityError,
    SourceSpec,
)

_HASH_DOMAIN = b"codeminer-artifact-selection-v1\0"


@dataclass(frozen=True)
class FileRecord:
    """Content identity for one selected source file."""

    source: Path
    relative_path: PurePosixPath
    size: int
    sha256: str


@dataclass(frozen=True)
class SelectionIdentity:
    """Canonical identity for a selected file set."""

    files: int
    bytes: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {"files": self.files, "bytes": self.bytes, "sha256": self.sha256}


def source_path(spec: SourceSpec, root: Path) -> Path:
    """Resolve one source without permitting symbolic-link or root escape."""

    source = root / Path(*spec.path.parts)
    current = root
    for part in spec.path.parts:
        current = current / part
        if current.is_symlink():
            raise ArtifactBundleError(
                f"source {spec.source_id!r} traverses symbolic link {current}"
            )
    try:
        source.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ArtifactBundleError(
            f"source {spec.source_id!r} escapes root {spec.root!r}"
        ) from exc
    return source


def select_source(spec: SourceSpec, root: Path) -> tuple[FileRecord, ...]:
    """Select and hash files for one source specification."""

    source = source_path(spec, root)
    if source.is_symlink():
        raise ArtifactBundleError(f"source {spec.source_id!r} is a symbolic link")
    if source.is_file():
        relative = PurePosixPath(source.name)
        if not _included(relative, spec.include, spec.exclude):
            raise ArtifactBundleError(
                f"source {spec.source_id!r} file is excluded by its own patterns"
            )
        return (_file_record(source, relative),)
    if not source.is_dir():
        raise ArtifactBundleError(
            f"source {spec.source_id!r} does not exist: {spec.path.as_posix()}"
        )

    records = []
    excluded_dirs = set(spec.exclude_dir_names)
    for current, dirnames, filenames in os.walk(source, topdown=True):
        current_path = Path(current)
        kept_dirs = []
        for dirname in sorted(dirnames):
            child = current_path / dirname
            relative = PurePosixPath(child.relative_to(source).as_posix())
            if dirname in excluded_dirs or _matches(relative, spec.exclude):
                continue
            if child.is_symlink():
                raise ArtifactBundleError(
                    f"source {spec.source_id!r} contains symbolic link {relative}"
                )
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in sorted(filenames):
            child = current_path / filename
            relative = PurePosixPath(child.relative_to(source).as_posix())
            if not _included(relative, spec.include, spec.exclude):
                continue
            if child.is_symlink():
                raise ArtifactBundleError(
                    f"source {spec.source_id!r} contains symbolic link {relative}"
                )
            if not child.is_file():
                raise ArtifactBundleError(
                    f"source {spec.source_id!r} contains non-regular file {relative}"
                )
            records.append(_file_record(child, relative))
    if not records:
        raise ArtifactBundleError(f"source {spec.source_id!r} selected no files")
    return tuple(sorted(records, key=lambda record: record.relative_path.as_posix()))


def selection_identity(records: Sequence[FileRecord]) -> SelectionIdentity:
    """Return a path-sensitive canonical identity for selected files."""

    digest = hashlib.sha256()
    digest.update(_HASH_DOMAIN)
    total_bytes = 0
    for record in records:
        relative = record.relative_path.as_posix()
        if "\n" in relative or "\0" in relative:
            raise ArtifactBundleError(f"unsupported source filename: {relative!r}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(record.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(record.sha256.encode("ascii"))
        digest.update(b"\0")
        total_bytes += record.size
    return SelectionIdentity(
        files=len(records), bytes=total_bytes, sha256=digest.hexdigest()
    )


def write_checksums(root: Path | str) -> dict[str, str]:
    """Write a complete SHA-256 inventory for regular files below ``root``."""

    root_path = Path(root).resolve(strict=True)
    checksum_path = root_path / CHECKSUM_FILE
    if checksum_path.exists():
        raise ArtifactBundleError(f"checksum file already exists: {checksum_path}")
    checksums = {}
    for path in _bundle_files(root_path, exclude_checksum=True):
        relative = path.relative_to(root_path).as_posix()
        checksums[relative] = sha256_file(path)
    checksum_path.write_text(
        "".join(f"{digest}  {relative}\n" for relative, digest in checksums.items()),
        encoding="utf-8",
    )
    return checksums


def verify_bundle(root: Path | str) -> dict[str, Any]:
    """Verify all files in a bundle, rejecting missing and unlisted payloads."""

    root_path = Path(root).expanduser().resolve(strict=True)
    checksum_path = root_path / CHECKSUM_FILE
    if not checksum_path.is_file():
        raise ArtifactIntegrityError(f"missing {CHECKSUM_FILE} in {root_path}")
    expected = _read_checksums(checksum_path)
    actual_paths = {
        path.relative_to(root_path).as_posix(): path
        for path in _bundle_files(root_path, exclude_checksum=True)
    }
    if set(expected) != set(actual_paths):
        missing = sorted(set(expected) - set(actual_paths))
        extra = sorted(set(actual_paths) - set(expected))
        raise ArtifactIntegrityError(
            f"bundle inventory differs; missing={missing}, unlisted={extra}"
        )
    total_bytes = 0
    for relative, path in actual_paths.items():
        if sha256_file(path) != expected[relative]:
            raise ArtifactIntegrityError(f"checksum mismatch: {relative}")
        total_bytes += path.stat().st_size
    return {"files": len(actual_paths), "bytes": total_bytes}


def create_archive(root: Path | str, output: Path | str) -> dict[str, Any]:
    """Create a deterministic ``.tar.gz`` archive from a verified bundle."""

    root_path = Path(root).expanduser().resolve(strict=True)
    verification = verify_bundle(root_path)
    output_path = Path(output).expanduser().absolute()
    if output_path.suffixes[-2:] != [".tar", ".gz"]:
        raise ArtifactBundleError("archive output must end in .tar.gz")
    sidecar = Path(f"{output_path}.sha256")
    if output_path.exists() or sidecar.exists():
        raise ArtifactBundleError(f"archive output already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.parent / f".{output_path.name}.tmp-{uuid.uuid4().hex}"
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(
                filename="", mode="wb", compresslevel=9, fileobj=raw, mtime=0
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
                ) as archive:
                    _add_archive_tree(archive, root_path)
        os.replace(temporary, output_path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    digest = sha256_file(output_path)
    sidecar.write_text(f"{digest}  {output_path.name}\n", encoding="utf-8")
    return {
        "archive": str(output_path),
        "sha256": digest,
        "bytes": output_path.stat().st_size,
        "bundle_files": verification["files"],
    }


def sha256_file(path: Path) -> str:
    """Hash a regular file without loading it into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_record(path: Path, relative: PurePosixPath) -> FileRecord:
    stat = path.stat()
    return FileRecord(
        source=path,
        relative_path=relative,
        size=stat.st_size,
        sha256=sha256_file(path),
    )


def _matches(path: PurePosixPath, patterns: Sequence[str]) -> bool:
    value = path.as_posix()
    return any(
        fnmatch.fnmatchcase(value, pattern)
        or ("/" not in pattern and fnmatch.fnmatchcase(path.name, pattern))
        for pattern in patterns
    )


def _included(
    path: PurePosixPath, include: Sequence[str], exclude: Sequence[str]
) -> bool:
    return _matches(path, include) and not _matches(path, exclude)


def _bundle_files(root: Path, *, exclude_checksum: bool) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ArtifactIntegrityError(f"bundle contains symbolic link: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ArtifactIntegrityError(
                f"bundle contains non-regular file: {relative}"
            )
        if exclude_checksum and relative == CHECKSUM_FILE:
            continue
        if "\n" in relative:
            raise ArtifactIntegrityError(f"unsupported bundle filename: {relative!r}")
        files.append(path)
    return files


def _read_checksums(path: Path) -> dict[str, str]:
    checksums = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line:
            continue
        try:
            digest, relative = raw_line.split("  ", 1)
        except ValueError as exc:
            raise ArtifactIntegrityError(
                f"invalid checksum line {line_number}"
            ) from exc
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ArtifactIntegrityError(f"invalid checksum on line {line_number}")
        relative_path = PurePosixPath(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ArtifactIntegrityError(
                f"checksum path must stay relative: {relative!r}"
            )
        relative = relative_path.as_posix()
        if relative in checksums:
            raise ArtifactIntegrityError(f"duplicate checksum path: {relative}")
        if relative == CHECKSUM_FILE:
            raise ArtifactIntegrityError(f"{CHECKSUM_FILE} cannot checksum itself")
        checksums[relative] = digest
    if not checksums:
        raise ArtifactIntegrityError("checksum inventory is empty")
    return checksums


def _add_archive_tree(archive: tarfile.TarFile, root: Path) -> None:
    paths = [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]
    for path in paths:
        if path.is_symlink():
            raise ArtifactIntegrityError(f"cannot archive symbolic link: {path}")
        arcname = root.name
        if path != root:
            arcname = f"{root.name}/{path.relative_to(root).as_posix()}"
        info = archive.gettarinfo(str(path), arcname=arcname)
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = 0
        info.pax_headers = {}
        if path.is_dir():
            archive.addfile(info)
        elif path.is_file():
            with path.open("rb") as handle:
                archive.addfile(info, handle)
        else:
            raise ArtifactIntegrityError(f"cannot archive non-regular path: {path}")
