# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Stable storage helpers for multimodal repository knowledge bundles."""

from __future__ import annotations

import json
import os
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

MULTIMODAL_KNOWLEDGE_BUNDLE_SCHEMA = "codenib.multimodal-knowledge-bundle.v1"
MULTIMODAL_KNOWLEDGE_BUNDLE_VERSION = 1
_MAX_BUNDLE_BYTES = 128 * 1024 * 1024


def build_multimodal_knowledge_bundle(
    *,
    media_manifest: Mapping[str, Any],
    visual_facts_manifest: Mapping[str, Any],
    source_candidate_count: int,
    grounding_manifest: Mapping[str, Any],
    knowledge_view: Mapping[str, Any],
) -> dict[str, Any]:
    """Wrap multimodal pipeline outputs in a versioned, hashable bundle."""

    bundle: dict[str, Any] = {
        "schema": MULTIMODAL_KNOWLEDGE_BUNDLE_SCHEMA,
        "schema_version": MULTIMODAL_KNOWLEDGE_BUNDLE_VERSION,
        "media_manifest": dict(media_manifest),
        "visual_facts_manifest": dict(visual_facts_manifest),
        "source_candidate_count": int(source_candidate_count),
        "grounding_manifest": dict(grounding_manifest),
        "knowledge_view": dict(knowledge_view),
        "component_sha256": {
            "media_manifest": str(media_manifest.get("manifest_sha256") or ""),
            "visual_facts_manifest": str(
                visual_facts_manifest.get("manifest_sha256") or ""
            ),
            "grounding_manifest": str(grounding_manifest.get("manifest_sha256") or ""),
            "knowledge_view": str(knowledge_view.get("view_sha256") or ""),
        },
    }
    bundle["bundle_sha256"] = _stable_sha256(
        {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    )
    return bundle


def save_multimodal_knowledge_bundle(
    bundle: Mapping[str, Any],
    path: str | Path,
) -> None:
    """Atomically write a multimodal knowledge bundle as stable JSON."""

    validated = validate_multimodal_knowledge_bundle(bundle)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(validated, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def load_multimodal_knowledge_bundle(path: str | Path) -> dict[str, Any]:
    """Load and validate a persisted multimodal knowledge bundle."""

    source = Path(path)
    size = source.stat().st_size
    if size > _MAX_BUNDLE_BYTES:
        raise ValueError("multimodal knowledge bundle exceeds the byte limit")
    with source.open("rb") as handle:
        raw = handle.read(_MAX_BUNDLE_BYTES + 1)
    if len(raw) > _MAX_BUNDLE_BYTES:
        raise ValueError("multimodal knowledge bundle exceeds the byte limit")
    data = json.loads(raw.decode("utf-8"))
    return validate_multimodal_knowledge_bundle(data)


def validate_multimodal_knowledge_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalized bundle or raise ``ValueError`` for invalid input."""

    if not isinstance(bundle, Mapping):
        raise ValueError("multimodal knowledge bundle must be an object")
    data = dict(bundle)
    if data.get("schema") != MULTIMODAL_KNOWLEDGE_BUNDLE_SCHEMA:
        raise ValueError("multimodal knowledge bundle schema is unsupported")
    if data.get("schema_version") != MULTIMODAL_KNOWLEDGE_BUNDLE_VERSION:
        raise ValueError("multimodal knowledge bundle version is unsupported")
    for key in (
        "media_manifest",
        "visual_facts_manifest",
        "grounding_manifest",
        "knowledge_view",
        "component_sha256",
    ):
        if not isinstance(data.get(key), Mapping):
            raise ValueError(f"multimodal knowledge bundle field {key!r} is invalid")
        data[key] = dict(data[key])
    source_candidate_count = data.get("source_candidate_count")
    if isinstance(source_candidate_count, bool) or not isinstance(
        source_candidate_count, int
    ):
        raise ValueError(
            "multimodal knowledge bundle source_candidate_count is invalid"
        )
    if source_candidate_count < 0:
        raise ValueError(
            "multimodal knowledge bundle source_candidate_count is invalid"
        )
    expected_hash = _stable_sha256(
        {key: value for key, value in data.items() if key != "bundle_sha256"}
    )
    recorded_hash = data.get("bundle_sha256")
    if recorded_hash:
        if recorded_hash != expected_hash:
            raise ValueError("multimodal knowledge bundle hash does not match")
    else:
        data["bundle_sha256"] = expected_hash
    return data


def _stable_sha256(payload: Mapping[str, Any]) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "MULTIMODAL_KNOWLEDGE_BUNDLE_SCHEMA",
    "MULTIMODAL_KNOWLEDGE_BUNDLE_VERSION",
    "build_multimodal_knowledge_bundle",
    "load_multimodal_knowledge_bundle",
    "save_multimodal_knowledge_bundle",
    "validate_multimodal_knowledge_bundle",
]
