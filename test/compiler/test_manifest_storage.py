# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import inspect
import json
from array import array
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path

import pytest

import codenib.compiler as compiler_module
import codenib.compiler.manifest_storage as manifest_storage_module
from codenib.compiler.index_builders import BM25IndexBuilder, VectorIndexBuilder
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.compiler.manifest_storage import (
    BM25_PROFILE_AXES,
    DEFAULT_MAX_MANIFEST_BYTES,
    REPO_MANIFEST_BM25_GENERATION_CONTRACT,
    REPO_MANIFEST_IMPORT_PLAN_SCHEMA,
    REPO_MANIFEST_PROFILE_CONTRACT,
    REPO_MANIFEST_PROFILE_NAME,
    REPO_MANIFEST_VECTOR_GENERATION_CONTRACT,
    VECTOR_PROFILE_AXES,
    RepoManifestImportPlan,
    SourceIntent,
    ViewImportIntent,
    ViewSelection,
    plan_repo_manifest_import,
    plan_repo_manifest_import_bytes,
)
from codenib.index.embedding.model_policy import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_REVISION,
)
from codenib.provider_routes import resolve_inference_route
from codenib.storage.models import StorageValidationError, ViewProfile, canonical_json


def _file_record(seed: str, *, filename: str | None = None) -> dict[str, object]:
    record: dict[str, object] = {"size": 10, "sha256": seed * 64}
    if filename is not None:
        record["file"] = filename
    return record


def _bm25_config() -> dict[str, object]:
    config = BM25IndexBuilder(
        languages=["python", "typescript"],
        max_k=96,
        max_lines_per_chunk=240,
    ).artifact_identity()
    config["artifact_file_fingerprints"] = {
        "documents.json": _file_record("a"),
        "bm25_metadata.json": _file_record("b"),
    }
    config["chunk_count"] = 11
    config["source_file_count"] = 3
    config["file_count"] = 11
    return config


def _vector_config() -> dict[str, object]:
    config = VectorIndexBuilder(
        languages=["python", "typescript"],
        embedding_model="test/model",
        embedding_dimension=4,
        build_levels=["l0", "l2"],
        max_lines_per_chunk=240,
    ).artifact_identity()
    config["persistence_config_fingerprint"] = _file_record(
        "c", filename="config_test__model.json"
    )
    config["document_count"] = {"l0": 3, "l2": 8}
    config["last_commit"] = "c" * 40
    return config


def _set_vector_route(
    entry: IndexEntry,
    *,
    model: str = "test/model",
    options: dict[str, object] | None = None,
) -> None:
    route = resolve_inference_route(
        operation="embeddings",
        provider="huggingface",
        model=model,
        dimension=4,
        compatibility_options=options or {},
        environ={},
    )
    _set_entry_fields(
        entry,
        {
            "embedding_model": route.model,
            "embedding_provider": route.provider,
            "embedding_dimension": route.dimension,
            "dimension": route.dimension,
            "embedding_endpoint": route.endpoint,
            "embedding_kwargs": route.compatibility_options,
            "embedding_route": route.public_identity(),
            "embedding_fingerprint": route.compatibility_fingerprint,
        },
    )


def _entry(
    view_type: str,
    config: dict[str, object],
    *,
    fingerprint: str,
    commit: str,
    status: str = "fresh",
) -> IndexEntry:
    return IndexEntry(
        index_type=view_type,
        path=f"views/{view_type}",
        built_at="2026-08-10T00:00:00+00:00",
        built_at_epoch=1.0,
        status=status,
        config=copy.deepcopy(config),
        metadata={**copy.deepcopy(config), "build_duration_seconds": 0.25},
        commit=commit,
        source_fingerprint=fingerprint,
    )


def _manifest(
    *,
    fingerprint_version: int = 2,
    include_vector: bool = True,
    commit: str = "c" * 40,
) -> RepoManifest:
    prefix = "sha256-v2:" if fingerprint_version == 2 else "sha256:"
    fingerprint = prefix + "d" * 64
    indexes = {
        "bm25": _entry(
            "bm25",
            _bm25_config(),
            fingerprint=fingerprint,
            commit=commit,
        )
    }
    if include_vector:
        indexes["vector"] = _entry(
            "vector",
            _vector_config(),
            fingerprint=fingerprint,
            commit=commit,
        )
    return RepoManifest(
        repo_path="source",
        commit=commit,
        last_indexed_commit=commit,
        source_fingerprint=fingerprint,
        last_indexed_source_fingerprint=fingerprint,
        languages=["python", "typescript"],
        file_count=3,
        indexes=indexes,
        capabilities={
            "sparse_search": True,
            "dense_search": include_vector,
            "hybrid_search": include_vector,
            "symbol_navigation": False,
        },
        compiled_at="2026-08-10T00:00:00+00:00",
        compiled_at_epoch=1.0,
    )


def _add_unselected_view(
    manifest: RepoManifest, *, status: str = "stale"
) -> IndexEntry:
    entry = _entry(
        "symbol_graph",
        {},
        fingerprint=manifest.source_fingerprint,
        commit=manifest.commit,
        status=status,
    )
    manifest.indexes["symbol_graph"] = entry
    return entry


def _set_entry_field(entry: IndexEntry, field: str, value: object) -> None:
    entry.config[field] = copy.deepcopy(value)
    entry.metadata[field] = copy.deepcopy(value)


def _set_entry_fields(entry: IndexEntry, values: dict[str, object]) -> None:
    for field, value in values.items():
        _set_entry_field(entry, field, value)


def _remove_entry_field(entry: IndexEntry, field: str) -> None:
    entry.config.pop(field, None)
    entry.metadata.pop(field, None)


def _vector_incremental_provenance() -> dict[str, object]:
    return {
        "chunks_reembedded": 3,
        "chunks_from_cache": 2,
        "cache_hit_rate": 2 / 11,
        "new_commit": "c" * 40,
    }


def _profile_id(manifest: RepoManifest, view_type: str) -> str:
    plan = plan_repo_manifest_import(manifest, views=[view_type])
    return plan.view_map[view_type].profile_id


def test_plan_accepts_current_builder_contracts_and_is_canonical() -> None:
    manifest = _manifest()
    plan = plan_repo_manifest_import(manifest)

    assert plan.schema == REPO_MANIFEST_IMPORT_PLAN_SCHEMA
    assert plan.source.disposition == "content-bound"
    assert plan.source.eligible_for_verification is True
    assert plan.inert is False
    assert plan.selection.to_dict() == {
        "required_views": ["bm25"],
        "optional_views": ["vector"],
        "selected_views": ["bm25", "vector"],
        "skipped_views": {},
    }
    assert tuple(plan.view_map) == ("bm25", "vector")
    assert plan.view_map["bm25"].profile.config == {
        "contract": REPO_MANIFEST_PROFILE_CONTRACT,
        "manifest_version": "1.1",
        "builder_schema": manifest.indexes["bm25"].config["builder_schema"],
        "compatibility": {
            axis: manifest.indexes["bm25"].config[axis]
            for axis in BM25_PROFILE_AXES
            if axis != "builder_schema"
        },
    }
    assert plan.view_map["bm25"].generation_record["contract"] == (
        REPO_MANIFEST_BM25_GENERATION_CONTRACT
    )
    assert plan.view_map["vector"].generation_record["contract"] == (
        REPO_MANIFEST_VECTOR_GENERATION_CONTRACT
    )
    assert plan.view_map["vector"].profile.config["compatibility"][
        "embedding_load_policy"
    ] == {"revision": None, "trust_remote_code": False}

    reordered = json.dumps(
        manifest.to_dict(),
        indent=4,
        sort_keys=False,
    ).encode()
    reparsed = plan_repo_manifest_import_bytes(reordered)
    assert reparsed.canonical_manifest_bytes == plan.canonical_manifest_bytes
    assert reparsed.manifest_digest == plan.manifest_digest
    assert reparsed.canonical_bytes == plan.canonical_bytes
    assert reparsed.plan_digest == plan.plan_digest


def test_planner_symbols_are_lazy_compiler_exports() -> None:
    expected = {
        "RepoManifestImportPlan",
        "SourceIntent",
        "ViewImportIntent",
        "ViewSelection",
        "plan_repo_manifest_import",
        "plan_repo_manifest_import_bytes",
    }

    assert expected <= set(compiler_module.__all__)
    assert compiler_module.plan_repo_manifest_import is plan_repo_manifest_import
    assert list(inspect.signature(plan_repo_manifest_import).parameters) == [
        "manifest",
        "views",
        "required_views",
        "optional_views",
        "max_manifest_bytes",
    ]


def test_selected_view_paths_are_exact_artifact_relative_contracts() -> None:
    required = _manifest(include_vector=False)
    required.indexes["bm25"].path = "state/bm25"
    with pytest.raises(StorageValidationError, match="artifact_path_mismatch"):
        plan_repo_manifest_import(required, views=["bm25"])

    optional = _manifest()
    optional.indexes["vector"].path = "views/vector/"
    plan = plan_repo_manifest_import(
        optional,
        required_views=["bm25"],
        optional_views=["vector"],
    )
    assert plan.selection.selected_views == ("bm25",)
    assert plan.selection.skipped_views == {"vector": "not_current"}


def test_public_view_intent_rejects_a_rebased_artifact_path() -> None:
    plan = plan_repo_manifest_import(_manifest(include_vector=False))
    intent = plan.views[0]
    entry = intent.manifest_entry
    entry["path"] = "other/bm25"

    with pytest.raises(StorageValidationError, match="path must be exactly"):
        replace(intent, manifest_entry_json=canonical_json(entry))


def test_plan_is_detached_and_does_not_claim_catalog_or_source_authority() -> None:
    manifest = _manifest()
    plan = plan_repo_manifest_import(manifest)
    before = plan.canonical_bytes
    manifest.indexes["bm25"].config["max_k"] = 1
    manifest.source_fingerprint = "sha256-v2:" + "e" * 64

    assert plan.canonical_bytes == before
    assert plan.manifest.indexes["bm25"].config["max_k"] == 96
    assert plan.to_dict()["source"]["eligible_for_verification"] is True
    assert "source_verified" not in plan.canonical_bytes.decode("ascii")
    assert "repository_id" not in plan.canonical_bytes.decode("ascii")
    assert "source_revision_id" not in plan.canonical_bytes.decode("ascii")
    with pytest.raises(TypeError):
        plan.view_map["extra"] = plan.views[0]  # type: ignore[index]
    with pytest.raises(TypeError):
        plan.selection.skipped_views["extra"] = "not_current"  # type: ignore[index]


@pytest.mark.parametrize(
    "claim",
    [
        "repository_id",
        "namespace_id",
        "source_revision_id",
        "profile_id",
        "generation_id",
        "view_generation_id",
        "object_id",
        "object",
        "storage_key",
        "snapshot_id",
        "catalog_snapshot_id",
        "receipt",
        "bundle_receipt",
        "pin_id",
        "source_verified",
        "commit_verified",
        "checkout_verified",
        "verification_scope",
        "native_index_authorization",
        "source_authority",
        "directory_ownership",
        "publication_receipt",
        "verified_source",
        "durable",
    ],
)
def test_whole_manifest_rejects_authority_claims_in_unselected_views(
    claim: str,
) -> None:
    manifest = _manifest()
    entry = _add_unselected_view(manifest)
    entry.metadata["nested"] = {claim: "forged"}

    with pytest.raises(
        StorageValidationError,
        match="authority claim|credential field|secret field",
    ):
        plan_repo_manifest_import(manifest)


@pytest.mark.parametrize("location", ["unselected", "stale-selected"])
@pytest.mark.parametrize(
    "claim",
    [
        "catalog_id",
        "receipt_id",
        "source_verified_at",
        "is_source_verified",
        "authority_id",
        "ownership_id",
        "directory_fd",
        "resolved_path",
        "diagnostics",
        "stack_trace",
        "failure_reason",
        "exception_type",
    ],
)
def test_whole_manifest_rejects_qualified_claim_names(
    location: str,
    claim: str,
) -> None:
    manifest = _manifest()
    if location == "unselected":
        entry = _add_unselected_view(manifest)
    else:
        entry = manifest.indexes["vector"]
        entry.status = "stale"
    entry.metadata["nested"] = {claim: "forged"}

    with pytest.raises(StorageValidationError, match="authority claim|diagnostic"):
        plan_repo_manifest_import(manifest)


@pytest.mark.parametrize(
    ("location", "field", "value", "message"),
    [
        ("unsupported", "api_key", "secret", "secret field"),
        ("stale", "error_message", "host traceback", "diagnostic field"),
        ("unsupported", "note", "Bearer forged", "authorization credentials"),
        ("stale", "note", "Basic forged", "authorization credentials"),
        (
            "repo_path",
            "path",
            "https://user:password@example.test/repo",
            "URL credentials",
        ),
        ("entry_path", "path", "Basic forged", "authorization credentials"),
    ],
)
def test_whole_manifest_publication_policy_covers_unselected_and_structural_data(
    location: str,
    field: str,
    value: str,
    message: str,
) -> None:
    manifest = _manifest()
    if location == "unsupported":
        _add_unselected_view(manifest).metadata[field] = value
    elif location == "stale":
        manifest.indexes["vector"].status = "stale"
        manifest.indexes["vector"].metadata[field] = value
    elif location == "repo_path":
        manifest.repo_path = value
    else:
        manifest.indexes["bm25"].path = value

    with pytest.raises(StorageValidationError, match=message):
        plan_repo_manifest_import(manifest)


def test_normal_repo_path_and_display_commit_are_not_authority_claim_keys() -> None:
    manifest = _manifest(commit="display-provenance")
    manifest.repo_path = "/workspace/repo"

    plan = plan_repo_manifest_import(manifest)

    assert plan.source.display_commit == "display-provenance"
    assert plan.manifest.repo_path == "/workspace/repo"


def test_v1_source_is_diagnostic_and_commit_is_only_display_provenance() -> None:
    first_manifest = _manifest(fingerprint_version=1, commit="a" * 40)
    second_manifest = _manifest(fingerprint_version=1, commit="b" * 40)
    first = plan_repo_manifest_import(first_manifest)
    second = plan_repo_manifest_import(second_manifest)

    assert first.source.disposition == "legacy-diagnostic"
    assert first.source.eligible_for_verification is False
    assert first.inert is True
    assert first.source.display_commit == "a" * 40
    assert first.source.identity_digest == second.source.identity_digest
    assert first.manifest_digest != second.manifest_digest
    assert first.plan_digest != second.plan_digest


@pytest.mark.parametrize("axis", BM25_PROFILE_AXES)
def test_required_bm25_profile_fails_closed_when_axis_is_missing(axis: str) -> None:
    manifest = _manifest(include_vector=False)
    manifest.indexes["bm25"].config.pop(axis)
    manifest.indexes["bm25"].metadata.pop(axis)

    with pytest.raises(StorageValidationError, match="incomplete profile.*missing"):
        plan_repo_manifest_import(manifest)


@pytest.mark.parametrize("axis", VECTOR_PROFILE_AXES)
def test_required_vector_profile_fails_closed_when_axis_is_missing(axis: str) -> None:
    manifest = _manifest()
    manifest.indexes["vector"].config.pop(axis)
    manifest.indexes["vector"].metadata.pop(axis)

    with pytest.raises(StorageValidationError, match="incomplete profile.*missing"):
        plan_repo_manifest_import(manifest, views=["vector"])


@pytest.mark.parametrize("view_type", ["bm25", "vector"])
def test_unknown_builder_schemas_fail_closed(view_type: str) -> None:
    manifest = _manifest()
    _set_entry_field(manifest.indexes[view_type], "builder_schema", 999)

    with pytest.raises(StorageValidationError, match="current portable schema"):
        plan_repo_manifest_import(manifest, views=[view_type])

    optional = plan_repo_manifest_import(
        manifest,
        required_views=["bm25" if view_type == "vector" else "vector"],
        optional_views=[view_type],
    )
    assert optional.selection.skipped_views[view_type] == "incomplete_profile"


@pytest.mark.parametrize("view_type", ["bm25", "vector"])
def test_every_profile_axis_participates_in_profile_identity(view_type: str) -> None:
    plan = plan_repo_manifest_import(_manifest(), views=[view_type])
    intent = plan.view_map[view_type]
    axes = BM25_PROFILE_AXES if view_type == "bm25" else VECTOR_PROFILE_AXES
    baseline = intent.profile.config

    for axis in axes:
        changed = copy.deepcopy(baseline)
        if axis == "builder_schema":
            changed[axis] += 1
        else:
            current = changed["compatibility"][axis]
            changed["compatibility"][axis] = _different_json_value(current)
        altered = ViewProfile.create(
            view_type,
            changed,
            name=intent.profile.name,
        )
        assert altered.profile_id != intent.profile_id, axis


@pytest.mark.parametrize("view_type", ["bm25", "vector"])
def test_optional_ignore_directories_participate_in_profile_identity(
    view_type: str,
) -> None:
    manifest = _manifest()
    baseline = _profile_id(manifest, view_type)
    _set_entry_field(
        manifest.indexes[view_type],
        "additional_ignore_dirs",
        ["vendored-source"],
    )

    plan = plan_repo_manifest_import(manifest, views=[view_type])
    compatibility = plan.view_map[view_type].profile.config["compatibility"]

    assert compatibility["additional_ignore_dirs"] == ["vendored-source"]
    assert plan.view_map[view_type].profile_id != baseline


def test_remote_vector_profile_binds_document_input_limit() -> None:
    manifest = _manifest()
    entry = manifest.indexes["vector"]
    remote_identity = VectorIndexBuilder(
        languages=["python", "typescript"],
        embedding_model="test/model",
        embedding_provider="openai",
        embedding_dimension=4,
        embedding_endpoint="https://embedding.example/v1",
        embedding_credential_env="TEST_EMBEDDING_API_KEY",
        build_levels=["l0", "l2"],
        max_lines_per_chunk=240,
        embedding_document_max_chars=12_000,
    ).artifact_identity()
    _set_entry_fields(entry, remote_identity)

    first = plan_repo_manifest_import(manifest, views=["vector"])
    assert (
        first.view_map["vector"].profile.config["compatibility"][
            "embedding_document_max_chars"
        ]
        == 12_000
    )

    _set_entry_field(entry, "embedding_document_max_chars", 8_000)
    second = plan_repo_manifest_import(manifest, views=["vector"])
    assert second.view_map["vector"].profile_id != first.view_map["vector"].profile_id


def test_remote_vector_profile_requires_document_input_limit() -> None:
    manifest = _manifest()
    entry = manifest.indexes["vector"]
    remote_identity = VectorIndexBuilder(
        languages=["python", "typescript"],
        embedding_model="test/model",
        embedding_provider="openai",
        embedding_dimension=4,
        embedding_endpoint="https://embedding.example/v1",
        build_levels=["l0", "l2"],
        max_lines_per_chunk=240,
    ).artifact_identity()
    remote_identity.pop("embedding_document_max_chars")
    _set_entry_fields(entry, remote_identity)

    with pytest.raises(StorageValidationError, match="document bound is missing"):
        plan_repo_manifest_import(manifest, views=["vector"])


def test_vector_profile_binds_resolved_artifact_model_policy() -> None:
    manifest = _manifest()
    baseline = _profile_id(manifest, "vector")
    _set_vector_route(
        manifest.indexes["vector"],
        options={
            "revision": "a" * 40,
            "model_kwargs": {"trust_remote_code": True},
        },
    )

    plan = plan_repo_manifest_import(manifest, views=["vector"])
    compatibility = plan.view_map["vector"].profile.config["compatibility"]

    assert compatibility["embedding_load_policy"] == {
        "revision": "a" * 40,
        "trust_remote_code": True,
    }
    assert plan.view_map["vector"].profile_id != baseline


def test_vector_profile_materializes_current_default_model_policy() -> None:
    manifest = _manifest()
    entry = manifest.indexes["vector"]
    _set_vector_route(entry, model=DEFAULT_EMBEDDING_MODEL)
    config_name = f"config_{DEFAULT_EMBEDDING_MODEL.replace('/', '__')}.json"
    _set_entry_field(
        entry,
        "persistence_config_fingerprint",
        _file_record("c", filename=config_name),
    )

    plan = plan_repo_manifest_import(manifest, views=["vector"])

    assert plan.view_map["vector"].profile.config["compatibility"][
        "embedding_load_policy"
    ] == {
        "revision": DEFAULT_EMBEDDING_REVISION,
        "trust_remote_code": True,
    }


def test_vector_profile_rejects_filesystem_shaped_artifact_models() -> None:
    manifest = _manifest()
    _set_vector_route(manifest.indexes["vector"], model="/tmp/local-model")

    with pytest.raises(StorageValidationError, match="model policy"):
        plan_repo_manifest_import(manifest, views=["vector"])


def test_vector_profile_rejects_conflicting_model_policy_options() -> None:
    manifest = _manifest()
    _set_vector_route(
        manifest.indexes["vector"],
        options={
            "revision": "a" * 40,
            "model_kwargs": {"revision": "b" * 40},
        },
    )

    with pytest.raises(StorageValidationError, match="model policy"):
        plan_repo_manifest_import(manifest, views=["vector"])


def test_planning_uses_no_filesystem_or_ambient_route_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest()
    real_resolve = manifest_storage_module.resolve_embedding_artifact_route
    calls: list[object] = []

    def resolve_without_ambient_environment(config, **kwargs):
        calls.append(kwargs.get("environ"))
        assert kwargs.get("environ") == {}
        return real_resolve(config, **kwargs)

    def reject_filesystem_probe(*_args, **_kwargs):
        pytest.fail("manifest planning probed the filesystem")

    monkeypatch.setattr(
        manifest_storage_module,
        "resolve_embedding_artifact_route",
        resolve_without_ambient_environment,
    )
    monkeypatch.setattr(Path, "is_dir", reject_filesystem_probe)

    plan = plan_repo_manifest_import(manifest, views=["vector"])

    assert plan.selection.selected_views == ("vector",)
    assert calls and all(call == {} for call in calls)


def _different_json_value(value: object) -> object:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        return value + "-different"
    if value is None:
        return "explicit"
    if isinstance(value, list):
        return [*value, "different"]
    if isinstance(value, dict):
        return {**value, "explicit_difference": True}
    raise AssertionError(f"unhandled test value: {value!r}")


def test_generation_fingerprints_do_not_pollute_profile_identity() -> None:
    manifest = _manifest()
    original = plan_repo_manifest_import(manifest)
    changed = copy.deepcopy(manifest)
    record = changed.indexes["bm25"].config["artifact_file_fingerprints"]
    record["documents.json"]["sha256"] = "e" * 64
    changed.indexes["bm25"].metadata["artifact_file_fingerprints"] = copy.deepcopy(
        record
    )
    altered = plan_repo_manifest_import(changed)

    assert altered.view_map["bm25"].profile_id == (original.view_map["bm25"].profile_id)
    assert altered.view_map["bm25"].generation_record_digest != (
        original.view_map["bm25"].generation_record_digest
    )
    assert altered.plan_digest != original.plan_digest


def test_optional_incomplete_profile_is_explicitly_inert() -> None:
    manifest = _manifest()
    manifest.indexes["vector"].config.pop("embedding_route")
    manifest.indexes["vector"].metadata.pop("embedding_route")
    plan = plan_repo_manifest_import(manifest)

    assert plan.selection.selected_views == ("bm25",)
    assert plan.selection.optional_views == ("vector",)
    assert plan.selection.skipped_views == {"vector": "incomplete_profile"}
    assert tuple(plan.view_map) == ("bm25",)


@pytest.mark.parametrize(
    ("view_type", "record_field"),
    [
        ("bm25", "artifact_file_fingerprints"),
        ("vector", "persistence_config_fingerprint"),
    ],
)
def test_generation_record_is_required_but_not_a_profile_axis(
    view_type: str, record_field: str
) -> None:
    manifest = _manifest()
    manifest.indexes[view_type].config.pop(record_field)
    manifest.indexes[view_type].metadata.pop(record_field)

    with pytest.raises(StorageValidationError, match="incomplete generation record"):
        plan_repo_manifest_import(manifest, views=[view_type])


def test_optional_incomplete_generation_record_is_skipped() -> None:
    manifest = _manifest()
    manifest.indexes["vector"].config.pop("persistence_config_fingerprint")
    manifest.indexes["vector"].metadata.pop("persistence_config_fingerprint")
    plan = plan_repo_manifest_import(manifest)

    assert plan.selection.selected_views == ("bm25",)
    assert plan.selection.skipped_views == {"vector": "incomplete_generation_record"}


def test_optional_malformed_generation_record_is_skipped() -> None:
    manifest = _manifest()
    manifest.indexes["vector"].config["persistence_config_fingerprint"] = {
        "not": "a-record"
    }
    manifest.indexes["vector"].metadata["persistence_config_fingerprint"] = {
        "not": "a-record"
    }

    plan = plan_repo_manifest_import(manifest)

    assert plan.selection.skipped_views == {"vector": "incomplete_generation_record"}


def test_unknown_config_fields_fail_closed_or_skip_when_optional() -> None:
    required = _manifest(include_vector=False)
    required.indexes["bm25"].config["new_semantic_axis"] = "unclassified"
    with pytest.raises(StorageValidationError, match="unclassified config fields"):
        plan_repo_manifest_import(required)

    optional = _manifest()
    optional.indexes["vector"].config["new_semantic_axis"] = "unclassified"
    plan = plan_repo_manifest_import(optional)
    assert plan.selection.skipped_views == {"vector": "incomplete_profile"}


def test_unknown_metadata_fields_cannot_bypass_profile_classification() -> None:
    required = _manifest(include_vector=False)
    required.indexes["bm25"].metadata["new_semantic_axis"] = "unclassified"
    with pytest.raises(StorageValidationError, match="unclassified metadata fields"):
        plan_repo_manifest_import(required)

    optional = _manifest()
    optional.indexes["vector"].metadata["new_semantic_axis"] = "unclassified"
    plan = plan_repo_manifest_import(optional)
    assert plan.selection.skipped_views == {"vector": "incomplete_profile"}


def test_profile_axis_cannot_conflict_with_duplicated_metadata() -> None:
    manifest = _manifest(include_vector=False)
    manifest.indexes["bm25"].metadata["max_k"] = 1

    with pytest.raises(StorageValidationError, match="conflicts with metadata"):
        plan_repo_manifest_import(manifest)


def test_selection_is_canonical_and_nonportable_views_are_not_inferred() -> None:
    manifest = _manifest()
    manifest.indexes["symbol_graph"] = _entry(
        "symbol_graph",
        {},
        fingerprint=manifest.source_fingerprint,
        commit=manifest.commit,
    )
    plan = plan_repo_manifest_import(
        manifest,
        required_views=["vector", "vector"],
        optional_views=["bm25", "bm25"],
    )

    assert plan.selection.required_views == ("vector",)
    assert plan.selection.optional_views == ("bm25",)
    assert plan.selection.selected_views == ("bm25", "vector")
    assert set(plan.view_map) == {"bm25", "vector"}


def test_noncurrent_optional_view_is_recorded_without_profile_guessing() -> None:
    manifest = _manifest()
    manifest.indexes["vector"].status = "stale"
    plan = plan_repo_manifest_import(
        manifest,
        required_views=["bm25"],
        optional_views=["vector"],
    )

    assert plan.selection.selected_views == ("bm25",)
    assert plan.selection.skipped_views == {"vector": "not_current"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"views": ["bm25"], "required_views": ["bm25"]},
        {"required_views": ["bm25"], "optional_views": ["bm25"]},
        {"views": ["symbol_graph"]},
        {"views": "bm25"},
        {"views": [""]},
        {"views": ["bm25"] * 129},
    ],
)
def test_invalid_selection_is_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(StorageValidationError):
        plan_repo_manifest_import(_manifest(), **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("drift", ["status", "commit", "fingerprint", "type"])
def test_required_view_needs_an_exact_manifest_source_binding(drift: str) -> None:
    manifest = _manifest(include_vector=False)
    entry = manifest.indexes["bm25"]
    if drift == "status":
        entry.status = "stale"
    elif drift == "commit":
        entry.commit = "e" * 40
    elif drift == "fingerprint":
        entry.source_fingerprint = "sha256-v2:" + "e" * 64
    else:
        entry.index_type = "vector"

    with pytest.raises(StorageValidationError, match="not current|does not match"):
        plan_repo_manifest_import(manifest, views=["bm25"])


@pytest.mark.parametrize(
    "corruption",
    ["duplicate", "nan", "infinity", "top-level-array"],
)
def test_strict_json_rejects_ambiguous_or_nonobject_documents(corruption: str) -> None:
    manifest = _manifest(include_vector=False)
    payload = json.dumps(manifest.to_dict())
    if corruption == "duplicate":
        payload = payload.replace(
            '"version": "1.1"', '"version": "1.1", "version": "1.1"'
        )
    elif corruption == "nan":
        payload = payload.replace(
            '"compiled_at_epoch": 1.0', '"compiled_at_epoch": NaN'
        )
    elif corruption == "infinity":
        payload = payload.replace(
            '"compiled_at_epoch": 1.0', '"compiled_at_epoch": Infinity'
        )
    else:
        payload = "[]"

    with pytest.raises(StorageValidationError, match="invalid or ambiguous|object"):
        plan_repo_manifest_import_bytes(payload)


@pytest.mark.parametrize(
    "encoding",
    ["utf-8-sig", "utf-16", "utf-16-le", "utf-32", "utf-32-le"],
)
def test_bytes_api_accepts_only_bomless_strict_utf8(encoding: str) -> None:
    text = json.dumps(_manifest(include_vector=False).to_dict())

    with pytest.raises(StorageValidationError):
        plan_repo_manifest_import_bytes(text.encode(encoding))


def test_bytes_api_rejects_invalid_utf8_and_string_bom() -> None:
    with pytest.raises(StorageValidationError, match="UTF-8"):
        plan_repo_manifest_import_bytes(b'{"version":"\xff"}')
    with pytest.raises(StorageValidationError, match="BOM"):
        plan_repo_manifest_import_bytes("\ufeff{}")


def test_manifest_json_complexity_fails_before_semantic_planning() -> None:
    manifest = _manifest(include_vector=False)
    nested: object = "leaf"
    for _ in range(70):
        nested = {"nested": nested}
    manifest.indexes["bm25"].metadata["note"] = nested

    with pytest.raises(StorageValidationError, match="complexity budget"):
        plan_repo_manifest_import_bytes(json.dumps(manifest.to_dict()))
    with pytest.raises(StorageValidationError, match="level depth limit"):
        plan_repo_manifest_import(manifest)


def test_manifest_rejects_lone_unicode_surrogates() -> None:
    manifest = _manifest(include_vector=False)
    manifest.compiled_at = "bad-\ud800"

    with pytest.raises(StorageValidationError, match="UTF-8"):
        plan_repo_manifest_import(manifest)
    payload = json.dumps(manifest.to_dict()).encode("ascii")
    with pytest.raises(StorageValidationError, match="UTF-8"):
        plan_repo_manifest_import_bytes(payload)


def test_bytes_api_parses_unicode_text_then_canonicalizes_to_ascii() -> None:
    manifest = _manifest(include_vector=False)
    manifest.compiled_at = "build-é"
    payload = json.dumps(manifest.to_dict(), ensure_ascii=False)

    plan = plan_repo_manifest_import_bytes(payload)

    assert b"build-\\u00e9" in plan.canonical_manifest_bytes


@pytest.mark.parametrize(
    "number",
    ["9" * 5000, "9" * 400 + ".0", "1e10000"],
)
def test_bytes_api_translates_oversized_json_numbers(number: str) -> None:
    payload = json.dumps(_manifest(include_vector=False).to_dict())
    payload = payload.replace(
        '"compiled_at_epoch": 1.0', f'"compiled_at_epoch": {number}'
    )

    with pytest.raises(StorageValidationError, match="invalid or ambiguous"):
        plan_repo_manifest_import_bytes(payload)


@pytest.mark.parametrize("epoch", [10**400, 1e308, float(2**63)])
def test_mapping_api_bounds_epoch_before_float_conversion(epoch: object) -> None:
    manifest = _manifest(include_vector=False)
    manifest.compiled_at_epoch = epoch  # type: ignore[assignment]

    with pytest.raises(StorageValidationError):
        plan_repo_manifest_import(manifest)


def test_manifest_input_and_canonical_output_are_bounded() -> None:
    payload = json.dumps(_manifest(include_vector=False).to_dict()).encode()
    with pytest.raises(StorageValidationError, match="positive integer"):
        plan_repo_manifest_import_bytes(payload, max_manifest_bytes=0)
    with pytest.raises(StorageValidationError, match="byte limit"):
        plan_repo_manifest_import_bytes(payload, max_manifest_bytes=len(payload) - 1)
    with pytest.raises(StorageValidationError, match="byte limit"):
        plan_repo_manifest_import(
            _manifest(include_vector=False),
            max_manifest_bytes=64,
        )
    with pytest.raises(StorageValidationError, match="fixed.*ceiling"):
        plan_repo_manifest_import_bytes(
            payload,
            max_manifest_bytes=DEFAULT_MAX_MANIFEST_BYTES + 1,
        )


def test_manifest_byte_limit_rejects_hostile_integer_subclasses() -> None:
    class ForgedLimit(int):
        def __le__(self, other: object) -> bool:
            return False

        def __gt__(self, other: object) -> bool:
            return False

    with pytest.raises(StorageValidationError, match="positive integer"):
        plan_repo_manifest_import(
            _manifest(include_vector=False),
            max_manifest_bytes=ForgedLimit(1),
        )


def test_manifest_size_gate_precedes_text_or_buffer_copy() -> None:
    class TrapText(str):
        def encode(self, *args, **kwargs):
            raise AssertionError("text was encoded before its size gate")

    class TrapBytearray(bytearray):
        def __bytes__(self):
            raise AssertionError("buffer was copied before its size gate")

    for payload in (TrapText("{}"), TrapBytearray(b"{}")):
        with pytest.raises(StorageValidationError, match="byte limit"):
            plan_repo_manifest_import_bytes(payload, max_manifest_bytes=1)


def test_manifest_memoryview_size_gate_uses_byte_size() -> None:
    payload = memoryview(array("I", [0x7B7D7B7D]))
    assert len(payload) == 1
    assert payload.nbytes > 1

    with pytest.raises(StorageValidationError, match="byte limit"):
        plan_repo_manifest_import_bytes(payload, max_manifest_bytes=1)


def test_released_manifest_memoryview_is_a_validation_error() -> None:
    payload = memoryview(b"{}")
    payload.release()

    with pytest.raises(StorageValidationError, match="buffer is unavailable"):
        plan_repo_manifest_import_bytes(payload)


def test_exact_manifest_is_snapshotted_without_calling_to_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(include_vector=False)
    original_to_dict = RepoManifest.to_dict

    def fail_to_dict(self: RepoManifest) -> dict[str, object]:
        if self is manifest:
            raise AssertionError(
                "caller RepoManifest.to_dict must not run before the budget gate"
            )
        return original_to_dict(self)

    monkeypatch.setattr(RepoManifest, "to_dict", fail_to_dict)
    plan = plan_repo_manifest_import(manifest)
    assert plan.selection.selected_views == ("bm25",)


def test_exact_manifest_rejects_hostile_fields_before_consuming_them() -> None:
    reads = 0

    def languages() -> Iterator[str]:
        nonlocal reads
        while True:
            reads += 1
            yield "python"

    manifest = _manifest(include_vector=False)
    manifest.languages = languages()  # type: ignore[assignment]
    with pytest.raises(StorageValidationError):
        plan_repo_manifest_import(manifest, max_manifest_bytes=1)
    assert reads == 0


@pytest.mark.parametrize("kind", ["manifest", "entry"])
def test_exact_manifest_models_with_missing_slots_fail_closed(kind: str) -> None:
    if kind == "manifest":
        value = object.__new__(RepoManifest)
    else:
        value = _manifest(include_vector=False)
        value.indexes["bm25"] = object.__new__(IndexEntry)

    with pytest.raises(StorageValidationError, match="missing|invalid"):
        plan_repo_manifest_import(value)


def test_import_plan_revalidates_forged_child_intents() -> None:
    plan = plan_repo_manifest_import(_manifest(include_vector=False), views=["bm25"])
    forged = object.__new__(ViewImportIntent)
    for field in (
        "view_type",
        "required",
        "manifest_entry_json",
        "profile",
        "generation_record_json",
    ):
        object.__setattr__(forged, field, getattr(plan.views[0], field))
    object.__setattr__(
        forged,
        "generation_record_json",
        canonical_json({"contract": "forged"}),
    )

    with pytest.raises(StorageValidationError, match="generation record"):
        RepoManifestImportPlan(
            manifest_json=plan.manifest_json,
            source=plan.source,
            selection=plan.selection,
            views=(forged,),
        )


def test_import_plan_rejects_mutable_selection_aliases() -> None:
    plan = plan_repo_manifest_import(_manifest(include_vector=False), views=["bm25"])
    forged = object.__new__(ViewSelection)
    object.__setattr__(forged, "required_views", ["bm25"])
    object.__setattr__(forged, "optional_views", [])
    object.__setattr__(forged, "selected_views", ["bm25"])
    object.__setattr__(forged, "skipped_items", ())

    with pytest.raises(StorageValidationError, match="tuple"):
        RepoManifestImportPlan(
            manifest_json=plan.manifest_json,
            source=plan.source,
            selection=forged,
            views=plan.views,
        )


def test_import_plan_rejects_hostile_scalar_and_profile_subclasses() -> None:
    class ForgedText(str):
        def __eq__(self, other: object) -> bool:
            return True

    class ForgedProfile(ViewProfile):
        def __eq__(self, other: object) -> bool:
            return True

    plan = plan_repo_manifest_import(_manifest(include_vector=False), views=["bm25"])
    profile = plan.views[0].profile

    with pytest.raises(StorageValidationError, match="text fields must be exact"):
        replace(plan.source, fingerprint=ForgedText(plan.source.fingerprint))
    with pytest.raises(StorageValidationError, match="profile is invalid"):
        replace(
            plan.views[0],
            profile=ForgedProfile(
                profile.view_type,
                profile.name,
                profile.config_json,
            ),
        )


def test_public_intent_value_objects_reject_subclasses() -> None:
    plan = plan_repo_manifest_import(_manifest(include_vector=False), views=["bm25"])

    class ForgedSourceIntent(SourceIntent):
        pass

    class ForgedViewSelection(ViewSelection):
        pass

    class ForgedViewImportIntent(ViewImportIntent):
        pass

    with pytest.raises(StorageValidationError, match="exact intent type"):
        ForgedSourceIntent(
            plan.source.fingerprint,
            plan.source.fingerprint_version,
            plan.source.file_count,
            plan.source.display_commit,
        )
    with pytest.raises(StorageValidationError, match="exact selection type"):
        ForgedViewSelection(
            plan.selection.required_views,
            plan.selection.optional_views,
            plan.selection.selected_views,
            plan.selection.skipped_items,
        )
    view = plan.views[0]
    with pytest.raises(StorageValidationError, match="exact intent type"):
        ForgedViewImportIntent(
            view.view_type,
            view.required,
            view.manifest_entry_json,
            view.profile,
            view.generation_record_json,
        )

    with pytest.raises(StorageValidationError, match="bounded text"):
        SourceIntent("x" * 129, 2, 1, "display")


def test_forged_profile_text_is_bounded_before_plan_comparison() -> None:
    plan = plan_repo_manifest_import(_manifest(include_vector=False), views=["bm25"])
    forged_profile = object.__new__(ViewProfile)
    object.__setattr__(forged_profile, "view_type", "bm25")
    object.__setattr__(forged_profile, "name", REPO_MANIFEST_PROFILE_NAME)
    object.__setattr__(
        forged_profile,
        "config_json",
        " " * (DEFAULT_MAX_MANIFEST_BYTES + 1),
    )
    with pytest.raises(StorageValidationError, match="byte limit"):
        replace(plan.views[0], profile=forged_profile)


def test_import_plan_rejects_exact_children_forged_without_constructors() -> None:
    class ForgedText(str):
        def __eq__(self, other: object) -> bool:
            return True

    plan = plan_repo_manifest_import(_manifest(include_vector=False), views=["bm25"])
    forged_source = object.__new__(SourceIntent)
    for field in ("fingerprint", "fingerprint_version", "file_count", "display_commit"):
        object.__setattr__(forged_source, field, getattr(plan.source, field))
    object.__setattr__(
        forged_source,
        "fingerprint",
        ForgedText("sha256-v2:" + "0" * 64),
    )
    with pytest.raises(StorageValidationError, match="text fields must be exact"):
        RepoManifestImportPlan(
            manifest_json=plan.manifest_json,
            source=forged_source,
            selection=plan.selection,
            views=plan.views,
        )

    forged_profile = object.__new__(ViewProfile)
    profile = plan.views[0].profile
    for field in ("view_type", "name", "config_json"):
        object.__setattr__(forged_profile, field, getattr(profile, field))
    object.__setattr__(forged_profile, "view_type", ForgedText("vector"))
    forged_view = object.__new__(ViewImportIntent)
    for field in (
        "view_type",
        "required",
        "manifest_entry_json",
        "profile",
        "generation_record_json",
    ):
        object.__setattr__(forged_view, field, getattr(plan.views[0], field))
    object.__setattr__(forged_view, "profile", forged_profile)
    with pytest.raises(StorageValidationError, match="profile fields must be exact"):
        RepoManifestImportPlan(
            manifest_json=plan.manifest_json,
            source=plan.source,
            selection=plan.selection,
            views=(forged_view,),
        )


@pytest.mark.parametrize("kind", ["source", "selection", "profile"])
def test_import_plan_missing_child_slots_are_validation_errors(kind: str) -> None:
    plan = plan_repo_manifest_import(_manifest(include_vector=False), views=["bm25"])
    source = plan.source
    selection = plan.selection
    views = plan.views
    if kind == "source":
        source = object.__new__(SourceIntent)
    elif kind == "selection":
        selection = object.__new__(ViewSelection)
    else:
        forged_view = object.__new__(ViewImportIntent)
        for field in (
            "view_type",
            "required",
            "manifest_entry_json",
            "profile",
            "generation_record_json",
        ):
            object.__setattr__(forged_view, field, getattr(plan.views[0], field))
        object.__setattr__(forged_view, "profile", object.__new__(ViewProfile))
        views = (forged_view,)

    with pytest.raises(StorageValidationError, match="invalid"):
        RepoManifestImportPlan(
            manifest_json=plan.manifest_json,
            source=source,
            selection=selection,
            views=views,
        )


def test_public_plan_values_are_bounded_before_traversal_or_encoding() -> None:
    plan = plan_repo_manifest_import(_manifest(include_vector=False), views=["bm25"])
    oversized_json = " " * (DEFAULT_MAX_MANIFEST_BYTES + 1)
    with pytest.raises(StorageValidationError, match="byte limit"):
        replace(plan, manifest_json=oversized_json)
    with pytest.raises(StorageValidationError, match="byte limit"):
        replace(plan.views[0], manifest_entry_json=oversized_json)
    with pytest.raises(StorageValidationError, match="byte limit"):
        replace(plan.views[0], generation_record_json=oversized_json)

    oversized_names = ("bm25",) * (
        len(manifest_storage_module.PORTABLE_STORAGE_VIEWS) + 1
    )
    with pytest.raises(StorageValidationError, match="tuple of portable views"):
        ViewSelection(oversized_names, (), ("bm25",), ())
    oversized_skips = (("vector", "not_current"),) * (
        len(manifest_storage_module.PORTABLE_STORAGE_VIEWS) + 1
    )
    with pytest.raises(StorageValidationError, match="canonical tuple"):
        ViewSelection(("bm25",), ("vector",), ("bm25",), oversized_skips)
    with pytest.raises(StorageValidationError, match="intent tuple"):
        replace(
            plan,
            views=plan.views
            * (len(manifest_storage_module.PORTABLE_STORAGE_VIEWS) + 1),
        )


def test_view_selection_stops_consuming_after_its_fixed_bound() -> None:
    manifest = _manifest(include_vector=False)

    class LyingSelection:
        def __init__(self) -> None:
            self.reads = 0

        def __len__(self) -> int:
            return 1

        def __getitem__(self, index: int) -> str:
            self.reads += 1
            return "bm25"

    requested = LyingSelection()
    with pytest.raises(StorageValidationError, match="too many"):
        plan_repo_manifest_import(
            manifest,
            views=requested,  # type: ignore[arg-type]
        )
    assert requested.reads == manifest_storage_module._MAX_INDEXES + 1


@pytest.mark.parametrize("error", [KeyError("selection failed"), OSError("I/O failed")])
def test_view_selection_normalizes_ordinary_iteration_errors(
    error: BaseException,
) -> None:
    class FailingSelection:
        def __iter__(self):
            raise error

    with pytest.raises(StorageValidationError, match="bounded sequence") as caught:
        plan_repo_manifest_import(
            _manifest(include_vector=False),
            views=FailingSelection(),  # type: ignore[arg-type]
        )
    assert caught.value.__cause__ is error


def test_view_selection_rejects_hostile_or_oversized_string_values() -> None:
    class HostileText(str):
        def strip(self, *args, **kwargs):
            raise AssertionError("overridden strip must not run")

    with pytest.raises(StorageValidationError, match="exact view names"):
        plan_repo_manifest_import(
            _manifest(include_vector=False),
            views=[HostileText("bm25")],
        )
    with pytest.raises(StorageValidationError, match="bounded text"):
        plan_repo_manifest_import(
            _manifest(include_vector=False),
            views=[" " * (manifest_storage_module._MAX_VIEW_NAME + 1)],
        )


def test_mapping_input_is_snapshotted_exactly_once() -> None:
    manifest = _manifest(include_vector=False).to_dict()

    class ChameleonMapping(Mapping[str, object]):
        def __init__(self) -> None:
            self.items_calls = 0

        def __getitem__(self, key: str) -> object:
            return manifest[key]

        def __iter__(self) -> Iterator[str]:
            return iter(manifest)

        def __len__(self) -> int:
            return len(manifest)

        def items(self):
            self.items_calls += 1
            return () if self.items_calls == 1 else manifest.items()

    value = ChameleonMapping()
    with pytest.raises(StorageValidationError, match="invalid shape"):
        plan_repo_manifest_import(value)
    assert value.items_calls == 1


def test_mapping_snapshot_rejects_repeated_container_aliases_after_one_read() -> None:
    class SharedMapping(Mapping[str, object]):
        def __init__(self) -> None:
            self.items_calls = 0

        def __getitem__(self, key: str) -> object:
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            return iter(("value",))

        def __len__(self) -> int:
            return 1

        def items(self):
            self.items_calls += 1
            return (("value", self.items_calls),)

    shared = SharedMapping()
    with pytest.raises(StorageValidationError, match="repeated or cyclic"):
        manifest_storage_module._snapshot_json_value(
            {"first": shared, "second": shared},
            label="aliased mapping",
        )
    assert shared.items_calls == 1


def test_parent_json_budget_is_charged_before_consuming_a_child() -> None:
    reads = 0

    class TrapMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            return iter(())

        def __len__(self) -> int:
            return 0

        def items(self):
            nonlocal reads
            reads += 1
            return ()

    manifest = _manifest(include_vector=False)
    manifest.indexes["bm25"].config = {"x" * 4096: TrapMapping()}
    with pytest.raises(StorageValidationError, match="byte limit"):
        plan_repo_manifest_import(manifest, max_manifest_bytes=1024)
    assert reads == 0


def test_mapping_snapshot_stops_at_the_node_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = 0

    class OversizedMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            return iter(())

        def __len__(self) -> int:
            return 1

        def items(self):
            nonlocal reads
            for index in range(1000):
                reads += 1
                yield f"key-{index}", index

    monkeypatch.setattr(manifest_storage_module, "_MAX_JSON_NODES", 4)
    with pytest.raises(StorageValidationError, match="node limit"):
        plan_repo_manifest_import(OversizedMapping())
    assert reads == 4


def test_mapping_snapshot_counts_del_using_canonical_json_size() -> None:
    payload = {"value": "\x7f"}
    expected = canonical_json(payload).encode("ascii")
    snapshot, estimated = manifest_storage_module._snapshot_json_value(
        payload,
        label="test mapping",
        max_bytes=len(expected),
    )
    assert snapshot == payload
    assert estimated == len(expected)

    with pytest.raises(StorageValidationError, match="byte limit"):
        manifest_storage_module._snapshot_json_value(
            payload,
            label="test mapping",
            max_bytes=len(expected) - 1,
        )


@pytest.mark.parametrize("error", [KeyError("mapping failed"), OSError("I/O failed")])
def test_mapping_snapshot_normalizes_hostile_iteration_errors(
    error: BaseException,
) -> None:
    class FailingMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            return iter(())

        def __len__(self) -> int:
            return 1

        def items(self):
            raise error

    with pytest.raises(StorageValidationError, match="snapshotted") as caught:
        plan_repo_manifest_import(FailingMapping())
    assert caught.value.__cause__ is error


@pytest.mark.parametrize(
    ("location", "field"),
    [
        ("top", "unknown"),
        ("repo", "unknown"),
        ("entry", "unknown"),
    ],
)
def test_manifest_shape_rejects_unknown_fields(location: str, field: str) -> None:
    data = _manifest(include_vector=False).to_dict()
    if location == "top":
        data[field] = True
    elif location == "repo":
        data["repo"][field] = True
    else:
        data["indexes"]["bm25"][field] = True

    with pytest.raises(StorageValidationError, match="invalid shape"):
        plan_repo_manifest_import(data)


@pytest.mark.parametrize(
    "fingerprint",
    ["", "sha256-v3:" + "a" * 64, "sha256-v2:" + "A" * 64, "sha256:short"],
)
def test_source_fingerprint_must_be_recognized_and_canonical(fingerprint: str) -> None:
    manifest = _manifest(include_vector=False)
    manifest.source_fingerprint = fingerprint
    manifest.last_indexed_source_fingerprint = fingerprint
    manifest.indexes["bm25"].source_fingerprint = fingerprint

    with pytest.raises(StorageValidationError, match="source fingerprint"):
        plan_repo_manifest_import(manifest)


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    [
        ("config", "api_key", "secret", "secret field"),
        ("metadata", "endpoint", "https://u:p@example.test", "URL credentials"),
        ("metadata", "error_message", "host path", "diagnostic field"),
    ],
)
def test_selected_entry_rejects_secrets_and_diagnostics(
    section: str, field: str, value: str, message: str
) -> None:
    manifest = _manifest(include_vector=False)
    getattr(manifest.indexes["bm25"], section)[field] = value

    with pytest.raises(StorageValidationError, match=message):
        plan_repo_manifest_import(manifest)


@pytest.mark.parametrize(
    ("view_type", "mutate", "message"),
    [
        (
            "bm25",
            lambda record: record["documents.json"].update(sha256="bad"),
            "SHA-256",
        ),
        (
            "bm25",
            lambda record: record["documents.json"].update(size=True),
            "size",
        ),
        (
            "vector",
            lambda record: record.update(file="config_other.json"),
            "file name",
        ),
        (
            "vector",
            lambda record: record.update(size=16 * 1024 * 1024 + 1),
            "size",
        ),
    ],
)
def test_generation_fingerprint_records_are_strict(
    view_type: str, mutate, message: str
) -> None:
    manifest = _manifest()
    field = (
        "artifact_file_fingerprints"
        if view_type == "bm25"
        else "persistence_config_fingerprint"
    )
    record = manifest.indexes[view_type].config[field]
    mutate(record)
    manifest.indexes[view_type].metadata[field] = copy.deepcopy(record)

    with pytest.raises(StorageValidationError, match=message):
        plan_repo_manifest_import(manifest, views=[view_type])


def test_conflicting_duplicate_generation_records_fail_closed() -> None:
    manifest = _manifest(include_vector=False)
    manifest.indexes["bm25"].metadata["artifact_file_fingerprints"]["documents.json"][
        "sha256"
    ] = ("e" * 64)

    with pytest.raises(StorageValidationError, match="conflicting"):
        plan_repo_manifest_import(manifest)


@pytest.mark.parametrize(
    ("view_type", "field", "value"),
    [
        ("bm25", "artifact_scope", None),
        ("bm25", "artifact_scope", "mutable"),
        ("bm25", "chunk_count", True),
        ("bm25", "file_count", -1),
        ("bm25", "portable_document_format", "unknown"),
        ("bm25", "source_file_count", 100),
        ("vector", "artifact_scope", "mutable"),
        ("vector", "document_count", True),
        ("vector", "document_count", {"l3": 1}),
        ("vector", "document_count", {"l0": -1}),
        ("vector", "last_commit", 1),
        ("vector", "last_commit", "bad\x00commit"),
        ("vector", "portable_document_format", "unknown"),
    ],
)
def test_non_profile_fields_have_explicit_generation_schemas(
    view_type: str,
    field: str,
    value: object,
) -> None:
    manifest = _manifest()
    _set_entry_field(manifest.indexes[view_type], field, value)

    with pytest.raises(StorageValidationError, match="incomplete generation record"):
        plan_repo_manifest_import(manifest, views=[view_type])


def test_supported_portable_generation_fields_remain_importable() -> None:
    manifest = _manifest()
    _set_entry_field(manifest.indexes["bm25"], "artifact_scope", "query-serving")
    _set_entry_field(
        manifest.indexes["bm25"],
        "portable_document_format",
        "codenib.bm25-documents.v1",
    )
    _set_entry_field(manifest.indexes["vector"], "artifact_scope", "query-serving")
    _set_entry_field(
        manifest.indexes["vector"],
        "portable_document_format",
        "codenib.vector-documents.v1",
    )

    plan = plan_repo_manifest_import(manifest)

    assert plan.selection.selected_views == ("bm25", "vector")


def test_optional_invalid_non_profile_field_is_explicitly_inert() -> None:
    manifest = _manifest()
    _set_entry_field(manifest.indexes["vector"], "document_count", True)

    plan = plan_repo_manifest_import(manifest)

    assert plan.selection.selected_views == ("bm25",)
    assert plan.selection.skipped_views == {"vector": "incomplete_generation_record"}


def test_required_vector_document_counts_are_positive_but_may_be_empty() -> None:
    manifest = _manifest()
    _set_entry_field(manifest.indexes["vector"], "document_count", {"l0": 0})

    with pytest.raises(StorageValidationError, match="incomplete generation record"):
        plan_repo_manifest_import(manifest, views=["vector"])

    _set_entry_field(manifest.indexes["vector"], "document_count", {})
    plan = plan_repo_manifest_import(manifest, views=["vector"])

    assert plan.selection.selected_views == ("vector",)


def test_optional_zero_vector_document_count_is_explicitly_inert() -> None:
    manifest = _manifest()
    _set_entry_field(manifest.indexes["vector"], "document_count", {"l0": 0})

    plan = plan_repo_manifest_import(manifest)

    assert plan.selection.selected_views == ("bm25",)
    assert plan.selection.skipped_views == {"vector": "incomplete_generation_record"}


@pytest.mark.parametrize("required", [True, False], ids=["required", "optional"])
@pytest.mark.parametrize(
    "provenance",
    ["full-empty", "incremental", "fallback", "fallback-empty"],
)
def test_vector_accepts_builder_provenance_without_profile_pollution(
    required: bool,
    provenance: str,
) -> None:
    manifest = _manifest()
    requested = ["vector"] if required else None
    baseline = plan_repo_manifest_import(manifest, views=requested)
    fields: dict[str, object]
    if provenance == "full-empty":
        fields = {"last_commit": ""}
    elif provenance == "incremental":
        _remove_entry_field(manifest.indexes["vector"], "last_commit")
        fields = _vector_incremental_provenance()
    else:
        assert provenance in {"fallback", "fallback-empty"}
        fields = {
            "update_mode": "full_rebuild",
            "incremental_fallback_reason": "ValueError",
        }
        if provenance == "fallback-empty":
            fields["last_commit"] = ""
    _set_entry_fields(manifest.indexes["vector"], fields)

    plan = plan_repo_manifest_import(manifest, views=requested)

    assert "vector" in plan.selection.selected_views
    assert plan.view_map["vector"].profile_id == baseline.view_map["vector"].profile_id
    assert plan.view_map["vector"].manifest_entry_digest != (
        baseline.view_map["vector"].manifest_entry_digest
    )


@pytest.mark.parametrize("required", [True, False], ids=["required", "optional"])
@pytest.mark.parametrize(
    "case",
    [
        "partial_incremental",
        "boolean_chunk_count",
        "negative_chunk_count",
        "boolean_cache_rate",
        "negative_cache_rate",
        "high_cache_rate",
        "invalid_new_commit",
        "empty_new_commit",
        "mismatched_new_commit",
        "missing_last_commit",
        "mismatched_last_commit",
        "last_commit_incremental_hybrid",
        "unknown_update_mode",
        "missing_fallback_reason",
        "fallback_missing_last_commit",
        "empty_fallback_reason",
        "nul_fallback_reason",
        "incremental_fallback_conflict",
    ],
)
def test_malformed_vector_provenance_fails_or_skips(
    required: bool,
    case: str,
) -> None:
    incremental = _vector_incremental_provenance()
    fields: dict[str, object]
    if case == "partial_incremental":
        fields = {"chunks_reembedded": 1}
    elif case == "boolean_chunk_count":
        fields = {**incremental, "chunks_reembedded": True}
    elif case == "negative_chunk_count":
        fields = {**incremental, "chunks_from_cache": -1}
    elif case == "boolean_cache_rate":
        fields = {**incremental, "cache_hit_rate": True}
    elif case == "negative_cache_rate":
        fields = {**incremental, "cache_hit_rate": -0.1}
    elif case == "high_cache_rate":
        fields = {**incremental, "cache_hit_rate": 1.1}
    elif case == "invalid_new_commit":
        fields = {**incremental, "new_commit": 1}
    elif case == "empty_new_commit":
        fields = {**incremental, "new_commit": ""}
    elif case == "mismatched_new_commit":
        fields = {**incremental, "new_commit": "f" * 40}
    elif case == "missing_last_commit":
        fields = {}
    elif case == "mismatched_last_commit":
        fields = {"last_commit": "f" * 40}
    elif case == "last_commit_incremental_hybrid":
        fields = incremental
    elif case == "unknown_update_mode":
        fields = {
            "update_mode": "incremental",
            "incremental_fallback_reason": "ValueError",
        }
    elif case == "missing_fallback_reason":
        fields = {"update_mode": "full_rebuild"}
    elif case == "fallback_missing_last_commit":
        fields = {
            "update_mode": "full_rebuild",
            "incremental_fallback_reason": "ValueError",
        }
    elif case == "empty_fallback_reason":
        fields = {
            "update_mode": "full_rebuild",
            "incremental_fallback_reason": "",
        }
    elif case == "nul_fallback_reason":
        fields = {
            "update_mode": "full_rebuild",
            "incremental_fallback_reason": "bad\x00reason",
        }
    else:
        assert case == "incremental_fallback_conflict"
        fields = {
            **incremental,
            "update_mode": "full_rebuild",
            "incremental_fallback_reason": "ValueError",
        }

    manifest = _manifest()
    incremental_fields = set(_vector_incremental_provenance())
    if (
        case != "last_commit_incremental_hybrid"
        and (set(fields) & incremental_fields or case == "missing_last_commit")
    ) or case == "fallback_missing_last_commit":
        _remove_entry_field(manifest.indexes["vector"], "last_commit")
    _set_entry_fields(manifest.indexes["vector"], fields)
    if required:
        with pytest.raises(StorageValidationError, match="generation record"):
            plan_repo_manifest_import(manifest, views=["vector"])
    else:
        plan = plan_repo_manifest_import(manifest)
        assert plan.selection.selected_views == ("bm25",)
        assert plan.selection.skipped_views == {
            "vector": "incomplete_generation_record"
        }


def test_vector_route_redundancies_are_checked_without_native_execution() -> None:
    manifest = _manifest()
    manifest.indexes["vector"].config["dimension"] = 8
    manifest.indexes["vector"].metadata["dimension"] = 8

    with pytest.raises(StorageValidationError, match="dimensions must agree"):
        plan_repo_manifest_import(manifest, views=["vector"])


def test_canonical_manifest_digest_commits_full_entry_but_not_object_authority() -> (
    None
):
    plan = plan_repo_manifest_import(_manifest(include_vector=False))
    manifest_data = json.loads(plan.canonical_manifest_bytes)

    assert canonical_json(manifest_data).encode("ascii") == (
        plan.canonical_manifest_bytes
    )
    assert plan.manifest_digest == plan.to_dict()["manifest"]["digest"]
    assert plan.view_map["bm25"].manifest_entry["path"] == "views/bm25"
    assert "object" not in plan.view_map["bm25"].to_dict()
    assert "receipt" not in plan.view_map["bm25"].to_dict()


def test_dataclass_replace_changes_only_display_provenance_identity_layer() -> None:
    plan = plan_repo_manifest_import(_manifest(include_vector=False))
    changed_source = replace(plan.source, display_commit="e" * 40)

    assert changed_source.identity_digest == plan.source.identity_digest
    assert changed_source.to_dict() != plan.source.to_dict()


@pytest.mark.parametrize(
    "changes",
    [
        {"fingerprint_version": True},
        {"fingerprint_version": 2.0},
        {"fingerprint_version": 1},
        {"fingerprint": "sha256-v2:" + "A" * 64},
        {"file_count": True},
        {"file_count": -1},
        {"display_commit": "bad-\ud800"},
    ],
)
def test_public_source_intent_constructor_enforces_canonical_binding(
    changes: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "fingerprint": "sha256-v2:" + "a" * 64,
        "fingerprint_version": 2,
        "file_count": 1,
        "display_commit": "display",
    }
    values.update(changes)

    with pytest.raises(StorageValidationError):
        SourceIntent(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "required_views": ("bm25", "bm25"),
            "optional_views": (),
            "selected_views": ("bm25",),
            "skipped_items": (),
        },
        {
            "required_views": ("bm25",),
            "optional_views": ("bm25",),
            "selected_views": ("bm25",),
            "skipped_items": (),
        },
        {
            "required_views": ("bm25",),
            "optional_views": ("vector",),
            "selected_views": ("bm25",),
            "skipped_items": (),
        },
        {
            "required_views": ("bm25",),
            "optional_views": ("vector",),
            "selected_views": ("bm25",),
            "skipped_items": (
                ("vector", "not_current"),
                ("vector", "incomplete_profile"),
            ),
        },
    ],
)
def test_public_selection_constructor_enforces_canonical_closure(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(StorageValidationError):
        ViewSelection(**kwargs)  # type: ignore[arg-type]


def test_public_view_intent_constructor_binds_canonical_json_and_profile() -> None:
    plan = plan_repo_manifest_import(_manifest(include_vector=False))
    intent = plan.views[0]
    pretty_entry = json.dumps(intent.manifest_entry, indent=2)
    pretty_generation = json.dumps(intent.generation_record, indent=2)
    forged_profile = ViewProfile.create(
        "bm25",
        {"contract": "forged"},
        name=REPO_MANIFEST_PROFILE_NAME,
    )

    with pytest.raises(StorageValidationError, match="canonical JSON"):
        replace(intent, manifest_entry_json=pretty_entry)
    with pytest.raises(StorageValidationError, match="profile does not match"):
        replace(intent, profile=forged_profile)
    with pytest.raises(StorageValidationError, match="canonical JSON"):
        replace(intent, generation_record_json=pretty_generation)


def test_public_plan_constructor_rejects_duplicate_or_cross_unbound_views() -> None:
    plan = plan_repo_manifest_import(_manifest())
    bm25 = plan.view_map["bm25"]

    with pytest.raises(StorageValidationError, match="duplicate view"):
        replace(plan, views=(bm25, bm25))

    forged_selection = ViewSelection(
        required_views=("bm25",),
        optional_views=("vector",),
        selected_views=("bm25",),
        skipped_items=(("vector", "not_current"),),
    )
    with pytest.raises(StorageValidationError, match="skip reason conflicts"):
        replace(plan, selection=forged_selection, views=(bm25,))


def test_public_plan_constructor_binds_source_and_canonical_manifest() -> None:
    plan = plan_repo_manifest_import(_manifest(include_vector=False))
    other_source = replace(plan.source, display_commit="other")

    with pytest.raises(StorageValidationError, match="source intent"):
        replace(plan, source=other_source)
    with pytest.raises(StorageValidationError, match="canonical JSON"):
        RepoManifestImportPlan(
            manifest_json=json.dumps(json.loads(plan.manifest_json), indent=2),
            source=plan.source,
            selection=plan.selection,
            views=plan.views,
        )


def test_public_view_intent_rejects_view_profile_cross_binding() -> None:
    plan = plan_repo_manifest_import(_manifest())
    vector = plan.view_map["vector"]

    with pytest.raises(StorageValidationError):
        ViewImportIntent(
            view_type="bm25",
            required=False,
            manifest_entry_json=vector.manifest_entry_json,
            profile=vector.profile,
            generation_record_json=vector.generation_record_json,
        )
