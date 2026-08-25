# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for backend-neutral fenced job publication outputs."""

from __future__ import annotations

import dataclasses
import json

import pytest

from codenib.storage.models import (
    MAX_VIEW_GENERATION_MEMBERS,
    VIEW_GENERATION_MEMBERS_METADATA_KEY,
    IndexJobViewOutput,
    ObjectRecord,
    StorageValidationError,
)


def _object(value: int, *, byte_size: int | None = None) -> ObjectRecord:
    digest = f"{value:064x}"
    return ObjectRecord(
        digest=digest,
        byte_size=value if byte_size is None else byte_size,
        storage_key=f"sha256/{digest[:2]}/{digest[2:]}",
        media_type="application/x-test-object",
    )


def test_job_view_output_binds_profile_metadata_and_all_member_records() -> None:
    primary = _object(9)
    member_one = _object(1)
    member_two = _object(2)

    first = IndexJobViewOutput.create(
        "semantic_facts",
        "profile_" + "a" * 64,
        primary,
        schema_version="facts.v1",
        metadata={"language": "python", "counts": {"documents": 2}},
        member_object_records=(member_two, member_one),
    )
    reordered = IndexJobViewOutput.create(
        "semantic_facts",
        "profile_" + "a" * 64,
        primary,
        schema_version="facts.v1",
        metadata={"counts": {"documents": 2}, "language": "python"},
        member_object_records=(member_one, member_two),
    )

    assert first == reordered
    assert first.view_type == "semantic_facts"
    assert first.profile_id == "profile_" + "a" * 64
    assert first.object_record == primary
    assert first.object_record is not primary
    assert first.member_object_records == (member_one, member_two)
    assert json.loads(first.metadata_json) == {
        "counts": {"documents": 2},
        "language": "python",
    }
    assert first.generation_metadata == {
        VIEW_GENERATION_MEMBERS_METADATA_KEY: [member_one.digest, member_two.digest],
        "counts": {"documents": 2},
        "language": "python",
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.profile_id = "profile_" + "b" * 64  # type: ignore[misc]


@pytest.mark.parametrize(
    ("metadata", "match"),
    [
        ({"api_key": "must-not-persist"}, "secret field"),
        ({"nested": {"authorization": "Bearer secret"}}, "secret field"),
        (
            {VIEW_GENERATION_MEMBERS_METADATA_KEY: []},
            "reserved catalog metadata",
        ),
    ],
)
def test_job_view_output_metadata_is_canonical_and_secret_free(
    metadata: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(StorageValidationError, match=match):
        IndexJobViewOutput.create(
            "bm25",
            "profile_" + "a" * 64,
            _object(9),
            schema_version="1",
            metadata=metadata,
        )


@pytest.mark.parametrize(
    ("members", "match"),
    [
        ((_object(1), _object(1)), "duplicate member"),
        ((_object(9),), "primary object"),
        (
            (
                _object(1),
                ObjectRecord(
                    digest=_object(1).digest,
                    byte_size=999,
                    storage_key=_object(1).storage_key,
                ),
            ),
            "duplicate member",
        ),
    ],
)
def test_job_view_output_rejects_ambiguous_member_closure(
    members: tuple[ObjectRecord, ...],
    match: str,
) -> None:
    with pytest.raises(StorageValidationError, match=match):
        IndexJobViewOutput.create(
            "semantic_facts",
            "profile_" + "a" * 64,
            _object(9),
            schema_version="1",
            member_object_records=members,
        )


def test_job_view_output_accepts_the_public_member_cap_and_rejects_one_more() -> None:
    members = tuple(_object(value) for value in range(MAX_VIEW_GENERATION_MEMBERS))
    output = IndexJobViewOutput.create(
        "semantic_facts",
        "profile_" + "a" * 64,
        _object(MAX_VIEW_GENERATION_MEMBERS + 1),
        schema_version="facts.v1",
        member_object_records=members,
    )

    assert len(output.member_object_records) == MAX_VIEW_GENERATION_MEMBERS
    assert output.member_object_records[0].digest == f"{0:064x}"
    assert output.member_object_records[-1].digest == (
        f"{MAX_VIEW_GENERATION_MEMBERS - 1:064x}"
    )

    with pytest.raises(StorageValidationError, match="too many member"):
        IndexJobViewOutput.create(
            "semantic_facts",
            "profile_" + "a" * 64,
            _object(MAX_VIEW_GENERATION_MEMBERS + 2),
            schema_version="facts.v1",
            member_object_records=members + (_object(MAX_VIEW_GENERATION_MEMBERS),),
        )


@pytest.mark.parametrize(
    "changed",
    ("view_type", "profile_id", "schema_version", "metadata_json"),
)
@pytest.mark.parametrize("nonexact_type", ("bytes", "str_subclass"))
def test_job_view_output_rejects_nonexact_text_before_normalization(
    changed: str,
    nonexact_type: str,
) -> None:
    class ForgedString(str):
        def strip(self, *_args: object, **_kwargs: object) -> str:
            return "attacker-controlled-normalization"

    values: dict[str, object] = {
        "view_type": "bm25",
        "profile_id": "profile_" + "a" * 64,
        "object_record": _object(9),
        "schema_version": "1",
        "metadata_json": "{}",
    }
    if nonexact_type == "bytes":
        values[changed] = str(values[changed]).encode()
    else:
        values[changed] = ForgedString(str(values[changed]))

    with pytest.raises(StorageValidationError, match="exact str"):
        IndexJobViewOutput(**values)  # type: ignore[arg-type]
