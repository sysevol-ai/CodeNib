# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CLI-private, read-only diagnostics for one SQLite catalog and local CAS."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from pathlib import Path

from .cas import _CAS_OBJECT_MODE, _open_child_directory, _open_directory_path
from .models import StorageIntegrityError, StorageValidationError
from .sqlite_catalog import LATEST_SCHEMA_VERSION

_AUDIT_CONTRACT = "codenib.local-storage-audit.v1"
_MAX_SAMPLE_LIMIT = 1_000
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SHARD_RE = re.compile(r"[0-9a-f]{2}\Z", re.ASCII)
_OBJECT_NAME_RE = re.compile(r"[0-9a-f]{62}\Z", re.ASCII)
_REACHABILITY = (
    "current_ref",
    "historical_snapshot",
    "generation_only",
    "unbound_registered",
)
_CAS_OBSERVATION_KINDS = (
    "present",
    "missing",
    "size_mismatch",
    "unregistered",
    "invalid",
)

_REACHABILITY_SQL = """
    WITH generation_objects(view_generation_id, digest) AS (
        SELECT view_generation_id, object_digest
        FROM view_generations
        UNION
        SELECT view_generation_id, object_digest
        FROM view_generation_objects
    ),
    current_objects(digest) AS (
        SELECT DISTINCT generation.digest
        FROM refs AS ref
        JOIN snapshot_views AS snapshot_view
            ON snapshot_view.snapshot_id = ref.snapshot_id
        JOIN generation_objects AS generation
            ON generation.view_generation_id = snapshot_view.view_generation_id
    ),
    retained_snapshot_objects(digest) AS (
        SELECT DISTINCT generation.digest
        FROM snapshots AS snapshot
        JOIN snapshot_views AS snapshot_view
            ON snapshot_view.snapshot_id = snapshot.snapshot_id
        JOIN generation_objects AS generation
            ON generation.view_generation_id = snapshot_view.view_generation_id
        WHERE snapshot.status = 'ready'
    ),
    attached_objects(digest) AS (
        SELECT DISTINCT digest FROM generation_objects
    )
    SELECT
        object.digest,
        object.storage_key,
        object.byte_size,
        CASE
            WHEN current.digest IS NOT NULL THEN 'current_ref'
            WHEN retained.digest IS NOT NULL THEN 'historical_snapshot'
            WHEN attached.digest IS NOT NULL THEN 'generation_only'
            ELSE 'unbound_registered'
        END AS reachability
    FROM objects AS object
    LEFT JOIN current_objects AS current
        ON current.digest = object.digest
    LEFT JOIN retained_snapshot_objects AS retained
        ON retained.digest = object.digest
    LEFT JOIN attached_objects AS attached
        ON attached.digest = object.digest
    ORDER BY object.digest
"""


class _Bucket:
    __slots__ = ("count", "expected_bytes", "observed_bytes", "samples")

    def __init__(self) -> None:
        self.count = 0
        self.expected_bytes = 0
        self.observed_bytes = 0
        self.samples: list[str] = []

    def add(
        self,
        sample: str,
        *,
        sample_limit: int,
        expected_bytes: int = 0,
        observed_bytes: int = 0,
    ) -> None:
        self.count += 1
        self.expected_bytes += expected_bytes
        self.observed_bytes += observed_bytes
        if len(self.samples) < sample_limit:
            self.samples.append(sample)


def _canonical_storage_key(digest: str) -> str:
    return f"sha256/{digest[:2]}/{digest[2:]}"


def _read_catalog_snapshot(
    path: Path,
) -> tuple[int, tuple[tuple[object, object, object, str], ...]]:
    """Read the current schema through one caller-owned private snapshot."""

    target = f"{path.as_uri()}?mode=ro"
    connection = sqlite3.connect(target, isolation_level=None, uri=True)
    try:
        version_row = connection.execute("PRAGMA user_version").fetchone()
        if version_row is None or type(version_row[0]) is not int:
            raise ValueError("SQLite audit snapshot has no canonical schema version")
        version = version_row[0]
        if version != LATEST_SCHEMA_VERSION:
            raise ValueError(
                "storage audit requires the current SQLite catalog schema; "
                "open it through a normal CodeNib command to migrate it first"
            )
        rows = connection.execute(_REACHABILITY_SQL).fetchall()
    finally:
        connection.close()
    return version, tuple((row[0], row[1], row[2], row[3]) for row in rows)


def _directory_names(descriptor: int) -> list[str]:
    with os.scandir(descriptor) as entries:
        return sorted(entry.name for entry in entries)


def _entry_metadata(descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _scan_shard(
    descriptor: int,
    shard_name: str,
    registered: dict[str, tuple[int, bool]],
    buckets: dict[str, _Bucket],
    observed: set[str],
    *,
    sample_limit: int,
) -> None:
    for object_name in _directory_names(descriptor):
        storage_key = f"sha256/{shard_name}/{object_name}"
        metadata = _entry_metadata(descriptor, object_name)
        if metadata is None:
            buckets["invalid"].add(storage_key, sample_limit=sample_limit)
            continue
        if _OBJECT_NAME_RE.fullmatch(object_name) is None:
            buckets["invalid"].add(
                storage_key,
                sample_limit=sample_limit,
                observed_bytes=(
                    metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0
                ),
            )
            continue

        digest = f"{shard_name}{object_name}"
        observed.add(digest)
        record = registered.get(digest)
        canonical_file = (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) == _CAS_OBJECT_MODE
        )
        if not canonical_file or record is not None and not record[1]:
            buckets["invalid"].add(
                storage_key,
                sample_limit=sample_limit,
                expected_bytes=record[0] if record is not None else 0,
                observed_bytes=(
                    metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0
                ),
            )
        elif record is None:
            buckets["unregistered"].add(
                storage_key,
                sample_limit=sample_limit,
                observed_bytes=metadata.st_size,
            )
        elif metadata.st_size != record[0]:
            buckets["size_mismatch"].add(
                storage_key,
                sample_limit=sample_limit,
                expected_bytes=record[0],
                observed_bytes=metadata.st_size,
            )
        else:
            buckets["present"].add(
                storage_key,
                sample_limit=sample_limit,
                expected_bytes=record[0],
                observed_bytes=metadata.st_size,
            )


def _scan_canonical_files(
    root: Path,
    registered: dict[str, tuple[int, bool]],
    buckets: dict[str, _Bucket],
    *,
    sample_limit: int,
) -> set[str]:
    """Scan below descriptor-bound directories without following replacements."""

    observed: set[str] = set()
    expected_layout_errors = (
        FileNotFoundError,
        NotADirectoryError,
        StorageIntegrityError,
        StorageValidationError,
        ValueError,
    )
    try:
        with _open_directory_path(root, label="CAS root") as root_descriptor:
            if root_descriptor is None:
                raise RuntimeError(
                    "storage audit requires descriptor-bound LocalCAS support"
                )
            try:
                with _open_child_directory(
                    root_descriptor,
                    root,
                    "sha256",
                    label="CAS SHA-256 root",
                ) as (sha256_descriptor, sha256_path):
                    assert sha256_descriptor is not None
                    for shard_name in _directory_names(sha256_descriptor):
                        shard_path = f"sha256/{shard_name}"
                        metadata = _entry_metadata(sha256_descriptor, shard_name)
                        if (
                            metadata is None
                            or _SHARD_RE.fullmatch(shard_name) is None
                            or not stat.S_ISDIR(metadata.st_mode)
                        ):
                            buckets["invalid"].add(
                                shard_path,
                                sample_limit=sample_limit,
                            )
                            continue
                        try:
                            with _open_child_directory(
                                sha256_descriptor,
                                sha256_path,
                                shard_name,
                                label="CAS digest shard",
                            ) as (shard_descriptor, _):
                                assert shard_descriptor is not None
                                _scan_shard(
                                    shard_descriptor,
                                    shard_name,
                                    registered,
                                    buckets,
                                    observed,
                                    sample_limit=sample_limit,
                                )
                        except expected_layout_errors:
                            buckets["invalid"].add(
                                shard_path,
                                sample_limit=sample_limit,
                            )
            except expected_layout_errors:
                buckets["invalid"].add("sha256", sample_limit=sample_limit)
    except expected_layout_errors:
        buckets["invalid"].add(".", sample_limit=sample_limit)
    return observed


def _audit_local_storage_snapshot(
    catalog_snapshot: str | Path,
    cas_root: str | Path,
    *,
    sample_limit: int = 20,
) -> dict[str, object]:
    """Observe one private catalog snapshot and LocalCAS without mutation.

    The filesystem walk is sequential and writers are not quiesced. Concurrent
    publication can therefore appear as unregistered or invalid temporary
    entries. No result is a deletion or reclaimability gate.
    """

    if type(sample_limit) is not int or not 0 <= sample_limit <= _MAX_SAMPLE_LIMIT:
        raise ValueError(f"sample_limit must be between 0 and {_MAX_SAMPLE_LIMIT}")

    version, rows = _read_catalog_snapshot(Path(catalog_snapshot).expanduser())
    reachability_buckets = {name: _Bucket() for name in _REACHABILITY}
    cas_buckets = {name: _Bucket() for name in _CAS_OBSERVATION_KINDS}
    registered: dict[str, tuple[int, bool]] = {}
    for digest, storage_key, byte_size, category in rows:
        sample_digest = digest if type(digest) is str else repr(digest)
        expected_size = byte_size if type(byte_size) is int and byte_size >= 0 else 0
        reachability_buckets[category].add(
            sample_digest,
            sample_limit=sample_limit,
            expected_bytes=expected_size,
        )
        valid_digest = type(digest) is str and _DIGEST_RE.fullmatch(digest) is not None
        canonical_key = _canonical_storage_key(digest) if valid_digest else None
        metadata_valid = (
            valid_digest
            and type(storage_key) is str
            and storage_key == canonical_key
            and type(byte_size) is int
            and byte_size >= 0
        )
        if valid_digest:
            registered[digest] = (expected_size, metadata_valid)
        else:
            cas_buckets["invalid"].add(
                f"catalog:{sample_digest}",
                sample_limit=sample_limit,
                expected_bytes=expected_size,
            )

    observed = _scan_canonical_files(
        Path(cas_root).expanduser(),
        registered,
        cas_buckets,
        sample_limit=sample_limit,
    )
    for digest, (byte_size, metadata_valid) in registered.items():
        if digest in observed:
            continue
        status = "missing" if metadata_valid else "invalid"
        cas_buckets[status].add(
            _canonical_storage_key(digest),
            sample_limit=sample_limit,
            expected_bytes=byte_size,
        )

    reachability = {
        name: {
            "object_count": reachability_buckets[name].count,
            "registered_bytes": reachability_buckets[name].expected_bytes,
            "sample_digests": list(reachability_buckets[name].samples),
        }
        for name in _REACHABILITY
    }
    cas_observations = {
        name: {
            "count": cas_buckets[name].count,
            "expected_bytes": cas_buckets[name].expected_bytes,
            "observed_bytes": cas_buckets[name].observed_bytes,
            "samples": list(cas_buckets[name].samples),
        }
        for name in _CAS_OBSERVATION_KINDS
    }
    return {
        "contract": _AUDIT_CONTRACT,
        "mode": "metadata_only",
        "cross_store_atomic": False,
        "content_hashes_verified": False,
        "writers_quiesced": False,
        "sample_limit": sample_limit,
        "catalog": {
            "schema_version": version,
            "object_count": sum(
                bucket.count for bucket in reachability_buckets.values()
            ),
            "registered_bytes": sum(
                bucket.expected_bytes for bucket in reachability_buckets.values()
            ),
            "reachability": reachability,
        },
        "cas": {"observations": cas_observations},
        "reclamation": {
            "assessed": False,
            "reclaimable_bytes": None,
        },
    }
