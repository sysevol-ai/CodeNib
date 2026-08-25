# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import stat

import pytest

import codenib.wiki.media_storage as media_storage
from codenib.wiki.media_artifacts import MEDIA_MANIFEST_SCHEMA
from codenib.wiki.media_facts import MEDIA_FACTS_SCHEMA
from codenib.wiki.media_grounding import MEDIA_GROUNDING_SCHEMA
from codenib.wiki.media_knowledge import MULTIMODAL_KNOWLEDGE_SCHEMA
from codenib.wiki.media_storage import (
    MULTIMODAL_KNOWLEDGE_BUNDLE_SCHEMA,
    build_multimodal_knowledge_bundle,
    load_multimodal_knowledge_bundle,
    save_multimodal_knowledge_bundle,
    validate_multimodal_knowledge_bundle,
)


def _sha256_json(value, *, ensure_ascii=True):
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=ensure_ascii,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _bundle(*, metadata=None):
    media_manifest = {
        "schema": MEDIA_MANIFEST_SCHEMA,
        "version": 1,
        "commit": "abc123",
        "source_selection_digest": "sha256:" + "a" * 64,
        "artifact_count": 0,
        "artifacts": [],
        "metadata": dict(metadata or {}),
    }
    media_manifest["manifest_sha256"] = _sha256_json(
        {
            key: value
            for key, value in media_manifest.items()
            if key not in {"artifact_count", "manifest_sha256"}
        }
    )
    visual_facts_manifest = {
        "schema": MEDIA_FACTS_SCHEMA,
        "version": 1,
        "media_manifest_sha256": media_manifest["manifest_sha256"],
        "fact_count": 0,
        "facts": [],
    }
    visual_facts_manifest["manifest_sha256"] = _sha256_json(
        {
            key: value
            for key, value in visual_facts_manifest.items()
            if key not in {"fact_count", "manifest_sha256"}
        }
    )
    grounding_manifest = {
        "schema": MEDIA_GROUNDING_SCHEMA,
        "version": 1,
        "visual_facts_manifest_sha256": visual_facts_manifest["manifest_sha256"],
        "binding_count": 0,
        "bindings": [],
    }
    grounding_manifest["manifest_sha256"] = _sha256_json(
        {
            key: value
            for key, value in grounding_manifest.items()
            if key not in {"binding_count", "manifest_sha256"}
        }
    )
    knowledge_view = {
        "schema": MULTIMODAL_KNOWLEDGE_SCHEMA,
        "version": 1,
        "media_manifest_sha256": media_manifest["manifest_sha256"],
        "visual_facts_manifest_sha256": visual_facts_manifest["manifest_sha256"],
        "grounding_manifest_sha256": grounding_manifest["manifest_sha256"],
        "entry_count": 0,
        "entries": [],
    }
    knowledge_view["view_sha256"] = _sha256_json(knowledge_view)
    return build_multimodal_knowledge_bundle(
        media_manifest=media_manifest,
        visual_facts_manifest=visual_facts_manifest,
        source_candidate_count=3,
        grounding_manifest=grounding_manifest,
        knowledge_view=knowledge_view,
    )


def _rehash_bundle(bundle):
    bundle["bundle_sha256"] = _sha256_json(
        {key: value for key, value in bundle.items() if key != "bundle_sha256"},
        ensure_ascii=False,
    )


def test_build_multimodal_knowledge_bundle_records_schema_and_hashes():
    bundle = _bundle()

    assert bundle["schema"] == MULTIMODAL_KNOWLEDGE_BUNDLE_SCHEMA
    assert bundle["schema_version"] == 1
    assert bundle["component_sha256"] == {
        "media_manifest": bundle["media_manifest"]["manifest_sha256"],
        "visual_facts_manifest": bundle["visual_facts_manifest"]["manifest_sha256"],
        "grounding_manifest": bundle["grounding_manifest"]["manifest_sha256"],
        "knowledge_view": bundle["knowledge_view"]["view_sha256"],
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


def test_save_multimodal_knowledge_bundle_preserves_existing_permissions(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text("old generation", encoding="utf-8")
    path.chmod(0o640)

    save_multimodal_knowledge_bundle(_bundle(), path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_validate_multimodal_knowledge_bundle_rejects_tampering():
    bundle = _bundle()
    bundle["source_candidate_count"] = 4

    with pytest.raises(ValueError, match="hash"):
        validate_multimodal_knowledge_bundle(bundle)


def test_validate_bundle_recomputes_component_hashes_after_outer_rehash():
    bundle = _bundle()
    bundle["media_manifest"]["commit"] = "tampered"
    _rehash_bundle(bundle)

    with pytest.raises(ValueError, match="media manifest hash"):
        validate_multimodal_knowledge_bundle(bundle)


def test_validate_bundle_rejects_missing_bundle_hash():
    bundle = _bundle()
    bundle.pop("bundle_sha256")

    with pytest.raises(ValueError, match="bundle_sha256"):
        validate_multimodal_knowledge_bundle(bundle)


def test_validate_bundle_rejects_unknown_component_hash():
    bundle = _bundle()
    bundle["component_sha256"]["unknown"] = "f" * 64
    _rehash_bundle(bundle)

    with pytest.raises(ValueError, match="component hash inventory"):
        validate_multimodal_knowledge_bundle(bundle)


def test_validate_bundle_returns_an_independent_copy():
    bundle = _bundle()

    validated = validate_multimodal_knowledge_bundle(bundle)
    validated["media_manifest"]["artifacts"].append({"path": "changed"})

    assert bundle["media_manifest"]["artifacts"] == []


def test_bundle_component_hashes_support_unicode_metadata():
    bundle = _bundle(metadata={"caption": "架构图"})

    assert validate_multimodal_knowledge_bundle(bundle) == bundle


def test_load_multimodal_knowledge_bundle_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(media_storage, "_MAX_BUNDLE_BYTES", 8)
    path = tmp_path / "bundle.json"
    path.write_text("x" * 9, encoding="utf-8")

    with pytest.raises(ValueError, match="byte limit"):
        load_multimodal_knowledge_bundle(path)


def test_load_multimodal_knowledge_bundle_rejects_duplicate_keys(tmp_path):
    bundle = _bundle()
    serialized = json.dumps(bundle)
    path = tmp_path / "bundle.json"
    path.write_text(
        serialized[:-1] + f', "schema": "{MULTIMODAL_KNOWLEDGE_BUNDLE_SCHEMA}"}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON object key"):
        load_multimodal_knowledge_bundle(path)


def test_load_multimodal_knowledge_bundle_rejects_nonfinite_numbers(tmp_path):
    bundle = _bundle()
    serialized = json.dumps(bundle)
    path = tmp_path / "bundle.json"
    path.write_text(serialized[:-1] + ', "unexpected": NaN}', encoding="utf-8")

    with pytest.raises(ValueError, match="non-finite JSON number"):
        load_multimodal_knowledge_bundle(path)


def test_load_multimodal_knowledge_bundle_rejects_symlink(tmp_path):
    target = tmp_path / "target.json"
    save_multimodal_knowledge_bundle(_bundle(), target)
    link = tmp_path / "bundle.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="stable regular file"):
        load_multimodal_knowledge_bundle(link)
