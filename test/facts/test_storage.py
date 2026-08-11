# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Receipt-based FactBatch artifact reuse over the existing object store."""

from __future__ import annotations

from dataclasses import replace

import pytest

from codenib.facts import (
    CAPABILITY_DEFINITIONS,
    COMPLETENESS_PARTIAL,
    DiagnosticFact,
    FactBatch,
    FactBatchArtifactError,
    FactBatchArtifactReceipt,
    FactBatchReuseCache,
    FactBatchReuseKey,
    SourceRange,
    SymbolFact,
    decode_fact_batch_artifact,
    encode_fact_batch_artifact,
    load_fact_batch,
    publish_fact_batch,
    sha256_digest,
)
from codenib.storage import LocalCAS
from codenib.types import NODE_TYPE_FUNCTION


def _batch(
    *,
    file_path: str = "src/main.py",
    content: str = "def run(): pass\n",
    profile: str = "scip-python-v1",
) -> FactBatch:
    return FactBatch(
        file_path=file_path,
        language="python",
        content_digest=sha256_digest(content),
        profile_digest=sha256_digest(profile),
        provider="scip-python",
        completeness=COMPLETENESS_PARTIAL,
        capabilities=(CAPABILITY_DEFINITIONS,),
        symbols=(
            SymbolFact(
                symbol_id="run",
                moniker="scip python project 1 main/run().",
                display_name="run",
                kind=NODE_TYPE_FUNCTION,
                definition_range=SourceRange(0, 0, 0, 9),
            ),
        ),
    )


def test_fact_batch_artifact_is_canonical_and_self_validating() -> None:
    batch = _batch()
    payload = encode_fact_batch_artifact(batch)

    assert decode_fact_batch_artifact(payload) == batch
    assert encode_fact_batch_artifact(decode_fact_batch_artifact(payload)) == payload
    with pytest.raises(FactBatchArtifactError, match="not canonical JSON"):
        decode_fact_batch_artifact(payload + b"\n")


def test_reuse_key_rejects_boolean_schema_version() -> None:
    values = FactBatchReuseKey.from_batch(_batch()).to_dict()
    values["schema_version"] = True

    with pytest.raises(FactBatchArtifactError, match="reuse schema"):
        FactBatchReuseKey.from_dict(values)


def test_fact_batch_publication_round_trips_verified_receipt(tmp_path) -> None:
    store = LocalCAS(tmp_path / "objects")
    batch = _batch()
    key = FactBatchReuseKey.from_batch(batch)

    receipt = publish_fact_batch(store, batch)

    assert receipt.reuse_key_digest == key.digest
    assert receipt.batch_digest == batch.digest()
    assert load_fact_batch(store, receipt, expected_key=key) == batch
    assert (
        FactBatchArtifactReceipt.from_dict(receipt.to_dict()).to_dict()
        == receipt.to_dict()
    )


def test_receipt_mapping_reuses_exact_file_content_and_profile(tmp_path) -> None:
    store = LocalCAS(tmp_path / "objects")
    receipts: dict = {}
    writer = FactBatchReuseCache(store, receipts)
    batch = _batch()
    receipt = writer.publish(batch)
    persisted = writer.receipt_snapshot()

    reader = FactBatchReuseCache(store, persisted)
    exact_key = FactBatchReuseKey.from_batch(batch)

    assert reader.lookup(exact_key) == batch
    assert reader.hits == 1
    assert reader.lookup(FactBatchReuseKey.from_batch(_batch(profile="v2"))) is None
    assert (
        reader.lookup(FactBatchReuseKey.from_batch(_batch(content="changed"))) is None
    )
    assert (
        reader.lookup(FactBatchReuseKey.from_batch(_batch(file_path="src/copy.py")))
        is None
    )
    assert reader.misses == 3
    assert writer.publications == 1
    assert receipts[exact_key.digest] == receipt


def test_same_reuse_key_rejects_nondeterministic_output(tmp_path) -> None:
    store = LocalCAS(tmp_path / "objects")
    cache = FactBatchReuseCache(store)
    batch = _batch()
    cache.publish(batch)
    changed = replace(
        batch,
        diagnostics=(
            DiagnosticFact(
                severity="warning",
                code="changed",
                message="same inputs produced a different output",
            ),
        ),
    )

    with pytest.raises(FactBatchArtifactError, match="different FactBatch"):
        cache.publish(changed)


def test_receipt_metadata_mismatch_fails_closed(tmp_path) -> None:
    store = LocalCAS(tmp_path / "objects")
    batch = _batch()
    key = FactBatchReuseKey.from_batch(batch)
    receipt = publish_fact_batch(store, batch)
    tampered = receipt.to_dict()
    tampered["byte_size"] += 1

    with pytest.raises(FactBatchArtifactError, match="differs from object store"):
        load_fact_batch(store, tampered, expected_key=key)
    with pytest.raises(FactBatchArtifactError, match="reuse key does not match"):
        load_fact_batch(
            store,
            receipt,
            expected_key=FactBatchReuseKey.from_batch(_batch(profile="other")),
        )


def test_artifact_read_limit_is_enforced_before_object_read(tmp_path) -> None:
    store = LocalCAS(tmp_path / "objects")
    batch = _batch()
    key = FactBatchReuseKey.from_batch(batch)
    receipt = publish_fact_batch(store, batch)

    with pytest.raises(FactBatchArtifactError, match="read limit"):
        load_fact_batch(
            store,
            receipt,
            expected_key=key,
            max_bytes=receipt.byte_size - 1,
        )
    with pytest.raises(ValueError, match="positive integer"):
        load_fact_batch(store, receipt, expected_key=key, max_bytes=True)
