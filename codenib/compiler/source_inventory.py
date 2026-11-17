# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Per-file content identity of the source one index generation was built from.

A source fingerprint says *whether* the indexed source still matches the
checkout. It cannot say *which files* differ. The inventory keeps one content
digest per source file next to the manifest, so a reader holding an older
generation can name exactly the files that changed since it was built.

The inventory is content-addressed and bound to its manifest entry through a
receipt (schema, digest, file count). A reader never trusts the file without
that receipt, and never resolves a caller-supplied path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

SOURCE_INVENTORY_SCHEMA = 1
SOURCE_INVENTORY_DIR = "source_inventory"
SOURCE_INVENTORY_METADATA_KEY = "source_inventory"
_MAX_INVENTORY_BYTES = 256 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class SourceDelta:
    """Source paths that differ between an indexed state and a later one."""

    modified: tuple[str, ...]
    added: tuple[str, ...]
    deleted: tuple[str, ...]


def _inventory_path(cache_dir: str | Path, digest: str) -> Path:
    if not isinstance(digest, str) or _SHA256.match(digest) is None:
        raise ValueError("source inventory receipt has no canonical digest")
    return Path(cache_dir) / SOURCE_INVENTORY_DIR / f"{digest}.json"


def write_source_inventory(
    cache_dir: str | Path,
    *,
    source_fingerprint: str,
    file_digests: Mapping[str, str],
) -> dict[str, Any]:
    """Persist one inventory and return the receipt that binds it."""

    payload = json.dumps(
        {
            "schema": SOURCE_INVENTORY_SCHEMA,
            "source_fingerprint": source_fingerprint,
            "files": dict(sorted(file_digests.items())),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    path = _inventory_path(cache_dir, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.stat().st_size != len(payload):
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    return {
        "schema": SOURCE_INVENTORY_SCHEMA,
        "sha256": digest,
        "file_count": len(file_digests),
    }


def load_source_inventory(
    cache_dir: str | Path,
    receipt: object,
    *,
    source_fingerprint: str,
) -> dict[str, str]:
    """Return ``{path: sha256}`` for the receipted inventory, or raise."""

    if not isinstance(receipt, Mapping):
        raise ValueError("index entry has no source inventory receipt")
    if receipt.get("schema") != SOURCE_INVENTORY_SCHEMA:
        raise ValueError("source inventory schema is unsupported")
    path = _inventory_path(cache_dir, receipt.get("sha256"))
    if path.is_symlink() or not path.is_file():
        raise ValueError("source inventory is missing")
    if path.stat().st_size > _MAX_INVENTORY_BYTES:
        raise ValueError("source inventory is too large")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != receipt["sha256"]:
        raise ValueError("source inventory does not match its receipt")
    data = json.loads(payload)
    files = data.get("files") if isinstance(data, dict) else None
    if (
        not isinstance(files, dict)
        or data.get("schema") != SOURCE_INVENTORY_SCHEMA
        or data.get("source_fingerprint") != source_fingerprint
        or len(files) != receipt.get("file_count")
        or not all(
            isinstance(name, str) and isinstance(value, str)
            for name, value in files.items()
        )
    ):
        raise ValueError("source inventory does not describe the indexed source")
    return files


def prune_source_inventories(cache_dir: str | Path, keep: Iterable[str]) -> None:
    """Remove inventories no manifest entry refers to any more."""

    kept = {f"{digest}.json" for digest in keep}
    directory = Path(cache_dir) / SOURCE_INVENTORY_DIR
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if name in kept or not name.endswith((".json", ".tmp")):
            continue
        try:
            os.unlink(directory / name)
        except OSError:
            pass


def source_delta(
    indexed: Mapping[str, str],
    current: Mapping[str, str],
) -> SourceDelta:
    """Name the paths whose content differs between two inventories."""

    return SourceDelta(
        modified=tuple(
            sorted(
                path
                for path, digest in indexed.items()
                if path in current and current[path] != digest
            )
        ),
        added=tuple(sorted(path for path in current if path not in indexed)),
        deleted=tuple(sorted(path for path in indexed if path not in current)),
    )
