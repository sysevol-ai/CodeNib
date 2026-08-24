# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Incremental update planning for multimodal repository knowledge."""

from __future__ import annotations

import hashlib
import json
from itertools import chain, islice
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from .media_facts import (
    MEDIA_FACTS_SCHEMA,
    MEDIA_FACTS_VERSION,
    normalize_visual_fact_pack,
)

MEDIA_INCREMENTAL_SCHEMA = "codenib.media-incremental-plan.v1"
MEDIA_INCREMENTAL_VERSION = 1
MEDIA_MANIFEST_DIFF_SCHEMA = "codenib.media-manifest-diff.v1"
MEDIA_MANIFEST_DIFF_VERSION = 1
_MAX_ARTIFACTS = 4096
_MAX_FACT_PACKS = 4096
_MAX_TEXT_BYTES = 4096


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
        previous_sha256 = _safe_text((before or {}).get("sha256"))
        current_sha256 = _safe_text((after or {}).get("sha256"))
        if before is None:
            status = "added"
        elif after is None:
            status = "removed"
        elif previous_sha256 == current_sha256:
            status = "unchanged"
        else:
            status = "changed"
        changes.append(
            {
                "path": path,
                "status": status,
                "previous_sha256": previous_sha256,
                "current_sha256": current_sha256,
            }
        )
    payload = {
        "schema": MEDIA_MANIFEST_DIFF_SCHEMA,
        "version": MEDIA_MANIFEST_DIFF_VERSION,
        "previous_media_manifest_sha256": _safe_text(previous.get("manifest_sha256")),
        "current_media_manifest_sha256": _safe_text(current.get("manifest_sha256")),
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
    payload["diff_sha256"] = _sha256_json(payload)
    return payload


def plan_incremental_visual_fact_update(
    previous_media_manifest: Mapping[str, Any],
    current_media_manifest: Mapping[str, Any],
    previous_visual_facts_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Plan which visual facts can be reused and which artifacts need VLM work."""

    diff = diff_media_manifests(previous_media_manifest, current_media_manifest)
    current_artifacts = _artifacts_by_path(current_media_manifest)
    previous_visual_facts_manifest = _require_mapping(
        previous_visual_facts_manifest,
        label="previous visual facts manifest",
    )
    previous_facts: dict[str, Mapping[str, Any]] = {}
    for fact in _mapping_items(
        previous_visual_facts_manifest.get("facts"),
        limit=_MAX_FACT_PACKS,
    ):
        path = _safe_relative_path(fact.get("artifact_path"))
        if path:
            previous_facts.setdefault(path, fact)
    reusable_fact_packs = []
    extract_artifact_paths = []
    removed_artifact_paths = []
    for change in diff["changes"]:
        path = change["path"]
        if change["status"] == "unchanged":
            artifact = current_artifacts[path]
            fact = previous_facts.get(path)
            if fact is None or _safe_text(fact.get("artifact_sha256")) != _safe_text(
                artifact.get("sha256")
            ):
                extract_artifact_paths.append(path)
                continue
            try:
                reusable_fact_packs.append(
                    normalize_visual_fact_pack(fact, artifact=artifact)
                )
            except ValueError:
                extract_artifact_paths.append(path)
        elif change["status"] in {"added", "changed"}:
            extract_artifact_paths.append(path)
        elif change["status"] == "removed":
            removed_artifact_paths.append(path)
    payload = {
        "schema": MEDIA_INCREMENTAL_SCHEMA,
        "version": MEDIA_INCREMENTAL_VERSION,
        "media_diff": diff,
        "current_media_manifest_sha256": _safe_text(
            current_media_manifest.get("manifest_sha256") or ""
        ),
        "previous_visual_facts_manifest_sha256": _safe_text(
            previous_visual_facts_manifest.get("manifest_sha256") or ""
        ),
        "reusable_fact_packs": reusable_fact_packs,
        "extract_artifact_paths": sorted(extract_artifact_paths),
        "removed_artifact_paths": sorted(removed_artifact_paths),
    }
    payload["plan_sha256"] = _sha256_json(payload)
    return payload


def merge_incremental_visual_facts(
    current_media_manifest: Mapping[str, Any],
    reusable_fact_packs: Iterable[Mapping[str, Any]],
    new_fact_packs: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Merge reused and newly extracted fact packs for the current media manifest."""

    current_artifacts = _artifacts_by_path(current_media_manifest)
    by_path: dict[str, dict[str, Any]] = {}
    packs = chain(
        _mapping_items(reusable_fact_packs, limit=_MAX_FACT_PACKS),
        _mapping_items(new_fact_packs, limit=_MAX_FACT_PACKS),
    )
    for pack in packs:
        path = _safe_relative_path(pack.get("artifact_path"))
        artifact = current_artifacts.get(path)
        if artifact is None or _safe_text(pack.get("artifact_sha256")) != _safe_text(
            artifact.get("sha256")
        ):
            continue
        normalized = normalize_visual_fact_pack(pack, artifact=artifact)
        by_path[path] = normalized
    facts = [by_path[path] for path in sorted(by_path)]
    payload = {
        "schema": MEDIA_FACTS_SCHEMA,
        "version": MEDIA_FACTS_VERSION,
        "media_manifest_sha256": _safe_text(
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
    manifest = _require_mapping(manifest, label="media manifest")
    artifacts: dict[str, dict[str, Any]] = {}
    for artifact in _mapping_items(
        manifest.get("artifacts"),
        limit=_MAX_ARTIFACTS,
    ):
        path = _safe_relative_path(artifact.get("path"))
        if not path:
            raise ValueError("media artifact path must be repository-relative")
        if path in artifacts:
            raise ValueError(f"duplicate media artifact path: {path}")
        sha256 = _safe_text(artifact.get("sha256"))
        if not sha256:
            raise ValueError(f"media artifact sha256 is required: {path}")
        artifacts[path] = {**artifact, "path": path, "sha256": sha256}
    return artifacts


def _require_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _mapping_items(value: Any, *, limit: int) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        return ()
    try:
        values = iter(value or ())
    except TypeError:
        return ()
    return (item for item in islice(values, limit) if isinstance(item, Mapping))


def _safe_relative_path(value: Any) -> str:
    text = _safe_text(value)
    if not text or "\\" in text:
        return ""
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return ""
    return path.as_posix()


def _safe_text(value: Any) -> str:
    text = str(value or "").strip()
    text = "".join(
        character
        for character in text
        if ord(character) >= 0x20 and ord(character) != 0x7F
    )
    raw = text.encode("utf-8")
    if len(raw) <= _MAX_TEXT_BYTES:
        return text
    return raw[:_MAX_TEXT_BYTES].decode("utf-8", errors="ignore").rstrip()


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
    "MEDIA_MANIFEST_DIFF_SCHEMA",
    "MEDIA_MANIFEST_DIFF_VERSION",
    "diff_media_manifests",
    "merge_incremental_visual_facts",
    "plan_incremental_visual_fact_update",
]
