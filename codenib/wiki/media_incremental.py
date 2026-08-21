# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Incremental update planning for multimodal repository knowledge."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from .media_facts import (
    MEDIA_FACTS_SCHEMA,
    MEDIA_FACTS_VERSION,
    normalize_visual_fact_pack,
)

MEDIA_INCREMENTAL_SCHEMA = "codenib.media-incremental-plan.v1"
MEDIA_INCREMENTAL_VERSION = 1


def diff_media_manifests(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a stable path/hash diff between two media manifests."""

    previous_artifacts = _artifacts_by_path(previous)
    current_artifacts = _artifacts_by_path(current)
    changes = []
    for path in sorted(set(previous_artifacts) | set(current_artifacts)):
        before = previous_artifacts.get(path)
        after = current_artifacts.get(path)
        if before is None:
            status = "added"
        elif after is None:
            status = "removed"
        elif before.get("sha256") == after.get("sha256"):
            status = "unchanged"
        else:
            status = "changed"
        changes.append(
            {
                "path": path,
                "status": status,
                "previous_sha256": str((before or {}).get("sha256") or ""),
                "current_sha256": str((after or {}).get("sha256") or ""),
            }
        )
    return {
        "schema": MEDIA_INCREMENTAL_SCHEMA,
        "version": MEDIA_INCREMENTAL_VERSION,
        "previous_media_manifest_sha256": str(previous.get("manifest_sha256") or ""),
        "current_media_manifest_sha256": str(current.get("manifest_sha256") or ""),
        "counts": {
            "added": sum(1 for change in changes if change["status"] == "added"),
            "removed": sum(1 for change in changes if change["status"] == "removed"),
            "changed": sum(1 for change in changes if change["status"] == "changed"),
            "unchanged": sum(
                1 for change in changes if change["status"] == "unchanged"
            ),
        },
        "changes": changes,
    }


def plan_incremental_visual_fact_update(
    previous_media_manifest: Mapping[str, Any],
    current_media_manifest: Mapping[str, Any],
    previous_visual_facts_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Plan which visual facts can be reused and which artifacts need VLM work."""

    diff = diff_media_manifests(previous_media_manifest, current_media_manifest)
    current_artifacts = _artifacts_by_path(current_media_manifest)
    previous_facts = {
        str(fact.get("artifact_path") or ""): dict(fact)
        for fact in previous_visual_facts_manifest.get("facts") or ()
        if isinstance(fact, Mapping) and fact.get("artifact_path")
    }
    reusable_fact_packs = []
    extract_artifact_paths = []
    removed_artifact_paths = []
    for change in diff["changes"]:
        path = change["path"]
        if change["status"] == "unchanged" and path in previous_facts:
            fact = previous_facts[path]
            if fact.get("artifact_sha256") == current_artifacts[path].get("sha256"):
                reusable_fact_packs.append(fact)
            else:
                extract_artifact_paths.append(path)
        elif change["status"] in {"added", "changed"}:
            extract_artifact_paths.append(path)
        elif change["status"] == "removed":
            removed_artifact_paths.append(path)
    return {
        "schema": MEDIA_INCREMENTAL_SCHEMA,
        "version": MEDIA_INCREMENTAL_VERSION,
        "media_diff": diff,
        "current_media_manifest_sha256": str(
            current_media_manifest.get("manifest_sha256") or ""
        ),
        "previous_visual_facts_manifest_sha256": str(
            previous_visual_facts_manifest.get("manifest_sha256") or ""
        ),
        "reusable_fact_packs": reusable_fact_packs,
        "extract_artifact_paths": sorted(extract_artifact_paths),
        "removed_artifact_paths": sorted(removed_artifact_paths),
    }


def merge_incremental_visual_facts(
    current_media_manifest: Mapping[str, Any],
    reusable_fact_packs: Iterable[Mapping[str, Any]],
    new_fact_packs: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Merge reused and newly extracted fact packs for the current media manifest."""

    current_artifacts = _artifacts_by_path(current_media_manifest)
    by_path: dict[str, dict[str, Any]] = {}
    for pack in list(reusable_fact_packs or ()) + list(new_fact_packs or ()):
        if not isinstance(pack, Mapping):
            continue
        normalized = normalize_visual_fact_pack(pack)
        path = normalized.get("artifact_path")
        artifact = current_artifacts.get(path)
        if artifact is None or normalized.get("artifact_sha256") != artifact.get(
            "sha256"
        ):
            continue
        by_path[path] = normalized
    facts = [by_path[path] for path in sorted(by_path)]
    payload = {
        "schema": MEDIA_FACTS_SCHEMA,
        "version": MEDIA_FACTS_VERSION,
        "media_manifest_sha256": str(
            current_media_manifest.get("manifest_sha256") or ""
        ),
        "fact_count": len(facts),
        "facts": facts,
    }
    payload["manifest_sha256"] = _sha256_json(
        {key: value for key, value in payload.items() if key != "manifest_sha256"}
    )
    return payload


def _artifacts_by_path(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(artifact.get("path") or ""): dict(artifact)
        for artifact in manifest.get("artifacts") or ()
        if isinstance(artifact, Mapping) and artifact.get("path")
    }


def _sha256_json(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "MEDIA_INCREMENTAL_SCHEMA",
    "MEDIA_INCREMENTAL_VERSION",
    "diff_media_manifests",
    "merge_incremental_visual_facts",
    "plan_incremental_visual_fact_update",
]
