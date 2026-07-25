"""Schema and validation for content-locked evaluation bundle manifests."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

MANIFEST_SCHEMA_VERSION = 1
LOCK_SCHEMA_VERSION = 1
PROVENANCE_SCHEMA_VERSION = 1
CHECKSUM_FILE = "SHA256SUMS"
MANIFEST_FILE = "BUNDLE_MANIFEST.json"
LOCK_FILE = "SOURCE_LOCK.json"
PROVENANCE_FILE = "PROVENANCE.json"
_RESERVED_PATHS = frozenset({CHECKSUM_FILE, MANIFEST_FILE, LOCK_FILE, PROVENANCE_FILE})
_ROOT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class ArtifactBundleError(ValueError):
    """Raised when a bundle manifest, source, or payload is invalid."""


class ArtifactIntegrityError(ArtifactBundleError):
    """Raised when content does not match its lock or checksum manifest."""


@dataclass(frozen=True)
class SourceSpec:
    """One selected source tree and its destination inside a bundle."""

    source_id: str
    root: str
    path: PurePosixPath
    destination: PurePosixPath
    include: tuple[str, ...] = ("**",)
    exclude: tuple[str, ...] = ()
    exclude_dir_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class BundleManifest:
    """Validated artifact bundle manifest."""

    path: Path
    raw: bytes
    name: str
    version: str
    sources: tuple[SourceSpec, ...]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


def load_manifest(path: Path | str) -> BundleManifest:
    """Load and validate a JSON bundle manifest."""

    manifest_path = Path(path).expanduser().resolve(strict=True)
    raw = manifest_path.read_bytes()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArtifactBundleError(f"invalid JSON manifest: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ArtifactBundleError("bundle manifest must be a JSON object")
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ArtifactBundleError(
            f"unsupported bundle manifest schema: {payload.get('schema_version')!r}"
        )

    bundle = payload.get("bundle")
    if not isinstance(bundle, Mapping):
        raise ArtifactBundleError("bundle manifest requires a 'bundle' object")
    name = _required_string(bundle, "name", context="bundle")
    version = _required_string(bundle, "version", context="bundle")

    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ArtifactBundleError("bundle manifest requires a non-empty source list")
    sources = tuple(
        _parse_source(item, index) for index, item in enumerate(raw_sources)
    )
    _validate_source_set(sources)
    return BundleManifest(
        path=manifest_path,
        raw=raw,
        name=name,
        version=version,
        sources=sources,
    )


def load_source_lock(path: Path | str, manifest: BundleManifest) -> Mapping[str, Any]:
    """Load a source lock and ensure it belongs to the supplied manifest."""

    lock_path = Path(path).expanduser().resolve(strict=True)
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactBundleError(f"invalid JSON source lock: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ArtifactBundleError("source lock must be a JSON object")
    if payload.get("schema_version") != LOCK_SCHEMA_VERSION:
        raise ArtifactBundleError(
            f"unsupported source lock schema: {payload.get('schema_version')!r}"
        )
    if payload.get("manifest_sha256") != manifest.sha256:
        raise ArtifactIntegrityError("source lock does not match the bundle manifest")
    sources = payload.get("sources")
    if not isinstance(sources, Mapping):
        raise ArtifactBundleError("source lock requires a 'sources' object")
    expected_ids = {spec.source_id for spec in manifest.sources}
    locked_ids = set(sources)
    if locked_ids != expected_ids:
        missing = sorted(expected_ids - locked_ids)
        extra = sorted(locked_ids - expected_ids)
        raise ArtifactIntegrityError(
            f"source lock IDs differ; missing={missing}, extra={extra}"
        )
    return payload


def parse_root_arguments(values: Iterable[str]) -> dict[str, Path]:
    """Parse repeated ``NAME=PATH`` command-line root bindings."""

    roots = {}
    for value in values:
        if "=" not in value:
            raise ArtifactBundleError(f"invalid --root {value!r}; expected NAME=/path")
        name, raw_path = value.split("=", 1)
        if not _ROOT_NAME.fullmatch(name) or not raw_path:
            raise ArtifactBundleError(f"invalid --root {value!r}; expected NAME=/path")
        if name in roots:
            raise ArtifactBundleError(f"duplicate --root name: {name}")
        roots[name] = Path(raw_path)
    return roots


def _parse_source(payload: Any, index: int) -> SourceSpec:
    if not isinstance(payload, Mapping):
        raise ArtifactBundleError(f"source {index} must be a JSON object")
    context = f"source {index}"
    source_id = _required_string(payload, "id", context=context)
    root = _required_string(payload, "root", context=context)
    if not _ROOT_NAME.fullmatch(root):
        raise ArtifactBundleError(f"{context} has invalid root name {root!r}")
    path = _relative_path(_required_string(payload, "path", context=context), "path")
    destination = _relative_path(
        _required_string(payload, "destination", context=context), "destination"
    )
    if destination == PurePosixPath("."):
        raise ArtifactBundleError(f"{context} destination cannot be the bundle root")
    return SourceSpec(
        source_id=source_id,
        root=root,
        path=path,
        destination=destination,
        include=_patterns(payload.get("include", ["**"]), "include", context),
        exclude=_patterns(payload.get("exclude", []), "exclude", context),
        exclude_dir_names=_directory_names(
            payload.get("exclude_dir_names", []), context
        ),
    )


def _validate_source_set(sources: Sequence[SourceSpec]) -> None:
    ids = [source.source_id for source in sources]
    duplicates = sorted({source_id for source_id in ids if ids.count(source_id) > 1})
    if duplicates:
        raise ArtifactBundleError(f"duplicate source IDs: {duplicates}")
    destinations = []
    for source in sources:
        destination = source.destination
        if destination.parts[0] in _RESERVED_PATHS:
            raise ArtifactBundleError(
                f"source {source.source_id!r} uses reserved destination {destination}"
            )
        for other_id, other in destinations:
            if _path_contains(destination, other) or _path_contains(other, destination):
                raise ArtifactBundleError(
                    "overlapping bundle destinations: "
                    f"{other_id!r}={other} and {source.source_id!r}={destination}"
                )
        destinations.append((source.source_id, destination))


def _required_string(payload: Mapping[str, Any], key: str, *, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ArtifactBundleError(f"{context} requires non-empty string {key!r}")
    return value.strip()


def _relative_path(value: str, field: str) -> PurePosixPath:
    if "\\" in value:
        raise ArtifactBundleError(f"{field} must use POSIX separators: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ArtifactBundleError(f"{field} must stay relative: {value!r}")
    return path if path.parts else PurePosixPath(".")


def _patterns(value: Any, field: str, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ArtifactBundleError(f"{context} {field} must be a string list")
    for pattern in value:
        if pattern.startswith("/") or ".." in PurePosixPath(pattern).parts:
            raise ArtifactBundleError(
                f"{context} {field} pattern must stay relative: {pattern!r}"
            )
    return tuple(value)


def _directory_names(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str)
        and item
        and "/" not in item
        and "\\" not in item
        and item not in {".", ".."}
        for item in value
    ):
        raise ArtifactBundleError(
            f"{context} exclude_dir_names must contain plain directory names"
        )
    return tuple(value)


def _path_contains(parent: PurePosixPath, child: PurePosixPath) -> bool:
    return parent == child or parent in child.parents
