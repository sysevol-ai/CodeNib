# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Receipt-retained publication of immutable outputs for one index job."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import islice
from typing import Any

from .cas import BlobInfo
from .models import (
    MAX_VIEW_GENERATION_MEMBERS,
    IndexJobRecord,
    IndexJobStatus,
    IndexJobViewOutput,
    ObjectRecord,
    StorageIntegrityError,
    StorageValidationError,
    canonical_json,
    content_id,
)
from .protocols import (
    RETAINED_IMPORT_RESPONSE_MAX_TEXT_CHARS,
    JobPublicationCatalog,
    ReceiptRetainingObjectStore,
    snapshot_retained_import_response,
)

_MAX_JOB_OUTPUTS = 64
_CATALOG_INT64_MAX = 9_223_372_036_854_775_807


def _exact_object_record(
    receipt: object,
    media_type: object,
    *,
    label: str,
) -> tuple[BlobInfo, ObjectRecord]:
    if type(receipt) is not BlobInfo:
        raise TypeError(f"{label} receipt must be an exact BlobInfo")
    values = (receipt.digest, receipt.byte_size, receipt.storage_key)
    if tuple(type(value) for value in values) != (str, int, str):
        raise TypeError(f"{label} receipt fields must use exact str/int types")
    if type(media_type) is not str:
        raise TypeError(f"{label} media type must be an exact str")
    record = ObjectRecord(
        digest=values[0],
        byte_size=values[1],
        storage_key=values[2],
        media_type=media_type,
    )
    if record.byte_size > _CATALOG_INT64_MAX:
        raise StorageValidationError(f"{label} byte size exceeds catalog int64 range")
    canonical_receipt = BlobInfo(
        digest=record.digest,
        byte_size=record.byte_size,
        storage_key=record.storage_key,
    )
    if canonical_receipt != receipt or record.media_type != media_type:
        raise StorageValidationError(f"{label} receipt is not canonical")
    return canonical_receipt, record


@dataclass(frozen=True, slots=True)
class IndexJobObjectArtifact:
    """One exact point-in-time object receipt plus catalog media type."""

    receipt: BlobInfo
    media_type: str = "application/octet-stream"
    _object_record: ObjectRecord = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        receipt, record = _exact_object_record(
            self.receipt,
            self.media_type,
            label="index job object",
        )
        object.__setattr__(self, "receipt", receipt)
        object.__setattr__(self, "media_type", record.media_type)
        object.__setattr__(self, "_object_record", record)


@dataclass(frozen=True, slots=True)
class IndexJobViewArtifact:
    """One requested view backed by a primary and optional member receipts."""

    view_type: str
    profile_id: str
    object_artifact: IndexJobObjectArtifact
    schema_version: str
    metadata_json: str = "{}"
    member_artifacts: tuple[IndexJobObjectArtifact, ...] = ()

    def __post_init__(self) -> None:
        if type(self.object_artifact) is not IndexJobObjectArtifact:
            raise TypeError("index job view object must be IndexJobObjectArtifact")
        if type(self.member_artifacts) is not tuple:
            raise TypeError("index job member artifacts must be an exact tuple")
        if len(self.member_artifacts) > MAX_VIEW_GENERATION_MEMBERS:
            raise StorageValidationError("view generation has too many member objects")
        if any(
            type(member) is not IndexJobObjectArtifact
            for member in self.member_artifacts
        ):
            raise TypeError(
                "index job member artifacts must be IndexJobObjectArtifact values"
            )
        primary_artifact = IndexJobObjectArtifact(
            self.object_artifact.receipt,
            self.object_artifact.media_type,
        )
        detached_members = tuple(
            IndexJobObjectArtifact(member.receipt, member.media_type)
            for member in self.member_artifacts
        )
        output = IndexJobViewOutput(
            view_type=self.view_type,
            profile_id=self.profile_id,
            object_record=primary_artifact._object_record,
            schema_version=self.schema_version,
            metadata_json=self.metadata_json,
            member_object_records=tuple(
                member._object_record for member in detached_members
            ),
        )
        members_by_digest = {
            member._object_record.digest: member for member in detached_members
        }
        ordered_members = tuple(
            members_by_digest[record.digest] for record in output.member_object_records
        )
        if len(ordered_members) != len(self.member_artifacts):
            raise StorageValidationError("duplicate index job member artifact")
        object.__setattr__(self, "view_type", output.view_type)
        object.__setattr__(self, "profile_id", output.profile_id)
        object.__setattr__(self, "object_artifact", primary_artifact)
        object.__setattr__(self, "schema_version", output.schema_version)
        object.__setattr__(self, "metadata_json", output.metadata_json)
        object.__setattr__(self, "member_artifacts", ordered_members)

    @classmethod
    def create(
        cls,
        view_type: str,
        profile_id: str,
        receipt: BlobInfo,
        *,
        schema_version: str,
        media_type: str = "application/octet-stream",
        metadata: Mapping[str, Any] | None = None,
        member_artifacts: Sequence[IndexJobObjectArtifact] = (),
    ) -> IndexJobViewArtifact:
        if isinstance(member_artifacts, (str, bytes, bytearray)):
            raise TypeError("index job member artifacts must be a sequence")
        try:
            members = tuple(
                islice(iter(member_artifacts), MAX_VIEW_GENERATION_MEMBERS + 1)
            )
        except TypeError as exc:
            raise TypeError("index job member artifacts must be a sequence") from exc
        if len(members) > MAX_VIEW_GENERATION_MEMBERS:
            raise StorageValidationError("view generation has too many member objects")
        return cls(
            view_type=view_type,
            profile_id=profile_id,
            object_artifact=IndexJobObjectArtifact(receipt, media_type),
            schema_version=schema_version,
            metadata_json=canonical_json({} if metadata is None else metadata),
            member_artifacts=members,
        )

    @property
    def metadata(self) -> dict[str, Any]:
        return json.loads(self.metadata_json)

    def _output(self) -> IndexJobViewOutput:
        return IndexJobViewOutput(
            view_type=self.view_type,
            profile_id=self.profile_id,
            object_record=self.object_artifact._object_record,
            schema_version=self.schema_version,
            metadata_json=self.metadata_json,
            member_object_records=tuple(
                member._object_record for member in self.member_artifacts
            ),
        )


def publish_job_artifacts(
    job_id: str,
    *,
    catalog: JobPublicationCatalog,
    object_store: ReceiptRetainingObjectStore,
    owner_id: str,
    fencing_token: int,
    outputs: Sequence[IndexJobViewArtifact],
) -> IndexJobRecord:
    """Retain exact receipts while the catalog atomically publishes them.

    No catalog read occurs before the object store has revalidated and retained
    the complete receipt set. The retention callback encloses the catalog's
    ``BEGIN IMMEDIATE`` through commit/rollback and validates the returned
    successful job before allowing the retention scope to end.
    """

    normalized_job_id = _bounded_exact_text(job_id, "job ID", 80)
    normalized_owner = _bounded_exact_text(owner_id, "lease owner ID", 256)
    token = _positive_catalog_integer(fencing_token, "fencing token")
    artifacts = _freeze_outputs(outputs)
    frozen_outputs = tuple(artifact._output() for artifact in artifacts)

    # Bound aggregate work before constructing the canonical closure. One view
    # may still use the complete public member limit.
    member_count = sum(len(output.member_object_records) for output in frozen_outputs)
    if member_count > MAX_VIEW_GENERATION_MEMBERS:
        raise StorageValidationError(
            "index job publication has too many aggregate member objects"
        )
    bounded_closure = snapshot_retained_import_response(
        {"outputs": [output.identity for output in frozen_outputs]},
        label="index job publication closure",
    )
    closure_preview = canonical_json(bounded_closure)
    if len(closure_preview) > RETAINED_IMPORT_RESPONSE_MAX_TEXT_CHARS:
        raise StorageValidationError("index job publication closure is too large")

    receipt_by_digest: dict[str, tuple[BlobInfo, str]] = {}
    for artifact in artifacts:
        for object_artifact in (artifact.object_artifact, *artifact.member_artifacts):
            receipt = object_artifact.receipt
            existing = receipt_by_digest.get(receipt.digest)
            current = (receipt, object_artifact.media_type)
            if existing is None:
                receipt_by_digest[receipt.digest] = current
            elif existing != current:
                raise StorageValidationError(
                    "one object digest has conflicting publication metadata"
                )
    retained_receipts = tuple(
        receipt_by_digest[digest][0] for digest in sorted(receipt_by_digest)
    )

    if not isinstance(object_store, ReceiptRetainingObjectStore):
        raise TypeError("index job publication requires ReceiptRetainingObjectStore")

    callback_called = False
    unset = object()
    attested_result: object = unset

    def publish_retained() -> IndexJobRecord:
        nonlocal attested_result, callback_called
        if callback_called:
            raise StorageIntegrityError(
                "object store invoked the publication callback more than once"
            )
        callback_called = True
        completed = catalog.publish_job_outputs(
            normalized_job_id,
            owner_id=normalized_owner,
            fencing_token=token,
            outputs=frozen_outputs,
        )
        _attest_completed_publication(
            completed,
            job_id=normalized_job_id,
            outputs=frozen_outputs,
        )
        attested_result = completed
        return completed

    completed = object_store.retain_receipts(retained_receipts, publish_retained)
    if not callback_called or attested_result is unset:
        raise StorageIntegrityError(
            "object store did not invoke the retained publication callback"
        )
    if completed is not attested_result:
        raise StorageIntegrityError(
            "object store replaced the retained publication callback result"
        )
    # This exact object was authenticated inside the retention callback.
    return attested_result  # type: ignore[return-value]


def _freeze_outputs(
    outputs: Sequence[IndexJobViewArtifact],
) -> tuple[IndexJobViewArtifact, ...]:
    if isinstance(outputs, (str, bytes, bytearray)) or not isinstance(
        outputs, Sequence
    ):
        raise TypeError("index job outputs must be a sequence")
    try:
        collected = tuple(islice(iter(outputs), _MAX_JOB_OUTPUTS + 1))
    except TypeError as exc:
        raise TypeError("index job outputs must be a sequence") from exc
    if not collected:
        raise StorageValidationError("index job publication requires an output")
    if len(collected) > _MAX_JOB_OUTPUTS:
        raise StorageValidationError(
            f"index job publication cannot exceed {_MAX_JOB_OUTPUTS} outputs"
        )
    if any(type(output) is not IndexJobViewArtifact for output in collected):
        raise TypeError("index job outputs must be IndexJobViewArtifact values")
    detached = tuple(
        IndexJobViewArtifact(
            view_type=output.view_type,
            profile_id=output.profile_id,
            object_artifact=output.object_artifact,
            schema_version=output.schema_version,
            metadata_json=output.metadata_json,
            member_artifacts=output.member_artifacts,
        )
        for output in collected
    )
    ordered = tuple(sorted(detached, key=lambda output: output.view_type))
    view_types = tuple(output.view_type for output in ordered)
    if len(view_types) != len(set(view_types)):
        raise StorageValidationError("index job publication has duplicate view type")
    return ordered


def _attest_completed_publication(
    completed: object,
    *,
    job_id: str,
    outputs: tuple[IndexJobViewOutput, ...],
) -> None:
    if type(completed) is not IndexJobRecord or completed.job_id != job_id:
        raise StorageIntegrityError("catalog returned the wrong index job")
    if (
        completed.status is not IndexJobStatus.SUCCEEDED
        or completed.result_snapshot_id is None
    ):
        raise StorageIntegrityError("catalog did not return a successful publication")
    request_views = completed.request.get("views")
    if not isinstance(request_views, dict):
        raise StorageIntegrityError("catalog returned an invalid job request closure")
    offered = {output.view_type: output for output in outputs}
    if any(view_type not in request_views for view_type in offered):
        raise StorageIntegrityError("catalog accepted an unrequested output view")
    if any(
        request.get("required") is True and view_type not in offered
        for view_type, request in request_views.items()
        if isinstance(request, dict)
    ):
        raise StorageIntegrityError("catalog omitted a required output view")

    snapshot_members: list[list[str]] = []
    for output in outputs:
        request = request_views.get(output.view_type)
        if (
            not isinstance(request, dict)
            or request.get("profile_id") != output.profile_id
        ):
            raise StorageIntegrityError("catalog accepted an output profile mismatch")
        generation_id = content_id(
            "view",
            {
                "repository_id": completed.repository_id,
                "source_revision_id": completed.source_revision_id,
                "profile_id": output.profile_id,
                "view_type": output.view_type,
                "object_digest": output.object_record.digest,
                "schema_version": output.schema_version,
                "metadata": output.generation_metadata,
            },
        )
        snapshot_members.append([output.view_type, generation_id])
    expected_snapshot_id = content_id(
        "snapshot",
        {
            "repository_id": completed.repository_id,
            "source_revision_id": completed.source_revision_id,
            "views": snapshot_members,
        },
    )
    if completed.result_snapshot_id != expected_snapshot_id:
        raise StorageIntegrityError("catalog returned a different publication snapshot")


def _bounded_exact_text(value: object, field: str, maximum: int) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise StorageValidationError(f"{field} must be non-empty canonical text")
    if "\x00" in value or len(value) > maximum:
        raise StorageValidationError(f"{field} is out of bounds")
    return value


def _positive_catalog_integer(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise StorageValidationError(f"{field} must be a positive integer")
    if value > _CATALOG_INT64_MAX:
        raise StorageValidationError(f"{field} exceeds catalog int64 range")
    return value


__all__ = [
    "IndexJobObjectArtifact",
    "IndexJobViewArtifact",
    "publish_job_artifacts",
]
