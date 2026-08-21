# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

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
    previous_facts = {
        "manifest_sha256": "previous-facts",
        "facts": [
            _fact("unchanged.png", "same"),
            _fact("changed.png", "old"),
            _fact("removed.png", "gone"),
        ],
    }

    plan = plan_incremental_visual_fact_update(
        previous_media,
        current_media,
        previous_facts,
    )

    assert [fact["artifact_path"] for fact in plan["reusable_fact_packs"]] == [
        "unchanged.png"
    ]
    assert plan["extract_artifact_paths"] == ["added.png", "changed.png"]
    assert plan["removed_artifact_paths"] == ["removed.png"]


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
