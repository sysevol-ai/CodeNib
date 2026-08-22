# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import pytest

import codenib.wiki.media_storage as media_storage
from codenib.wiki.media_storage import (
    MULTIMODAL_KNOWLEDGE_BUNDLE_SCHEMA,
    build_multimodal_knowledge_bundle,
    load_multimodal_knowledge_bundle,
    save_multimodal_knowledge_bundle,
    validate_multimodal_knowledge_bundle,
)


def _bundle():
    return build_multimodal_knowledge_bundle(
        media_manifest={
            "manifest_sha256": "media-hash",
            "artifact_count": 1,
            "artifacts": [],
        },
        visual_facts_manifest={
            "manifest_sha256": "facts-hash",
            "fact_count": 1,
            "facts": [],
        },
        source_candidate_count=3,
        grounding_manifest={
            "manifest_sha256": "grounding-hash",
            "binding_count": 2,
            "bindings": [],
        },
        knowledge_view={
            "view_sha256": "view-hash",
            "entry_count": 1,
            "entries": [],
        },
    )


def test_build_multimodal_knowledge_bundle_records_schema_and_hashes():
    bundle = _bundle()

    assert bundle["schema"] == MULTIMODAL_KNOWLEDGE_BUNDLE_SCHEMA
    assert bundle["schema_version"] == 1
    assert bundle["component_sha256"] == {
        "media_manifest": "media-hash",
        "visual_facts_manifest": "facts-hash",
        "grounding_manifest": "grounding-hash",
        "knowledge_view": "view-hash",
    }
    assert len(bundle["bundle_sha256"]) == 64
    assert (
        validate_multimodal_knowledge_bundle(bundle)["bundle_sha256"]
        == bundle["bundle_sha256"]
    )


def test_save_and_load_multimodal_knowledge_bundle_round_trips(tmp_path):
    path = tmp_path / "nested" / "bundle.json"
    bundle = _bundle()

    save_multimodal_knowledge_bundle(bundle, path)
    loaded = load_multimodal_knowledge_bundle(path)

    assert loaded == bundle
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == (
        MULTIMODAL_KNOWLEDGE_BUNDLE_SCHEMA
    )


def test_validate_multimodal_knowledge_bundle_rejects_tampering():
    bundle = _bundle()
    bundle["source_candidate_count"] = 4

    with pytest.raises(ValueError, match="hash"):
        validate_multimodal_knowledge_bundle(bundle)


def test_load_multimodal_knowledge_bundle_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(media_storage, "_MAX_BUNDLE_BYTES", 8)
    path = tmp_path / "bundle.json"
    path.write_text("x" * 9, encoding="utf-8")

    with pytest.raises(ValueError, match="byte limit"):
        load_multimodal_knowledge_bundle(path)
