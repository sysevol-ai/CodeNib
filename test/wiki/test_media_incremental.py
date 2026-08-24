# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from codenib.wiki.media_facts import (
    build_visual_facts_manifest,
    compute_visual_facts_manifest_sha256,
)
from codenib.wiki.media_incremental import (
    diff_media_manifests,
    merge_incremental_visual_facts,
    plan_incremental_visual_fact_update,
)


def _artifact(path, sha):
    return {
        "path": path,
        "sha256": sha,
        "role_hint": "repository_image",
        "mime_type": "image/png",
    }


def _fact(path, sha, name="Component"):
    return {
        "artifact_path": path,
        "artifact_sha256": sha,
        "role_hint": "repository_image",
        "extractor": "local/metadata",
        "entities": [
            {
                "name": name,
                "type": "component",
                "evidence": path,
                "confidence": 0.5,
                "grounding_candidates": [name],
            }
        ],
        "relations": [],
        "claims": [],
        "metadata": {},
    }


def _facts_manifest(media, facts):
    return merge_incremental_visual_facts(
        media,
        reusable_fact_packs=(),
        new_fact_packs=facts,
    )


def test_diff_media_manifests_reports_added_removed_changed_unchanged():
    previous = {
        "manifest_sha256": "previous",
        "artifacts": [
            _artifact("unchanged.png", "same"),
            _artifact("changed.png", "old"),
            _artifact("removed.png", "gone"),
        ],
    }
    current = {
        "manifest_sha256": "current",
        "artifacts": [
            _artifact("unchanged.png", "same"),
            _artifact("changed.png", "new"),
            _artifact("added.png", "fresh"),
        ],
    }

    diff = diff_media_manifests(previous, current)

    assert diff["schema"] == "codenib.media-manifest-diff.v1"
    assert diff["diff_sha256"]
    assert diff["counts"] == {
        "added": 1,
        "removed": 1,
        "changed": 1,
        "unchanged": 1,
    }
    statuses = {change["path"]: change["status"] for change in diff["changes"]}
    assert statuses == {
        "added.png": "added",
        "changed.png": "changed",
        "removed.png": "removed",
        "unchanged.png": "unchanged",
    }


def test_plan_incremental_visual_fact_update_reuses_only_matching_facts():
    previous_media = {
        "manifest_sha256": "previous-media",
        "artifacts": [
            _artifact("unchanged.png", "same"),
            _artifact("changed.png", "old"),
            _artifact("removed.png", "gone"),
        ],
    }
    current_media = {
        "manifest_sha256": "current-media",
        "artifacts": [
            _artifact("unchanged.png", "same"),
            _artifact("changed.png", "new"),
            _artifact("added.png", "fresh"),
        ],
    }
    previous_facts = _facts_manifest(
        previous_media,
        [
            _fact("unchanged.png", "same"),
            _fact("changed.png", "old"),
            _fact("removed.png", "gone"),
        ],
    )

    plan = plan_incremental_visual_fact_update(
        previous_media,
        current_media,
        previous_facts,
        expected_extractor="local/metadata",
    )

    assert [fact["artifact_path"] for fact in plan["reusable_fact_packs"]] == [
        "unchanged.png"
    ]
    assert plan["extract_artifact_paths"] == ["added.png", "changed.png"]
    assert plan["removed_artifact_paths"] == ["removed.png"]
    assert plan["schema"] == "codenib.media-incremental-plan.v1"
    assert plan["plan_sha256"]


def test_plan_extracts_unchanged_artifact_when_previous_fact_is_missing():
    media = {
        "manifest_sha256": "media",
        "artifacts": [_artifact("missing-fact.png", "same")],
    }

    plan = plan_incremental_visual_fact_update(
        media,
        media,
        _facts_manifest(media, []),
        expected_extractor="local/metadata",
    )

    assert plan["reusable_fact_packs"] == []
    assert plan["extract_artifact_paths"] == ["missing-fact.png"]


def test_plan_materializes_generator_manifests_once():
    artifact = _artifact("diagram.png", "same")
    previous_media = {
        "manifest_sha256": "previous-media",
        "artifacts": (item for item in [artifact]),
    }
    current_media = {
        "manifest_sha256": "current-media",
        "artifacts": (item for item in [artifact]),
    }
    previous_facts = _facts_manifest(
        {"manifest_sha256": "previous-media", "artifacts": [artifact]},
        [_fact("diagram.png", "same")],
    )
    previous_facts["facts"] = (fact for fact in previous_facts["facts"])

    plan = plan_incremental_visual_fact_update(
        previous_media,
        current_media,
        previous_facts,
        expected_extractor="local/metadata",
    )

    assert [fact["artifact_path"] for fact in plan["reusable_fact_packs"]] == [
        "diagram.png"
    ]
    assert plan["extract_artifact_paths"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("caption", "Updated caption"),
        ("surrounding_text", "Updated Markdown context"),
        ("mime_type", "image/svg+xml"),
        ("role_hint", "architecture_diagram"),
        (
            "references",
            [
                {
                    "markdown_path": "README.md",
                    "line": 4,
                    "alt_text": "Architecture",
                    "title": "Overview",
                }
            ],
        ),
    ],
)
def test_plan_reextracts_when_artifact_context_changes(field, value):
    previous_artifact = _artifact("diagram.png", "same")
    current_artifact = {**previous_artifact, field: value}
    previous_media = {
        "manifest_sha256": "previous-media",
        "artifacts": [previous_artifact],
    }
    current_media = {
        "manifest_sha256": "current-media",
        "artifacts": [current_artifact],
    }

    plan = plan_incremental_visual_fact_update(
        previous_media,
        current_media,
        _facts_manifest(previous_media, [_fact("diagram.png", "same")]),
        expected_extractor="local/metadata",
    )

    assert plan["media_diff"]["changes"][0]["status"] == "changed"
    assert plan["extract_artifact_paths"] == ["diagram.png"]
    assert plan["reusable_fact_packs"] == []


@pytest.mark.parametrize("expected_extractor", [None, "openai-compatible"])
def test_plan_requires_matching_extractor_for_reuse(expected_extractor):
    media = {
        "manifest_sha256": "media",
        "artifacts": [_artifact("diagram.png", "same")],
    }

    plan = plan_incremental_visual_fact_update(
        media,
        media,
        _facts_manifest(media, [_fact("diagram.png", "same")]),
        expected_extractor=expected_extractor,
    )

    assert plan["expected_extractor"] == (expected_extractor or "")
    assert plan["extract_artifact_paths"] == ["diagram.png"]
    assert plan["reusable_fact_packs"] == []


@pytest.mark.parametrize("expected_extractor", ["", "   ", 1])
def test_plan_rejects_invalid_expected_extractors(expected_extractor):
    media = {"manifest_sha256": "media", "artifacts": []}

    with pytest.raises(ValueError, match="expected_extractor"):
        plan_incremental_visual_fact_update(
            media,
            media,
            _facts_manifest(media, []),
            expected_extractor=expected_extractor,
        )


@pytest.mark.parametrize("corruption", ["manifest", "association", "fact_pack"])
def test_plan_rejects_untrusted_previous_fact_manifests(corruption):
    media = {
        "manifest_sha256": "media",
        "artifacts": [_artifact("diagram.png", "same")],
    }
    facts = _facts_manifest(media, [_fact("diagram.png", "same")])
    if corruption == "manifest":
        facts["manifest_sha256"] = "corrupted"
    elif corruption == "association":
        facts["media_manifest_sha256"] = "different-media"
        facts["manifest_sha256"] = compute_visual_facts_manifest_sha256(
            schema=facts["schema"],
            version=facts["version"],
            media_manifest_sha256=facts["media_manifest_sha256"],
            facts=facts["facts"],
        )
    else:
        facts["facts"][0]["entities"][0]["name"] = "Tampered"
        facts["manifest_sha256"] = compute_visual_facts_manifest_sha256(
            schema=facts["schema"],
            version=facts["version"],
            media_manifest_sha256=facts["media_manifest_sha256"],
            facts=facts["facts"],
        )

    plan = plan_incremental_visual_fact_update(
        media,
        media,
        facts,
        expected_extractor="local/metadata",
    )

    assert plan["extract_artifact_paths"] == ["diagram.png"]
    assert plan["reusable_fact_packs"] == []


def test_merge_incremental_visual_facts_keeps_current_artifacts_only():
    current_media = {
        "manifest_sha256": "current-media",
        "artifacts": [
            _artifact("unchanged.png", "same"),
            _artifact("added.png", "fresh"),
        ],
    }

    merged = merge_incremental_visual_facts(
        current_media,
        reusable_fact_packs=[
            _fact("unchanged.png", "same", name="Reused"),
            _fact("removed.png", "gone", name="Removed"),
            _fact("changed.png", "old", name="Stale"),
        ],
        new_fact_packs=[_fact("added.png", "fresh", name="New")],
    )

    assert merged["schema"] == "codenib.media-facts.v1"
    assert merged["media_manifest_sha256"] == "current-media"
    assert merged["fact_count"] == 2
    assert [fact["artifact_path"] for fact in merged["facts"]] == [
        "added.png",
        "unchanged.png",
    ]
    assert merged["manifest_sha256"]


def test_merge_anchors_reused_facts_to_current_artifact_provenance():
    artifact = _artifact("diagram.png", "same")
    fact = _fact("diagram.png", "same", name="Reused")
    fact["role_hint"] = "forged"

    merged = merge_incremental_visual_facts(
        {"manifest_sha256": "media", "artifacts": [artifact]},
        reusable_fact_packs=(pack for pack in [fact]),
        new_fact_packs=(),
    )

    assert merged["facts"][0]["artifact_path"] == "diagram.png"
    assert merged["facts"][0]["artifact_sha256"] == "same"
    assert merged["facts"][0]["role_hint"] == "repository_image"


def test_incremental_merge_uses_the_canonical_visual_facts_digest():
    media = {
        "manifest_sha256": "media",
        "artifacts": [_artifact("diagram.png", "same")],
    }
    full = build_visual_facts_manifest(media)

    incremental = merge_incremental_visual_facts(
        media,
        reusable_fact_packs=full["facts"],
        new_fact_packs=(),
    )

    assert incremental == full


@pytest.mark.parametrize("path", ["../secret.png", "/tmp/secret.png", "bad\\x.png"])
def test_incremental_helpers_reject_unsafe_artifact_paths(path):
    manifest = {
        "manifest_sha256": "media",
        "artifacts": [_artifact(path, "same")],
    }

    with pytest.raises(ValueError, match="repository-relative"):
        diff_media_manifests({}, manifest)


def test_incremental_helpers_reject_duplicate_artifact_paths():
    manifest = {
        "manifest_sha256": "media",
        "artifacts": [
            _artifact("diagram.png", "first"),
            _artifact("diagram.png", "second"),
        ],
    }

    with pytest.raises(ValueError, match="duplicate"):
        diff_media_manifests({}, manifest)


def test_incremental_helpers_reject_artifacts_without_content_hashes():
    manifest = {
        "manifest_sha256": "media",
        "artifacts": [_artifact("diagram.png", "")],
    }

    with pytest.raises(ValueError, match="sha256"):
        diff_media_manifests({}, manifest)


def test_incremental_helpers_reject_non_object_manifests():
    with pytest.raises(ValueError, match="media manifest"):
        diff_media_manifests([], {})
    with pytest.raises(ValueError, match="visual facts manifest"):
        plan_incremental_visual_fact_update({}, {}, [])
