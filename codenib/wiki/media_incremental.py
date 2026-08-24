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
    compute_visual_facts_manifest_sha256,
    normalize_visual_fact_pack,
)

MEDIA_INCREMENTAL_SCHEMA = "codenib.media-incremental-plan.v1"
MEDIA_INCREMENTAL_VERSION = 1
MEDIA_MANIFEST_DIFF_SCHEMA = "codenib.media-manifest-diff.v1"
MEDIA_MANIFEST_DIFF_VERSION = 1
_MAX_ARTIFACTS = 4096
_MAX_FACT_PACKS = 4096
_MAX_REFERENCES_PER_ARTIFACT = 64
_MAX_TEXT_BYTES = 4096


def diff_media_manifests(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a stable path/hash diff between two media manifests."""

    previous_artifacts = _artifacts_by_path(previous)
    current_artifacts = _artifacts_by_path(current)
    return _diff_artifact_maps(
        previous,
        current,
        previous_artifacts=previous_artifacts,
        current_artifacts=current_artifacts,
    )


def _diff_artifact_maps(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    previous_artifacts: Mapping[str, Mapping[str, Any]],
    current_artifacts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    changes = []
    for path in sorted(set(previous_artifacts) | set(current_artifacts)):
        before = previous_artifacts.get(path)
        after = current_artifacts.get(path)
        previous_sha256 = _safe_text((before or {}).get("sha256"))
        current_sha256 = _safe_text((after or {}).get("sha256"))
        previous_extraction_sha256 = (
            _artifact_extraction_sha256(before) if before is not None else ""
        )
        current_extraction_sha256 = (
            _artifact_extraction_sha256(after) if after is not None else ""
        )
        if before is None:
            status = "added"
        elif after is None:
            status = "removed"
        elif previous_extraction_sha256 == current_extraction_sha256:
            status = "unchanged"
        else:
            status = "changed"
        changes.append(
            {
                "path": path,
                "status": status,
                "previous_sha256": previous_sha256,
                "current_sha256": current_sha256,
                "previous_extraction_sha256": previous_extraction_sha256,
                "current_extraction_sha256": current_extraction_sha256,
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
    *,
    expected_extractor: str | None = None,
) -> dict[str, Any]:
    """Plan reusable facts for the explicitly selected extraction policy.

    Reuse is disabled unless ``expected_extractor`` names the extractor that
    will be used for new work. Callers should change that identifier whenever
    their model or extraction policy changes.
    """

    previous_artifacts = _artifacts_by_path(previous_media_manifest)
    current_artifacts = _artifacts_by_path(current_media_manifest)
    diff = _diff_artifact_maps(
        previous_media_manifest,
        current_media_manifest,
        previous_artifacts=previous_artifacts,
        current_artifacts=current_artifacts,
    )
    previous_visual_facts_manifest = _require_mapping(
        previous_visual_facts_manifest,
        label="previous visual facts manifest",
    )
    if expected_extractor is not None and not isinstance(expected_extractor, str):
        raise ValueError("expected_extractor must be a non-empty string")
    extractor = _safe_text(expected_extractor)
    if expected_extractor is not None and not extractor:
        raise ValueError("expected_extractor must be a non-empty string")
    previous_fact_items = _bounded_fact_items(
        previous_visual_facts_manifest.get("facts")
    )
    previous_facts = (
        _trusted_previous_facts(
            previous_visual_facts_manifest,
            previous_media_manifest_sha256=_safe_text(
                previous_media_manifest.get("manifest_sha256")
            ),
            previous_artifacts=previous_artifacts,
            facts=previous_fact_items,
        )
        if previous_fact_items is not None
        else {}
    )
    reusable_fact_packs = []
    extract_artifact_paths = []
    removed_artifact_paths = []
    for change in diff["changes"]:
        path = change["path"]
        if change["status"] == "unchanged":
            artifact = current_artifacts[path]
            fact = previous_facts.get(path)
            if (
                not extractor
                or fact is None
                or _safe_text(fact.get("extractor")) != extractor
                or _safe_text(fact.get("artifact_sha256"))
                != _safe_text(artifact.get("sha256"))
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
        "expected_extractor": extractor,
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
    payload["manifest_sha256"] = compute_visual_facts_manifest_sha256(
        schema=MEDIA_FACTS_SCHEMA,
        version=MEDIA_FACTS_VERSION,
        media_manifest_sha256=payload["media_manifest_sha256"],
        facts=facts,
    )
    return payload


def _trusted_previous_facts(
    manifest: Mapping[str, Any],
    *,
    previous_media_manifest_sha256: str,
    previous_artifacts: Mapping[str, Mapping[str, Any]],
    facts: list[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    if (
        not previous_media_manifest_sha256
        or manifest.get("schema") != MEDIA_FACTS_SCHEMA
        or type(manifest.get("version")) is not int
        or manifest.get("version") != MEDIA_FACTS_VERSION
        or _safe_text(manifest.get("media_manifest_sha256"))
        != previous_media_manifest_sha256
    ):
        return {}
    try:
        expected_manifest_sha256 = compute_visual_facts_manifest_sha256(
            schema=MEDIA_FACTS_SCHEMA,
            version=MEDIA_FACTS_VERSION,
            media_manifest_sha256=previous_media_manifest_sha256,
            facts=facts,
        )
    except (TypeError, ValueError):
        return {}
    if _safe_text(manifest.get("manifest_sha256")) != expected_manifest_sha256:
        return {}

    trusted: dict[str, dict[str, Any]] = {}
    for fact in facts:
        path = _safe_relative_path(fact.get("artifact_path"))
        artifact = previous_artifacts.get(path)
        if not path or path in trusted or artifact is None:
            return {}
        try:
            normalized = normalize_visual_fact_pack(fact, artifact=artifact)
        except ValueError:
            return {}
        if _safe_text(fact.get("fact_pack_sha256")) != normalized.get(
            "fact_pack_sha256"
        ):
            return {}
        trusted[path] = normalized
    return trusted


def _bounded_fact_items(value: Any) -> list[Mapping[str, Any]] | None:
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        return None
    try:
        items = list(islice(iter(value), _MAX_FACT_PACKS + 1))
    except TypeError:
        return None
    if len(items) > _MAX_FACT_PACKS or any(
        not isinstance(item, Mapping) for item in items
    ):
        return None
    return items


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


def _artifact_extraction_sha256(artifact: Mapping[str, Any]) -> str:
    references = [
        {
            "markdown_path": _safe_text(reference.get("markdown_path")),
            "line": (
                reference.get("line") if type(reference.get("line")) is int else 0
            ),
            "alt_text": _safe_text(reference.get("alt_text")),
            "title": _safe_text(reference.get("title")),
            "surrounding_text": _safe_text(reference.get("surrounding_text")),
        }
        for reference in _mapping_items(
            artifact.get("references"),
            limit=_MAX_REFERENCES_PER_ARTIFACT,
        )
    ]
    return _sha256_json(
        {
            "path": _safe_relative_path(artifact.get("path")),
            "sha256": _safe_text(artifact.get("sha256")),
            "media_type": _safe_text(artifact.get("media_type")),
            "mime_type": _safe_text(artifact.get("mime_type")),
            "role_hint": _safe_text(artifact.get("role_hint")),
            "caption": _safe_text(artifact.get("caption")),
            "surrounding_text": _safe_text(artifact.get("surrounding_text")),
            "references": references,
        }
    )


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
