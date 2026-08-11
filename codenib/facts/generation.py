# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Durable catalog generations composed from immutable per-file facts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from ..storage.models import canonical_json
from ..storage.protocols import IndexCatalog, ObjectStore
from .clangd import CLANGD_FACT_PROVIDER, ClangdFactProfile
from .model import FACT_BATCH_SCHEMA_VERSION, FactBatch, sha256_digest
from .storage import (
    FACT_BATCH_ARTIFACT_MEDIA_TYPE,
    FactBatchArtifactReceipt,
    FactBatchReuseKey,
    load_fact_batch,
    publish_fact_batch,
)

FACT_BATCH_GENERATION_SCHEMA = "codenib.fact-batch-generation.v1"
FACT_BATCH_GENERATION_MEDIA_TYPE = "application/vnd.codenib.fact-batch-generation+json"
FACT_BATCH_VIEW_TYPE = "semantic_fact_batches"
DEFAULT_FACT_BATCH_REF = "semantic-facts"
DEFAULT_MAX_FACT_BATCH_GENERATION_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_FACT_BATCH_UNITS = 200_000
DEFAULT_MAX_FACT_DEFINITIONS = 5_000_000

_MANIFEST_FIELDS = frozenset(
    {
        "artifact_schema",
        "repository_id",
        "source_revision_id",
        "profile_digest",
        "provider",
        "fact_batch_schema_version",
        "batch_set_digest",
        "units",
        "definitions",
    }
)
_UNIT_FIELDS = frozenset({"reuse_key", "receipt"})
_DEFINITION_FIELDS = frozenset({"moniker", "file_path", "symbol_id"})


class FactBatchGenerationError(RuntimeError):
    """A generation manifest or publication failed closed validation."""


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty")
    if value != value.strip() or "\x00" in value:
        raise ValueError(f"{field} must be canonical text")
    return value


def _canonical_path(value: Any) -> str:
    text = _required_text(value, "file_path")
    path = PurePosixPath(text)
    if (
        "\\" in text
        or path.is_absolute()
        or path.as_posix() != text
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("file_path must be a canonical repository-relative path")
    return text


def _fact_digest(value: Any, field: str) -> str:
    text = _required_text(value, field)
    if (
        not text.startswith("sha256:")
        or len(text) != 71
        or any(character not in "0123456789abcdef" for character in text[7:])
    ):
        raise ValueError(f"{field} must use canonical sha256:<64 lowercase hex>")
    return text


def _exact_fields(
    payload: Mapping[str, Any], expected: frozenset[str], label: str
) -> None:
    actual = frozenset(payload)
    if actual != expected:
        raise FactBatchGenerationError(
            f"{label} fields differ; missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise FactBatchGenerationError(f"duplicate JSON object key: {key!r}")
        output[key] = value
    return output


def _reject_nonfinite(value: str) -> None:
    raise FactBatchGenerationError(f"non-finite JSON number is forbidden: {value}")


@dataclass(frozen=True, slots=True)
class FactBatchGenerationUnit:
    reuse_key: FactBatchReuseKey
    receipt: FactBatchArtifactReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.reuse_key, FactBatchReuseKey):
            raise TypeError("reuse_key must be a FactBatchReuseKey")
        if not isinstance(self.receipt, FactBatchArtifactReceipt):
            raise TypeError("receipt must be a FactBatchArtifactReceipt")
        if self.receipt.reuse_key_digest != self.reuse_key.digest:
            raise ValueError("FactBatch unit receipt does not match its reuse key")

    @property
    def file_path(self) -> str:
        return self.reuse_key.file_path

    def to_dict(self) -> dict[str, Any]:
        return {
            "reuse_key": self.reuse_key.to_dict(),
            "receipt": self.receipt.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FactBatchGenerationUnit":
        if not isinstance(payload, Mapping):
            raise FactBatchGenerationError("generation unit must be an object")
        _exact_fields(payload, _UNIT_FIELDS, "generation unit")
        try:
            return cls(
                reuse_key=FactBatchReuseKey.from_dict(payload["reuse_key"]),
                receipt=FactBatchArtifactReceipt.from_dict(payload["receipt"]),
            )
        except (TypeError, ValueError) as exc:
            raise FactBatchGenerationError(f"invalid generation unit: {exc}") from exc


@dataclass(frozen=True, slots=True, order=True)
class FactDefinitionPointer:
    moniker: str
    file_path: str
    symbol_id: str

    def __post_init__(self) -> None:
        _required_text(self.moniker, "definition moniker")
        object.__setattr__(self, "file_path", _canonical_path(self.file_path))
        _required_text(self.symbol_id, "definition symbol_id")

    def to_dict(self) -> dict[str, str]:
        return {
            "moniker": self.moniker,
            "file_path": self.file_path,
            "symbol_id": self.symbol_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FactDefinitionPointer":
        if not isinstance(payload, Mapping):
            raise FactBatchGenerationError("definition pointer must be an object")
        _exact_fields(payload, _DEFINITION_FIELDS, "definition pointer")
        try:
            return cls(**payload)
        except (TypeError, ValueError) as exc:
            raise FactBatchGenerationError(
                f"invalid definition pointer: {exc}"
            ) from exc


def _batch_set_digest(units: Sequence[FactBatchGenerationUnit]) -> str:
    return sha256_digest(
        canonical_json(
            {
                "units": [
                    {
                        "file_path": unit.file_path,
                        "batch_digest": unit.receipt.batch_digest,
                    }
                    for unit in units
                ]
            }
        )
    )


@dataclass(frozen=True, slots=True)
class FactBatchGenerationManifest:
    repository_id: str
    source_revision_id: str
    profile_digest: str
    provider: str
    units: tuple[FactBatchGenerationUnit, ...]
    definitions: tuple[FactDefinitionPointer, ...]
    batch_set_digest: str
    fact_batch_schema_version: int = FACT_BATCH_SCHEMA_VERSION
    artifact_schema: str = FACT_BATCH_GENERATION_SCHEMA

    def __post_init__(self) -> None:
        _required_text(self.repository_id, "repository_id")
        _required_text(self.source_revision_id, "source_revision_id")
        _required_text(self.provider, "provider")
        if self.artifact_schema != FACT_BATCH_GENERATION_SCHEMA:
            raise ValueError("unsupported FactBatch generation schema")
        if (
            isinstance(self.fact_batch_schema_version, bool)
            or not isinstance(self.fact_batch_schema_version, int)
            or self.fact_batch_schema_version != FACT_BATCH_SCHEMA_VERSION
        ):
            raise ValueError("unsupported FactBatch schema in generation")
        object.__setattr__(
            self,
            "profile_digest",
            _fact_digest(self.profile_digest, "profile_digest"),
        )
        object.__setattr__(
            self,
            "batch_set_digest",
            _fact_digest(self.batch_set_digest, "batch_set_digest"),
        )
        ordered_units = tuple(sorted(self.units, key=lambda unit: unit.file_path))
        if ordered_units != self.units:
            raise ValueError("FactBatch generation units must be path-sorted")
        paths = [unit.file_path for unit in self.units]
        if len(paths) != len(set(paths)):
            raise ValueError("FactBatch generation paths must be unique")
        if len(self.units) > DEFAULT_MAX_FACT_BATCH_UNITS:
            raise ValueError("FactBatch generation exceeds the unit limit")
        for unit in self.units:
            if (
                unit.reuse_key.profile_digest != self.profile_digest
                or unit.reuse_key.provider != self.provider
            ):
                raise ValueError(
                    "FactBatch unit profile/provider differs from manifest"
                )
        ordered_definitions = tuple(sorted(set(self.definitions)))
        if ordered_definitions != self.definitions:
            raise ValueError("FactBatch definitions must be unique and sorted")
        if len(self.definitions) > DEFAULT_MAX_FACT_DEFINITIONS:
            raise ValueError("FactBatch generation exceeds the definition limit")
        path_set = set(paths)
        if any(pointer.file_path not in path_set for pointer in self.definitions):
            raise ValueError("definition pointer names a file outside the generation")
        if self.batch_set_digest != _batch_set_digest(self.units):
            raise ValueError("FactBatch generation batch-set digest differs")

    @classmethod
    def create(
        cls,
        *,
        repository_id: str,
        source_revision_id: str,
        profile_digest: str,
        provider: str,
        units: Sequence[FactBatchGenerationUnit],
        batches: Sequence[FactBatch],
    ) -> "FactBatchGenerationManifest":
        ordered_units = tuple(sorted(units, key=lambda unit: unit.file_path))
        batch_by_path = {batch.file_path: batch for batch in batches}
        if len(batch_by_path) != len(batches):
            raise ValueError("FactBatch generation contains duplicate batch paths")
        if set(batch_by_path) != {unit.file_path for unit in ordered_units}:
            raise ValueError("FactBatch generation units and batches differ")
        pointers = tuple(
            sorted(
                {
                    FactDefinitionPointer(
                        moniker=symbol.moniker,
                        file_path=batch.file_path,
                        symbol_id=symbol.symbol_id,
                    )
                    for batch in batches
                    for symbol in batch.symbols
                }
            )
        )
        return cls(
            repository_id=repository_id,
            source_revision_id=source_revision_id,
            profile_digest=profile_digest,
            provider=provider,
            units=ordered_units,
            definitions=pointers,
            batch_set_digest=_batch_set_digest(ordered_units),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_schema": self.artifact_schema,
            "repository_id": self.repository_id,
            "source_revision_id": self.source_revision_id,
            "profile_digest": self.profile_digest,
            "provider": self.provider,
            "fact_batch_schema_version": self.fact_batch_schema_version,
            "batch_set_digest": self.batch_set_digest,
            "units": [unit.to_dict() for unit in self.units],
            "definitions": [pointer.to_dict() for pointer in self.definitions],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FactBatchGenerationManifest":
        if not isinstance(payload, Mapping):
            raise FactBatchGenerationError("generation manifest must be an object")
        _exact_fields(payload, _MANIFEST_FIELDS, "generation manifest")
        try:
            units = payload["units"]
            definitions = payload["definitions"]
            if not isinstance(units, list) or not isinstance(definitions, list):
                raise ValueError("units and definitions must be lists")
            return cls(
                artifact_schema=payload["artifact_schema"],
                repository_id=payload["repository_id"],
                source_revision_id=payload["source_revision_id"],
                profile_digest=payload["profile_digest"],
                provider=payload["provider"],
                fact_batch_schema_version=payload["fact_batch_schema_version"],
                batch_set_digest=payload["batch_set_digest"],
                units=tuple(FactBatchGenerationUnit.from_dict(unit) for unit in units),
                definitions=tuple(
                    FactDefinitionPointer.from_dict(pointer) for pointer in definitions
                ),
            )
        except FactBatchGenerationError:
            raise
        except (TypeError, ValueError) as exc:
            raise FactBatchGenerationError(
                f"invalid generation manifest: {exc}"
            ) from exc


def encode_fact_batch_generation(manifest: FactBatchGenerationManifest) -> bytes:
    if not isinstance(manifest, FactBatchGenerationManifest):
        raise TypeError("manifest must be a FactBatchGenerationManifest")
    return canonical_json(manifest.to_dict()).encode("utf-8")


def decode_fact_batch_generation(
    payload: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_FACT_BATCH_GENERATION_BYTES,
) -> FactBatchGenerationManifest:
    if not isinstance(payload, bytes):
        raise TypeError("FactBatch generation payload must be bytes")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if not payload or len(payload) > max_bytes:
        raise FactBatchGenerationError(
            f"FactBatch generation size must be between 1 and {max_bytes} bytes"
        )
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except FactBatchGenerationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactBatchGenerationError(
            f"invalid FactBatch generation JSON: {exc}"
        ) from exc
    manifest = FactBatchGenerationManifest.from_dict(decoded)
    if payload != encode_fact_batch_generation(manifest):
        raise FactBatchGenerationError("FactBatch generation is not canonical JSON")
    return manifest


@dataclass(frozen=True, slots=True)
class LoadedFactBatchGeneration:
    snapshot_id: str
    ref_generation: int
    view_generation_id: str
    manifest_object_digest: str
    manifest: FactBatchGenerationManifest
    batches: tuple[FactBatch, ...]

    @property
    def batch_by_path(self) -> dict[str, FactBatch]:
        return {batch.file_path: batch for batch in self.batches}

    @property
    def unit_by_path(self) -> dict[str, FactBatchGenerationUnit]:
        return {unit.file_path: unit for unit in self.manifest.units}

    @property
    def reachable_object_digests(self) -> frozenset[str]:
        return frozenset(
            {self.manifest_object_digest}
            | {unit.receipt.object_digest for unit in self.manifest.units}
        )


def _verified_object_payload(
    store: ObjectStore,
    object_summary: Mapping[str, Any],
    *,
    expected_media_type: str,
    max_bytes: int,
) -> bytes:
    try:
        digest = object_summary["digest"]
        byte_size = object_summary["byte_size"]
        storage_key = object_summary["storage_key"]
        media_type = object_summary["media_type"]
    except (KeyError, TypeError) as exc:
        raise FactBatchGenerationError("catalog object summary is incomplete") from exc
    if media_type != expected_media_type:
        raise FactBatchGenerationError("catalog object media type differs")
    if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 1:
        raise FactBatchGenerationError("catalog object size is invalid")
    if byte_size > max_bytes:
        raise FactBatchGenerationError("catalog object exceeds its read limit")
    verified = store.verify(digest)
    if (
        verified.digest != digest
        or verified.byte_size != byte_size
        or verified.storage_key != storage_key
    ):
        raise FactBatchGenerationError("catalog object differs from object store")
    payload = store.read_bytes(digest)
    if len(payload) != byte_size or hashlib.sha256(payload).hexdigest() != digest:
        raise FactBatchGenerationError("catalog object bytes differ from receipt")
    return payload


def load_fact_batch_generation(
    catalog: IndexCatalog,
    store: ObjectStore,
    repository_id: str,
    *,
    ref_name: str = DEFAULT_FACT_BATCH_REF,
) -> LoadedFactBatchGeneration:
    resolved = catalog.resolve_ref(repository_id, ref_name)
    manifest_summary = resolved.get("manifest") or {}
    views = manifest_summary.get("views") or {}
    view = views.get(FACT_BATCH_VIEW_TYPE)
    if not isinstance(view, Mapping):
        raise FactBatchGenerationError(
            f"ref {ref_name!r} has no {FACT_BATCH_VIEW_TYPE!r} view"
        )
    if view.get("schema_version") != FACT_BATCH_GENERATION_SCHEMA:
        raise FactBatchGenerationError("FactBatch view generation schema differs")
    payload = _verified_object_payload(
        store,
        view.get("object") or {},
        expected_media_type=FACT_BATCH_GENERATION_MEDIA_TYPE,
        max_bytes=DEFAULT_MAX_FACT_BATCH_GENERATION_BYTES,
    )
    manifest = decode_fact_batch_generation(payload)
    if manifest.repository_id != repository_id or manifest.source_revision_id != (
        manifest_summary.get("source") or {}
    ).get("source_revision_id"):
        raise FactBatchGenerationError(
            "FactBatch manifest source identity differs from catalog snapshot"
        )
    profile = view.get("profile") or {}
    profile_config = profile.get("config") or {}
    compatibility = profile_config.get("compatibility")
    if not isinstance(compatibility, Mapping):
        raise FactBatchGenerationError(
            "FactBatch catalog profile compatibility is missing"
        )
    computed_profile_digest = sha256_digest(canonical_json(dict(compatibility)))
    if (
        profile_config.get("fact_profile_digest") != manifest.profile_digest
        or computed_profile_digest != manifest.profile_digest
        or compatibility.get("provider") != manifest.provider
    ):
        raise FactBatchGenerationError(
            "FactBatch manifest profile differs from catalog profile"
        )
    member_objects = view.get("member_objects")
    if not isinstance(member_objects, list):
        raise FactBatchGenerationError("catalog member object summary is missing")
    catalog_members = {
        member.get("digest") for member in member_objects if isinstance(member, Mapping)
    }
    unit_members = {unit.receipt.object_digest for unit in manifest.units}
    if catalog_members != unit_members or len(member_objects) != len(catalog_members):
        raise FactBatchGenerationError(
            "catalog member reachability differs from FactBatch manifest"
        )

    batches = []
    definitions = []
    for unit in manifest.units:
        batch = load_fact_batch(store, unit.receipt, expected_key=unit.reuse_key)
        if (
            batch.profile_digest != manifest.profile_digest
            or batch.provider != manifest.provider
        ):
            raise FactBatchGenerationError(
                "FactBatch unit profile/provider differs from generation"
            )
        batches.append(batch)
        definitions.extend(
            FactDefinitionPointer(symbol.moniker, batch.file_path, symbol.symbol_id)
            for symbol in batch.symbols
        )
    if tuple(sorted(set(definitions))) != manifest.definitions:
        raise FactBatchGenerationError(
            "FactBatch definition index differs from generation units"
        )
    return LoadedFactBatchGeneration(
        snapshot_id=resolved["snapshot_id"],
        ref_generation=resolved["generation"],
        view_generation_id=view["view_generation_id"],
        manifest_object_digest=view["object"]["digest"],
        manifest=manifest,
        batches=tuple(sorted(batches, key=lambda batch: batch.file_path)),
    )


@dataclass(frozen=True, slots=True)
class FactBatchPublicationResult:
    snapshot_id: str
    ref_generation: int
    changed: bool
    view_generation_id: str
    manifest_object_digest: str
    manifest: FactBatchGenerationManifest
    reused_units: int
    published_units: int


def publish_fact_batch_generation(
    catalog: IndexCatalog,
    store: ObjectStore,
    *,
    repository_id: str,
    source_revision_id: str,
    profile: ClangdFactProfile,
    upserts: Sequence[FactBatch],
    deletes: Iterable[str] = (),
    replace_all: bool = False,
    ref_name: str = DEFAULT_FACT_BATCH_REF,
    expected_generation: int = 0,
    profile_name: str = "clangd",
    additional_view_generation_ids: Sequence[str] = (),
) -> FactBatchPublicationResult:
    """Publish an atomic FactBatch generation, reusing unchanged prior units."""

    if not isinstance(profile, ClangdFactProfile):
        raise TypeError("profile must be a ClangdFactProfile")
    if isinstance(expected_generation, bool) or not isinstance(
        expected_generation, int
    ):
        raise ValueError("expected_generation must be an integer")
    if expected_generation < 0:
        raise ValueError("expected_generation must not be negative")

    previous = None
    if expected_generation:
        previous = load_fact_batch_generation(
            catalog, store, repository_id, ref_name=ref_name
        )
        if previous.ref_generation != expected_generation:
            raise FactBatchGenerationError(
                "FactBatch ref generation changed before incremental publication"
            )
    previous_batches = previous.batch_by_path if previous is not None else {}
    previous_units = previous.unit_by_path if previous is not None else {}
    if (
        previous is not None
        and not replace_all
        and (
            previous.manifest.profile_digest != profile.digest
            or previous.manifest.provider != CLANGD_FACT_PROVIDER
        )
    ):
        raise FactBatchGenerationError(
            "profile/provider changes require a complete FactBatch rebuild"
        )

    deleted = {_canonical_path(path) for path in deletes}
    incoming: dict[str, FactBatch] = {}
    for batch in upserts:
        if not isinstance(batch, FactBatch):
            raise TypeError("FactBatch upserts must contain only FactBatch values")
        if batch.file_path in incoming:
            raise ValueError(f"duplicate FactBatch upsert path {batch.file_path!r}")
        if (
            batch.language != "cpp"
            or batch.profile_digest != profile.digest
            or batch.provider != CLANGD_FACT_PROVIDER
            or batch.position_encoding != profile.position_encoding
        ):
            raise ValueError(
                f"FactBatch {batch.file_path!r} differs from the clangd profile"
            )
        incoming[batch.file_path] = batch
    overlap = sorted(deleted & set(incoming))
    if overlap:
        raise ValueError(f"FactBatch paths cannot be upserted and deleted: {overlap}")

    final_batches = {} if replace_all else dict(previous_batches)
    for path in deleted:
        final_batches.pop(path, None)
    final_batches.update(incoming)

    units: list[FactBatchGenerationUnit] = []
    reused = 0
    published = 0
    for path, batch in sorted(final_batches.items()):
        key = FactBatchReuseKey.from_batch(batch)
        prior = previous_units.get(path)
        if (
            prior is not None
            and prior.reuse_key == key
            and previous_batches[path] != batch
        ):
            raise FactBatchGenerationError(
                "the same FactBatch reuse identity produced different facts"
            )
        if (
            prior is not None
            and prior.reuse_key == key
            and previous_batches[path] == batch
        ):
            receipt = prior.receipt
            reused += 1
        else:
            receipt = publish_fact_batch(store, batch)
            published += 1
        # Reused units were fully verified while loading the pinned previous
        # generation. New units were fully verified by publish_fact_batch.
        # Reading either object a second time here adds no publication
        # boundary and doubles large-generation I/O.
        catalog.register_object(
            receipt.object_digest,
            storage_key=receipt.storage_key,
            byte_size=receipt.byte_size,
            media_type=FACT_BATCH_ARTIFACT_MEDIA_TYPE,
        )
        units.append(FactBatchGenerationUnit(key, receipt))

    manifest = FactBatchGenerationManifest.create(
        repository_id=repository_id,
        source_revision_id=source_revision_id,
        profile_digest=profile.digest,
        provider=CLANGD_FACT_PROVIDER,
        units=units,
        batches=tuple(final_batches.values()),
    )
    manifest_payload = encode_fact_batch_generation(manifest)
    manifest_object = store.put_bytes(manifest_payload)
    verified_manifest = store.verify(manifest_object.digest)
    if verified_manifest != manifest_object:
        raise FactBatchGenerationError(
            "FactBatch generation object-store receipt changed"
        )
    observed_manifest_digest = hashlib.sha256(manifest_payload).hexdigest()
    if (
        manifest_object.digest != observed_manifest_digest
        or manifest_object.byte_size != len(manifest_payload)
    ):
        raise FactBatchGenerationError(
            "FactBatch generation object-store receipt is inconsistent"
        )
    catalog.register_object(
        manifest_object.digest,
        storage_key=manifest_object.storage_key,
        byte_size=manifest_object.byte_size,
        media_type=FACT_BATCH_GENERATION_MEDIA_TYPE,
    )
    profile_id = catalog.create_view_profile(
        FACT_BATCH_VIEW_TYPE,
        profile.catalog_config(),
        name=profile_name,
    )
    view_generation_id = catalog.stage_view_generation(
        repository_id,
        source_revision_id,
        profile_id,
        FACT_BATCH_VIEW_TYPE,
        manifest_object.digest,
        schema_version=FACT_BATCH_GENERATION_SCHEMA,
        metadata={
            "manifest_schema": FACT_BATCH_GENERATION_SCHEMA,
            "fact_profile_digest": profile.digest,
            "provider": CLANGD_FACT_PROVIDER,
            "unit_count": len(manifest.units),
            "definition_count": len(manifest.definitions),
            "batch_set_digest": manifest.batch_set_digest,
        },
        member_object_digests=[unit.receipt.object_digest for unit in manifest.units],
    )
    desired_views = [*additional_view_generation_ids, view_generation_id]
    if len(desired_views) != len(set(desired_views)):
        raise ValueError("duplicate desired view generation IDs")
    publication = catalog.publish_snapshot(
        repository_id,
        source_revision_id,
        desired_views,
        ref_name=ref_name,
        expected_generation=expected_generation,
    )
    return FactBatchPublicationResult(
        snapshot_id=publication["snapshot_id"],
        ref_generation=publication["generation"],
        changed=publication["changed"],
        view_generation_id=view_generation_id,
        manifest_object_digest=manifest_object.digest,
        manifest=manifest,
        reused_units=reused,
        published_units=published,
    )


__all__ = [
    "DEFAULT_FACT_BATCH_REF",
    "DEFAULT_MAX_FACT_BATCH_GENERATION_BYTES",
    "DEFAULT_MAX_FACT_BATCH_UNITS",
    "DEFAULT_MAX_FACT_DEFINITIONS",
    "FACT_BATCH_GENERATION_MEDIA_TYPE",
    "FACT_BATCH_GENERATION_SCHEMA",
    "FACT_BATCH_VIEW_TYPE",
    "FactBatchGenerationError",
    "FactBatchGenerationManifest",
    "FactBatchGenerationUnit",
    "FactBatchPublicationResult",
    "FactDefinitionPointer",
    "LoadedFactBatchGeneration",
    "decode_fact_batch_generation",
    "encode_fact_batch_generation",
    "load_fact_batch_generation",
    "publish_fact_batch_generation",
]
