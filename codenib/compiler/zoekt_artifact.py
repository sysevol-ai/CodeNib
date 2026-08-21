# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Authenticate Zoekt shards before handing them to an external process."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable

from .._atomic_directory import (
    PublicationDirectoryReader,
    capture_directory_ownership,
    directory_ownership_file_records,
    directory_ownership_inventory,
    directory_ownership_root_identity,
    reopen_authenticated_directory,
    require_publishable_directory_ownership,
)
from .._captured_directory import OwnedDirectoryStage

if TYPE_CHECKING:
    from .manifest import IndexEntry


ZOEKT_SHARD_TREE_RECEIPT_SCHEMA = "codenib.zoekt-shard-tree.v1"
ZOEKT_BUILDER_SCHEMA = 3

_MAX_FILES = 100_000
_MAX_TOTAL_BYTES = 64 << 30
_MAX_METADATA_BYTES = 16 << 20
_MAX_PATH_BYTES = 4_096
_MAX_COMPONENT_BYTES = 255
_SHA256_HEX_LENGTH = 64


def _is_nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _require_digest(value: object, *, label: str, prefix: bool = False) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a string")
    expected_length = _SHA256_HEX_LENGTH + (7 if prefix else 0)
    if len(value) != expected_length:
        raise ValueError(f"{label} must be a canonical SHA-256 digest")
    payload = value
    if prefix:
        if not value.startswith("sha256:"):
            raise ValueError(f"{label} must be a canonical SHA-256 digest")
        payload = value[7:]
    if payload != payload.lower() or any(
        character not in "0123456789abcdef" for character in payload
    ):
        raise ValueError(f"{label} must be a canonical SHA-256 digest")
    return value


def _require_flat_path(value: object) -> str:
    if type(value) is not str or not value or value in {".", ".."}:
        raise ValueError("Zoekt shard receipt path must be a flat relative path")
    if "/" in value or "\\" in value or "\x00" in value:
        raise ValueError("Zoekt shard receipt path must be a flat relative path")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("Zoekt shard receipt path must be valid UTF-8") from exc
    if len(encoded) > _MAX_PATH_BYTES or len(encoded) > _MAX_COMPONENT_BYTES:
        raise ValueError("Zoekt shard receipt path exceeds its bounded length")
    return value


def _canonical_digest(files: list[dict[str, Any]]) -> tuple[str, bytes]:
    canonical = json.dumps(
        {
            "schema": ZOEKT_SHARD_TREE_RECEIPT_SCHEMA,
            "files": files,
        },
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}", canonical


def _require_receipt(value: object) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {
        "schema",
        "digest",
        "file_count",
        "total_bytes",
        "files",
    }:
        raise ValueError("Zoekt shard tree receipt has an invalid shape")
    if value["schema"] != ZOEKT_SHARD_TREE_RECEIPT_SCHEMA:
        raise ValueError("Zoekt shard tree receipt schema is unsupported")
    _require_digest(value["digest"], label="Zoekt shard receipt digest", prefix=True)
    if not _is_nonnegative_int(value["file_count"]):
        raise ValueError("Zoekt shard receipt file_count must be nonnegative")
    if not _is_nonnegative_int(value["total_bytes"]):
        raise ValueError("Zoekt shard receipt total_bytes must be nonnegative")
    raw_files = value["files"]
    if type(raw_files) is not list:
        raise ValueError("Zoekt shard receipt files must be an array")
    if not 0 < len(raw_files) <= _MAX_FILES:
        raise ValueError("Zoekt shard receipt file count is out of bounds")

    files: list[dict[str, Any]] = []
    previous_path: bytes | None = None
    total_bytes = 0
    shard_count = 0
    for raw_file in raw_files:
        if type(raw_file) is not dict or set(raw_file) != {
            "path",
            "size",
            "sha256",
        }:
            raise ValueError("Zoekt shard receipt file has an invalid shape")
        path = _require_flat_path(raw_file["path"])
        path_bytes = path.encode("utf-8")
        if previous_path is not None and previous_path >= path_bytes:
            raise ValueError("Zoekt shard receipt files are not canonically sorted")
        previous_path = path_bytes
        size = raw_file["size"]
        if not _is_nonnegative_int(size):
            raise ValueError("Zoekt shard receipt file size must be nonnegative")
        total_bytes += size
        if total_bytes > _MAX_TOTAL_BYTES:
            raise ValueError("Zoekt shard receipt total size is out of bounds")
        digest = _require_digest(
            raw_file["sha256"],
            label="Zoekt shard receipt file digest",
        )
        files.append({"path": path, "size": size, "sha256": digest})
        shard_count += path.endswith(".zoekt")

    if shard_count == 0:
        raise ValueError("Zoekt shard receipt contains no .zoekt shard")
    if value["file_count"] != len(files) or value["total_bytes"] != total_bytes:
        raise ValueError("Zoekt shard receipt accounting is inconsistent")
    digest, canonical = _canonical_digest(files)
    if len(canonical) > _MAX_METADATA_BYTES:
        raise ValueError("Zoekt shard receipt metadata exceeds its size limit")
    if value["digest"] != digest:
        raise ValueError("Zoekt shard receipt digest does not match its files")
    return {
        "schema": ZOEKT_SHARD_TREE_RECEIPT_SCHEMA,
        "digest": digest,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "files": files,
    }


def require_zoekt_manifest_receipt(entry: IndexEntry) -> dict[str, Any]:
    """Return one detached exact receipt from a current builder entry."""

    config = entry.config
    metadata = entry.metadata
    for label, values in (("config", config), ("metadata", metadata)):
        if type(values) is not dict:
            raise ValueError(f"Zoekt manifest {label} must be a JSON object")
        if (
            values.get("builder_schema") != ZOEKT_BUILDER_SCHEMA
            or type(values.get("builder_schema")) is not int
        ):
            raise ValueError("Zoekt runtime requires current builder schema 3")
        if values.get("shard_tree_receipt_schema") != ZOEKT_SHARD_TREE_RECEIPT_SCHEMA:
            raise ValueError("Zoekt runtime requires the current shard receipt schema")
    config_receipt = _require_receipt(config.get("shard_tree_receipt"))
    metadata_receipt = _require_receipt(metadata.get("shard_tree_receipt"))
    if config_receipt != metadata_receipt:
        raise ValueError("Zoekt manifest receipt copies do not match")
    return config_receipt


def _flat_file_policy(relative: str, kind: str, _mode: int, _size: int) -> None:
    if kind != "file" or "/" in relative:
        raise RuntimeError("Zoekt shard tree must contain only flat regular files")
    _require_flat_path(relative)


def _files_from_ownership(ownership: object) -> list[dict[str, Any]]:
    inventory = directory_ownership_inventory(ownership)  # type: ignore[arg-type]
    records = directory_ownership_file_records(ownership)  # type: ignore[arg-type]
    if len(inventory) != len(records):
        raise RuntimeError("Zoekt shard tree contains a non-file entry")
    return [
        {"path": record.path, "size": record.size, "sha256": record.sha256}
        for record in records
    ]


def _require_ownership_receipt(ownership: object, receipt: dict[str, Any]) -> None:
    files = _files_from_ownership(ownership)
    observed_digest, canonical = _canonical_digest(files)
    if len(canonical) > _MAX_METADATA_BYTES:
        raise RuntimeError("Zoekt shard tree metadata exceeds its size limit")
    observed = {
        "schema": ZOEKT_SHARD_TREE_RECEIPT_SCHEMA,
        "digest": observed_digest,
        "file_count": len(files),
        "total_bytes": sum(file["size"] for file in files),
        "files": files,
    }
    if observed != receipt:
        raise RuntimeError("Zoekt shard tree differs from its manifest receipt")


def _annotate_cleanup(primary: BaseException, cleanup: BaseException) -> None:
    try:
        primary.add_note(f"Zoekt shard snapshot cleanup also failed: {cleanup!r}")
    except (AttributeError, TypeError):  # pragma: no cover - Python < 3.11
        pass


class ZoektShardSnapshot:
    """Private authenticated shard generation retained for one webserver."""

    __slots__ = (
        "_stage",
        "_ownership",
        "_root_descriptor",
        "_search_path",
        "_closed",
    )

    def __init__(self, stage: OwnedDirectoryStage, ownership: object) -> None:
        self._stage: OwnedDirectoryStage | None = stage
        self._ownership = ownership
        self._root_descriptor = -1
        self._search_path: Path | None = None
        self._closed = False
        try:
            # OwnedDirectoryStage acquired and installed this descriptor in its
            # owner during construction. Borrow it instead of opening another
            # cancellation-sensitive handle at a Python call boundary.
            descriptor = stage._descriptor  # type: ignore[attr-defined]
            if descriptor < 0:
                raise RuntimeError("private Zoekt shard snapshot owner is closed")
            opened = os.fstat(descriptor)
            expected = directory_ownership_root_identity(ownership)  # type: ignore[arg-type]
            observed = (
                opened.st_dev,
                opened.st_ino,
                stat.S_IFMT(opened.st_mode),
                getattr(opened, "st_file_attributes", 0),
            )
            if observed != expected:
                raise RuntimeError("private Zoekt shard snapshot root changed")
            self._root_descriptor = descriptor
            if sys.platform.startswith("linux"):
                search_path = Path("/proc") / str(os.getpid()) / "fd" / str(descriptor)
                if not search_path.is_dir():
                    raise RuntimeError(
                        "descriptor-anchored Zoekt runtime paths are unavailable"
                    )
                self._search_path = search_path
            else:
                raise RuntimeError(
                    "authenticated Zoekt runtime snapshots require Linux /proc"
                )
        except BaseException:
            self._root_descriptor = -1
            raise

    @property
    def search_path(self) -> str:
        """Return a descriptor-anchored path after rechecking the snapshot."""

        self.verify()
        assert self._search_path is not None
        return str(self._search_path)

    @property
    def closed(self) -> bool:
        return self._closed

    def verify(self) -> None:
        if self._closed or self._stage is None or self._root_descriptor < 0:
            raise RuntimeError("Zoekt shard snapshot is closed")
        opened = os.fstat(self._root_descriptor)
        expected = directory_ownership_root_identity(self._ownership)  # type: ignore[arg-type]
        observed = (
            opened.st_dev,
            opened.st_ino,
            stat.S_IFMT(opened.st_mode),
            getattr(opened, "st_file_attributes", 0),
        )
        if observed != expected:
            raise RuntimeError("private Zoekt shard snapshot root changed")
        current = capture_directory_ownership(
            self._stage.path,
            entry_policy=_flat_file_policy,
        )
        require_publishable_directory_ownership(
            current,
            label="private Zoekt shard snapshot",
        )
        if current != self._ownership:
            raise RuntimeError("private Zoekt shard snapshot changed")
        assert self._search_path is not None
        anchored = os.stat(self._search_path)
        anchored_identity = (
            anchored.st_dev,
            anchored.st_ino,
            stat.S_IFMT(anchored.st_mode),
            getattr(anchored, "st_file_attributes", 0),
        )
        if anchored_identity != expected:
            raise RuntimeError("descriptor-anchored Zoekt snapshot path changed")

    def close(self) -> None:
        if self._closed:
            return
        failure: BaseException | None = None
        if self._stage is not None:
            try:
                self._stage.discard()
            except BaseException as exc:  # noqa: B036 - retain stage for retry
                failure = exc
                self._root_descriptor = self._stage._descriptor  # type: ignore[attr-defined]
            else:
                self._stage = None
                self._root_descriptor = -1
        if self._stage is None:
            self._closed = True
        if failure is not None:
            raise failure


class _ZoektStageOwner:
    """Retain a stage before its cancellation-sensitive constructor runs."""

    __slots__ = ("stage",)

    def __init__(self) -> None:
        self.stage: OwnedDirectoryStage | None = None

    def retain(self, stage: OwnedDirectoryStage) -> None:
        if self.stage is not None and self.stage is not stage:
            raise RuntimeError("private Zoekt stage ownership changed")
        self.stage = stage


class _OwnedZoektStage(OwnedDirectoryStage):
    """Install stage ownership before delegating resource acquisition."""

    def __init__(self, owner: _ZoektStageOwner, destination: Path) -> None:
        owner.retain(self)
        super().__init__(destination)


def capture_authenticated_zoekt_snapshot(
    entry: IndexEntry,
    *,
    install: Callable[[ZoektShardSnapshot], None] | None = None,
) -> ZoektShardSnapshot:
    """Copy exact receipt-bound shards into a retained private generation."""

    receipt = require_zoekt_manifest_receipt(entry)
    source_path = Path(entry.path)
    ownership = capture_directory_ownership(
        source_path,
        entry_policy=_flat_file_policy,
    )
    require_publishable_directory_ownership(ownership, label="Zoekt shard tree")
    _require_ownership_receipt(ownership, receipt)

    destination = Path(tempfile.gettempdir()) / (
        f"codenib-zoekt-runtime-{os.getpid()}-{secrets.token_hex(12)}"
    )
    stage_owner = _ZoektStageOwner()
    stage: OwnedDirectoryStage | None = None
    snapshot: ZoektShardSnapshot | None = None
    try:
        _OwnedZoektStage(stage_owner, destination)
        stage = stage_owner.stage
        if stage is None:
            raise RuntimeError("private Zoekt stage constructor lost ownership")

        def copy_shards(reader: PublicationDirectoryReader) -> None:
            for record in reader.file_records():
                relative = PurePosixPath(record.path)
                with reader.iter_authenticated_chunks(
                    relative,
                    max_bytes=record.size,
                ) as chunks:
                    stage.write_file(
                        relative,
                        chunks,
                        mode=record.mode,
                        max_bytes=record.size,
                    )

        reopen_authenticated_directory(source_path, ownership, copy_shards)
        snapshot_ownership = stage.capture_ownership()
        require_publishable_directory_ownership(
            snapshot_ownership,
            label="private Zoekt shard snapshot",
        )
        _require_ownership_receipt(snapshot_ownership, receipt)
        snapshot = ZoektShardSnapshot(stage, snapshot_ownership)
        snapshot.verify()
        if install is not None:
            install(snapshot)
        return snapshot
    except BaseException as primary:  # noqa: B036 - clean cancellation too
        try:
            if snapshot is None:
                owned_stage = stage_owner.stage
                if owned_stage is not None:
                    owned_stage.discard()
            else:
                snapshot.close()
        except BaseException as cleanup:  # noqa: B036 - keep primary failure
            _annotate_cleanup(primary, cleanup)
        raise


__all__ = [
    "ZOEKT_BUILDER_SCHEMA",
    "ZOEKT_SHARD_TREE_RECEIPT_SCHEMA",
    "ZoektShardSnapshot",
    "capture_authenticated_zoekt_snapshot",
    "require_zoekt_manifest_receipt",
]
