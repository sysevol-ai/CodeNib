# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Race-resistant reads of repository files, including contained symlinks."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_MAX_COMPONENTS = 256
_MAX_SYMLINKS = 40
_MAX_SYMLINK_TARGET_BYTES = 4_096
_READ_CHUNK_BYTES = 1024 * 1024
SECURE_CONTAINED_SYMLINKS = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in getattr(os, "supports_dir_fd", ())
    and os.stat in getattr(os, "supports_dir_fd", ())
    and os.stat in getattr(os, "supports_follow_symlinks", ())
    and os.readlink in getattr(os, "supports_dir_fd", ())
)


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _binding_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0,
        getattr(metadata, "st_file_attributes", 0),
    )


def _version_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        *_binding_identity(metadata),
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _relative_parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("source path must be a repository-relative POSIX path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
        or len(relative.parts) > _MAX_COMPONENTS
    ):
        raise ValueError("source path must be a repository-relative POSIX path")
    return relative.parts


def _normalize_link_target(
    prefix: tuple[str, ...],
    target: str,
) -> tuple[str, ...]:
    if not target or "\x00" in target or os.path.isabs(target):
        raise ValueError("source symlink target must be relative")
    if len(os.fsencode(target)) > _MAX_SYMLINK_TARGET_BYTES:
        raise ValueError("source symlink target exceeds its byte limit")
    resolved = list(prefix)
    for part in target.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                raise ValueError("source symlink resolves outside the repository")
            resolved.pop()
            continue
        resolved.append(part)
        if len(resolved) > _MAX_COMPONENTS:
            raise ValueError("resolved source path exceeds its component limit")
    if not resolved:
        raise ValueError("source symlink does not resolve to a file")
    return tuple(resolved)


@dataclass(slots=True)
class _PathObservation:
    parent_descriptor: int
    name: str
    identity: tuple[int, ...]
    link_target: str | None = None


@dataclass(slots=True)
class _BoundRepositoryFile:
    descriptor: int
    opened_identity: tuple[int, ...]
    root: Path
    root_descriptor: int
    root_identity: tuple[int, ...]
    observations: list[_PathObservation]

    def read_bytes(self, *, max_bytes: int) -> bytes:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError("source byte limit must be positive")
        opened = os.fstat(self.descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _version_identity(opened) != self.opened_identity
            or opened.st_size < 0
            or opened.st_size > max_bytes
        ):
            raise ValueError("source file is not a stable bounded regular file")
        payload = bytearray()
        remaining = opened.st_size
        while remaining:
            chunk = os.read(self.descriptor, min(remaining, _READ_CHUNK_BYTES))
            if not chunk:
                raise ValueError("source file was truncated while being read")
            payload.extend(chunk)
            remaining -= len(chunk)
        if os.read(self.descriptor, 1):
            raise ValueError("source file grew while being read")
        self.verify()
        return bytes(payload)

    def verify(self) -> None:
        if _version_identity(os.fstat(self.root_descriptor)) != self.root_identity:
            raise ValueError("repository root changed during source resolution")
        root_after = self.root.lstat()
        if _binding_identity(root_after) != _binding_identity(
            os.fstat(self.root_descriptor)
        ):
            raise ValueError("repository root changed during source resolution")
        for observation in self.observations:
            current = os.stat(
                observation.name,
                dir_fd=observation.parent_descriptor,
                follow_symlinks=False,
            )
            if _version_identity(current) != observation.identity:
                raise ValueError("source path changed during contained resolution")
            if observation.link_target is not None:
                if (
                    not stat.S_ISLNK(current.st_mode)
                    or os.readlink(
                        observation.name,
                        dir_fd=observation.parent_descriptor,
                    )
                    != observation.link_target
                ):
                    raise ValueError(
                        "source symlink changed during contained resolution"
                    )
        if _version_identity(os.fstat(self.descriptor)) != self.opened_identity:
            raise ValueError("source file changed while it was being read")

    def close(self) -> None:
        descriptors = [
            self.descriptor,
            self.root_descriptor,
            *(item.parent_descriptor for item in self.observations),
        ]
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _regular_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )


def _open_secure(root: Path, parts: tuple[str, ...]) -> _BoundRepositoryFile:
    root_before = root.lstat()
    if _is_link_or_reparse(root_before) or not stat.S_ISDIR(root_before.st_mode):
        raise ValueError("repository root must be a real directory")
    root_descriptor = os.open(root, _directory_flags())
    current_descriptor = -1
    source_descriptor = -1
    observations: list[_PathObservation] = []
    seen_links: set[tuple[int, int]] = set()
    try:
        root_opened = os.fstat(root_descriptor)
        if _binding_identity(root_opened) != _binding_identity(root_before):
            raise ValueError("repository root changed during source resolution")
        current_descriptor = os.dup(root_descriptor)
        pending = list(parts)
        resolved_prefix: tuple[str, ...] = ()
        symlink_count = 0
        while pending:
            name = pending.pop(0)
            before = os.stat(name, dir_fd=current_descriptor, follow_symlinks=False)
            if bool(
                getattr(before, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError("source path contains a reparse point")
            if stat.S_ISLNK(before.st_mode):
                symlink_count += 1
                if symlink_count > _MAX_SYMLINKS:
                    raise ValueError("source symlink resolution exceeds its limit")
                link_identity = (before.st_dev, before.st_ino)
                if link_identity in seen_links:
                    raise ValueError("source symlink resolution contains a loop")
                seen_links.add(link_identity)
                target = os.readlink(name, dir_fd=current_descriptor)
                after = os.stat(
                    name,
                    dir_fd=current_descriptor,
                    follow_symlinks=False,
                )
                if _version_identity(after) != _version_identity(before):
                    raise ValueError(
                        "source symlink changed during contained resolution"
                    )
                observations.append(
                    _PathObservation(
                        parent_descriptor=os.dup(current_descriptor),
                        name=name,
                        identity=_version_identity(before),
                        link_target=target,
                    )
                )
                target_parts = _normalize_link_target(resolved_prefix, target)
                if len(target_parts) + len(pending) > _MAX_COMPONENTS:
                    raise ValueError("resolved source path exceeds its component limit")
                pending = [*target_parts, *pending]
                resolved_prefix = ()
                os.close(current_descriptor)
                current_descriptor = os.dup(root_descriptor)
                continue

            observations.append(
                _PathObservation(
                    parent_descriptor=os.dup(current_descriptor),
                    name=name,
                    identity=_version_identity(before),
                )
            )
            if pending:
                if not stat.S_ISDIR(before.st_mode):
                    raise ValueError("source path has a non-directory component")
                child_descriptor = os.open(
                    name,
                    _directory_flags(),
                    dir_fd=current_descriptor,
                )
                opened = os.fstat(child_descriptor)
                if _binding_identity(opened) != _binding_identity(before):
                    os.close(child_descriptor)
                    raise ValueError("source directory changed while opening")
                os.close(current_descriptor)
                current_descriptor = child_descriptor
                resolved_prefix = (*resolved_prefix, name)
                continue

            if not stat.S_ISREG(before.st_mode):
                raise ValueError("source path is not a regular repository file")
            source_descriptor = os.open(
                name,
                _regular_flags(),
                dir_fd=current_descriptor,
            )
            opened = os.fstat(source_descriptor)
            if _binding_identity(opened) != _binding_identity(before):
                raise ValueError("source file changed while opening")
            binding = _BoundRepositoryFile(
                descriptor=source_descriptor,
                opened_identity=_version_identity(opened),
                root=root,
                root_descriptor=root_descriptor,
                root_identity=_version_identity(root_opened),
                observations=observations,
            )
            source_descriptor = -1
            root_descriptor = -1
            try:
                binding.verify()
            except BaseException:
                binding.close()
                raise
            return binding
        raise ValueError("source path does not resolve to a file")
    except OSError as exc:
        raise ValueError("source path could not be resolved safely") from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if current_descriptor >= 0:
            os.close(current_descriptor)
        if root_descriptor >= 0:
            os.close(root_descriptor)
        if root_descriptor >= 0 or source_descriptor >= 0:
            for observation in observations:
                try:
                    os.close(observation.parent_descriptor)
                except OSError:
                    pass


def _static_components(
    root: Path,
    parts: tuple[str, ...],
) -> tuple[Path, tuple[tuple[int, ...], ...]]:
    current = root
    identities: list[tuple[int, ...]] = []
    root_metadata = root.lstat()
    if _is_link_or_reparse(root_metadata) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("repository root must be a real directory")
    identities.append(_version_identity(root_metadata))
    for index, part in enumerate(parts):
        current /= part
        metadata = current.lstat()
        if _is_link_or_reparse(metadata):
            raise ValueError(
                "safe contained symlink resolution requires directory-fd support"
            )
        expected = stat.S_ISREG if index == len(parts) - 1 else stat.S_ISDIR
        if not expected(metadata.st_mode):
            raise ValueError("source path is not a regular repository file")
        identities.append(_version_identity(metadata))
    return current, tuple(identities)


def _read_static(root: Path, parts: tuple[str, ...], *, max_bytes: int) -> bytes:
    source, identities = _static_components(root, parts)
    descriptor = -1
    try:
        descriptor = os.open(source, _regular_flags())
        opened = os.fstat(descriptor)
        if _version_identity(opened) != identities[-1] or opened.st_size > max_bytes:
            raise ValueError("source file is not a stable bounded regular file")
        payload = bytearray()
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
            if not chunk:
                raise ValueError("source file was truncated while being read")
            payload.extend(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("source file grew while being read")
        _source, after_identities = _static_components(root, parts)
        if after_identities != identities or _version_identity(
            os.fstat(descriptor)
        ) != (identities[-1]):
            raise ValueError("source path changed while it was being read")
        return bytes(payload)
    except OSError as exc:
        raise ValueError("source path could not be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def validate_repository_file(root: str | Path, relative: str) -> None:
    """Require one stable regular source file resolving beneath *root*."""

    repository = Path(root).expanduser().resolve()
    parts = _relative_parts(relative)
    if SECURE_CONTAINED_SYMLINKS:
        binding = _open_secure(repository, parts)
        try:
            try:
                binding.verify()
            except OSError as exc:
                raise ValueError("source path could not be resolved safely") from exc
        finally:
            binding.close()
        return
    _static_components(repository, parts)


def read_repository_file(
    root: str | Path,
    relative: str,
    *,
    max_bytes: int,
) -> bytes:
    """Read a bounded source file through the contained-symlink contract."""

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("source byte limit must be positive")
    repository = Path(root).expanduser().resolve()
    parts = _relative_parts(relative)
    if SECURE_CONTAINED_SYMLINKS:
        binding = _open_secure(repository, parts)
        try:
            return binding.read_bytes(max_bytes=max_bytes)
        except OSError as exc:
            raise ValueError("source path could not be read safely") from exc
        finally:
            binding.close()
    return _read_static(repository, parts, max_bytes=max_bytes)


def update_repository_file_hash(
    root: str | Path,
    relative: str,
    hasher: object,
) -> None:
    """Stream one stable contained source file into a hashlib-compatible hash."""

    update = getattr(hasher, "update", None)
    if not callable(update):
        raise TypeError("hasher must provide an update method")
    repository = Path(root).expanduser().resolve()
    parts = _relative_parts(relative)
    if not SECURE_CONTAINED_SYMLINKS:
        source, identities = _static_components(repository, parts)
        descriptor = -1
        try:
            descriptor = os.open(source, _regular_flags())
            opened = os.fstat(descriptor)
            if _version_identity(opened) != identities[-1]:
                raise ValueError("source file changed while opening")
            remaining = opened.st_size
            while remaining:
                block = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
                if not block:
                    raise ValueError("source file was truncated while hashing")
                update(block)
                remaining -= len(block)
            if os.read(descriptor, 1):
                raise ValueError("source file grew while hashing")
            _source, after_identities = _static_components(repository, parts)
            if after_identities != identities:
                raise ValueError("source path changed while hashing")
            return
        except OSError as exc:
            raise ValueError("source path could not be hashed safely") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    binding = _open_secure(repository, parts)
    try:
        opened = os.fstat(binding.descriptor)
        remaining = opened.st_size
        while remaining:
            block = os.read(binding.descriptor, min(remaining, _READ_CHUNK_BYTES))
            if not block:
                raise ValueError("source file was truncated while hashing")
            update(block)
            remaining -= len(block)
        if os.read(binding.descriptor, 1):
            raise ValueError("source file grew while hashing")
        binding.verify()
    except OSError as exc:
        raise ValueError("source path could not be hashed safely") from exc
    finally:
        binding.close()


__all__ = [
    "SECURE_CONTAINED_SYMLINKS",
    "read_repository_file",
    "update_repository_file_hash",
    "validate_repository_file",
]
