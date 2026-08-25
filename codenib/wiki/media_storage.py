# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Stable storage helpers for multimodal repository knowledge bundles."""

from __future__ import annotations

import copy
import hmac
import io
import json
import math
import os
import re
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from .._bounded_json import validate_bounded_json_stream, validate_json_complexity
from ._safe_file_reads import read_regular_bytes
from .media_artifacts import MEDIA_MANIFEST_SCHEMA, MEDIA_MANIFEST_VERSION
from .media_facts import (
    MEDIA_FACTS_SCHEMA,
    MEDIA_FACTS_VERSION,
    compute_visual_facts_manifest_sha256,
)
from .media_grounding import MEDIA_GROUNDING_SCHEMA, MEDIA_GROUNDING_VERSION
from .media_knowledge import MULTIMODAL_KNOWLEDGE_SCHEMA, MULTIMODAL_KNOWLEDGE_VERSION

MULTIMODAL_KNOWLEDGE_BUNDLE_SCHEMA = "codenib.multimodal-knowledge-bundle.v1"
MULTIMODAL_KNOWLEDGE_BUNDLE_VERSION = 1
_MAX_BUNDLE_BYTES = 128 * 1024 * 1024
_MAX_BUNDLE_NODES = 2_000_000
_MAX_BUNDLE_TOKENS = 4_000_000
_MAX_COMPONENT_ITEMS = 32_768
_MAX_SOURCE_CANDIDATES = 8_192
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SOURCE_SELECTION_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


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
        "source_candidate_count": source_candidate_count,
        "grounding_manifest": dict(grounding_manifest),
        "knowledge_view": dict(knowledge_view),
        "component_sha256": {
            "media_manifest": media_manifest.get("manifest_sha256"),
            "visual_facts_manifest": visual_facts_manifest.get("manifest_sha256"),
            "grounding_manifest": grounding_manifest.get("manifest_sha256"),
            "knowledge_view": knowledge_view.get("view_sha256"),
        },
    }
    bundle["bundle_sha256"] = _stable_sha256(
        {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    )
    return validate_multimodal_knowledge_bundle(bundle)


def save_multimodal_knowledge_bundle(
    bundle: Mapping[str, Any],
    path: str | Path,
) -> None:
    """Atomically write a multimodal knowledge bundle as stable JSON."""

    validated = validate_multimodal_knowledge_bundle(bundle)
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            validated,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    if len(payload) > _MAX_BUNDLE_BYTES:
        raise ValueError("multimodal knowledge bundle exceeds the byte limit")
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
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

    source = Path(path).expanduser()
    raw = read_regular_bytes(source, max_bytes=_MAX_BUNDLE_BYTES)
    if raw is None:
        raise ValueError(
            "multimodal knowledge bundle must be a stable regular file "
            "within the byte limit"
        )
    validate_bounded_json_stream(
        io.BytesIO(raw),
        label="multimodal knowledge bundle",
        max_bytes=_MAX_BUNDLE_BYTES,
        max_nodes=_MAX_BUNDLE_NODES,
        max_lexical_tokens=_MAX_BUNDLE_TOKENS,
    )
    try:
        data = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_nonfinite_number,
            parse_float=_finite_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("multimodal knowledge bundle contains invalid JSON") from exc
    return validate_multimodal_knowledge_bundle(data)


def validate_multimodal_knowledge_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalized bundle or raise ``ValueError`` for invalid input."""

    if not isinstance(bundle, Mapping):
        raise ValueError("multimodal knowledge bundle must be an object")
    data = dict(bundle)
    if data.get("schema") != MULTIMODAL_KNOWLEDGE_BUNDLE_SCHEMA:
        raise ValueError("multimodal knowledge bundle schema is unsupported")
    if (
        type(data.get("schema_version")) is not int
        or data["schema_version"] != MULTIMODAL_KNOWLEDGE_BUNDLE_VERSION
    ):
        raise ValueError("multimodal knowledge bundle version is unsupported")
    media_manifest = _mapping(data.get("media_manifest"), label="media_manifest")
    visual_facts_manifest = _mapping(
        data.get("visual_facts_manifest"),
        label="visual_facts_manifest",
    )
    grounding_manifest = _mapping(
        data.get("grounding_manifest"),
        label="grounding_manifest",
    )
    knowledge_view = _mapping(data.get("knowledge_view"), label="knowledge_view")
    component_sha256 = _mapping(
        data.get("component_sha256"),
        label="component_sha256",
    )
    data.update(
        {
            "media_manifest": media_manifest,
            "visual_facts_manifest": visual_facts_manifest,
            "grounding_manifest": grounding_manifest,
            "knowledge_view": knowledge_view,
            "component_sha256": component_sha256,
        }
    )
    source_candidate_count = data.get("source_candidate_count")
    if (
        type(source_candidate_count) is not int
        or not 0 <= source_candidate_count <= _MAX_SOURCE_CANDIDATES
    ):
        raise ValueError(
            "multimodal knowledge bundle source_candidate_count is invalid"
        )

    component_digests = {
        "media_manifest": _validate_media_manifest(media_manifest),
        "visual_facts_manifest": _validate_visual_facts_manifest(
            visual_facts_manifest,
        ),
        "grounding_manifest": _validate_grounding_manifest(grounding_manifest),
        "knowledge_view": _validate_knowledge_view(knowledge_view),
    }
    if (
        visual_facts_manifest["media_manifest_sha256"]
        != component_digests["media_manifest"]
    ):
        raise ValueError("visual facts manifest is bound to another media manifest")
    if (
        grounding_manifest["visual_facts_manifest_sha256"]
        != component_digests["visual_facts_manifest"]
    ):
        raise ValueError("grounding manifest is bound to another visual facts manifest")
    linked_view_digests = {
        "media_manifest": knowledge_view.get("media_manifest_sha256"),
        "visual_facts_manifest": knowledge_view.get("visual_facts_manifest_sha256"),
        "grounding_manifest": knowledge_view.get("grounding_manifest_sha256"),
    }
    if any(
        linked_view_digests[key] != component_digests[key]
        for key in linked_view_digests
    ):
        raise ValueError("multimodal knowledge view component binding does not match")
    if set(component_sha256) != set(component_digests):
        raise ValueError("multimodal knowledge component hash inventory is invalid")
    for key, digest in component_sha256.items():
        if type(key) is not str or not key:
            raise ValueError("multimodal knowledge component hash key is invalid")
        _digest(digest, label=f"component_sha256.{key}")
    if any(
        component_sha256.get(key) != digest for key, digest in component_digests.items()
    ):
        raise ValueError("multimodal knowledge component hash does not match")

    expected_hash = _stable_sha256(
        {key: value for key, value in data.items() if key != "bundle_sha256"}
    )
    recorded_hash = _digest(data.get("bundle_sha256"), label="bundle_sha256")
    if not hmac.compare_digest(recorded_hash, expected_hash):
        raise ValueError("multimodal knowledge bundle hash does not match")

    normalized = copy.deepcopy(data)
    validate_json_complexity(
        normalized,
        label="multimodal knowledge bundle",
        max_nodes=_MAX_BUNDLE_NODES,
    )
    if len(_canonical_json_bytes(normalized)) > _MAX_BUNDLE_BYTES:
        raise ValueError("multimodal knowledge bundle exceeds the byte limit")
    return normalized


def _validate_media_manifest(manifest: Mapping[str, Any]) -> str:
    if (
        manifest.get("schema") != MEDIA_MANIFEST_SCHEMA
        or type(manifest.get("version")) is not int
        or manifest["version"] != MEDIA_MANIFEST_VERSION
    ):
        raise ValueError("media manifest schema is unsupported")
    artifacts = _mapping_list(
        manifest.get("artifacts"),
        label="media_manifest.artifacts",
    )
    _matching_count(
        manifest.get("artifact_count"),
        artifacts,
        label="media_manifest.artifact_count",
    )
    metadata = _mapping(manifest.get("metadata"), label="media_manifest.metadata")
    expected = _component_sha256(
        {
            "schema": manifest["schema"],
            "version": manifest["version"],
            "commit": _text(manifest.get("commit"), label="media_manifest.commit"),
            "source_selection_digest": _source_selection_digest(
                manifest.get("source_selection_digest"),
                label="media_manifest.source_selection_digest",
            ),
            "artifacts": artifacts,
            "metadata": metadata,
        }
    )
    recorded = _digest(
        manifest.get("manifest_sha256"),
        label="media_manifest.manifest_sha256",
    )
    if not hmac.compare_digest(recorded, expected):
        raise ValueError("media manifest hash does not match")
    return recorded


def _validate_visual_facts_manifest(manifest: Mapping[str, Any]) -> str:
    if (
        manifest.get("schema") != MEDIA_FACTS_SCHEMA
        or type(manifest.get("version")) is not int
        or manifest["version"] != MEDIA_FACTS_VERSION
    ):
        raise ValueError("visual facts manifest schema is unsupported")
    facts = _mapping_list(
        manifest.get("facts"),
        label="visual_facts_manifest.facts",
    )
    _matching_count(
        manifest.get("fact_count"),
        facts,
        label="visual_facts_manifest.fact_count",
    )
    media_digest = _digest(
        manifest.get("media_manifest_sha256"),
        label="visual_facts_manifest.media_manifest_sha256",
    )
    expected = compute_visual_facts_manifest_sha256(
        schema=manifest["schema"],
        version=manifest["version"],
        media_manifest_sha256=media_digest,
        facts=facts,
    )
    recorded = _digest(
        manifest.get("manifest_sha256"),
        label="visual_facts_manifest.manifest_sha256",
    )
    if not hmac.compare_digest(recorded, expected):
        raise ValueError("visual facts manifest hash does not match")
    return recorded


def _validate_grounding_manifest(manifest: Mapping[str, Any]) -> str:
    if (
        manifest.get("schema") != MEDIA_GROUNDING_SCHEMA
        or type(manifest.get("version")) is not int
        or manifest["version"] != MEDIA_GROUNDING_VERSION
    ):
        raise ValueError("grounding manifest schema is unsupported")
    bindings = _mapping_list(
        manifest.get("bindings"),
        label="grounding_manifest.bindings",
    )
    _matching_count(
        manifest.get("binding_count"),
        bindings,
        label="grounding_manifest.binding_count",
    )
    facts_digest = _digest(
        manifest.get("visual_facts_manifest_sha256"),
        label="grounding_manifest.visual_facts_manifest_sha256",
    )
    expected = _component_sha256(
        {
            "schema": manifest["schema"],
            "version": manifest["version"],
            "visual_facts_manifest_sha256": facts_digest,
            "bindings": bindings,
        }
    )
    recorded = _digest(
        manifest.get("manifest_sha256"),
        label="grounding_manifest.manifest_sha256",
    )
    if not hmac.compare_digest(recorded, expected):
        raise ValueError("grounding manifest hash does not match")
    return recorded


def _validate_knowledge_view(view: Mapping[str, Any]) -> str:
    if (
        view.get("schema") != MULTIMODAL_KNOWLEDGE_SCHEMA
        or type(view.get("version")) is not int
        or view["version"] != MULTIMODAL_KNOWLEDGE_VERSION
    ):
        raise ValueError("multimodal knowledge view schema is unsupported")
    entries = _mapping_list(
        view.get("entries"),
        label="knowledge_view.entries",
    )
    _matching_count(
        view.get("entry_count"),
        entries,
        label="knowledge_view.entry_count",
    )
    for key in (
        "media_manifest_sha256",
        "visual_facts_manifest_sha256",
        "grounding_manifest_sha256",
    ):
        _digest(view.get(key), label=f"knowledge_view.{key}")
    expected = _component_sha256(
        {key: value for key, value in view.items() if key != "view_sha256"}
    )
    recorded = _digest(view.get("view_sha256"), label="knowledge_view.view_sha256")
    if not hmac.compare_digest(recorded, expected):
        raise ValueError("multimodal knowledge view hash does not match")
    return recorded


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"multimodal knowledge bundle field {label!r} is invalid")
    return dict(value)


def _mapping_list(value: Any, *, label: str) -> list[dict[str, Any]]:
    if (
        not isinstance(value, list)
        or len(value) > _MAX_COMPONENT_ITEMS
        or any(not isinstance(item, Mapping) for item in value)
    ):
        raise ValueError(f"multimodal knowledge bundle field {label!r} is invalid")
    return [dict(item) for item in value]


def _matching_count(value: Any, items: list[Any], *, label: str) -> None:
    if type(value) is not int or value != len(items):
        raise ValueError(f"multimodal knowledge bundle field {label!r} is invalid")


def _text(value: Any, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"multimodal knowledge bundle field {label!r} is invalid")
    return value


def _digest(value: Any, *, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"multimodal knowledge bundle field {label!r} is invalid")
    return value


def _source_selection_digest(value: Any, *, label: str) -> str:
    if type(value) is not str or _SOURCE_SELECTION_DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"multimodal knowledge bundle field {label!r} is invalid")
    return value


def _stable_sha256(payload: Mapping[str, Any]) -> str:
    return sha256(_canonical_json_bytes(payload)).hexdigest()


def _component_sha256(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise ValueError(
            "multimodal knowledge component must contain bounded JSON values"
        ) from exc
    return sha256(encoded).hexdigest()


def _canonical_json_bytes(payload: Any) -> bytes:
    try:
        return json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise ValueError(
            "multimodal knowledge bundle must contain bounded JSON values"
        ) from exc


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_number(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


__all__ = [
    "MULTIMODAL_KNOWLEDGE_BUNDLE_SCHEMA",
    "MULTIMODAL_KNOWLEDGE_BUNDLE_VERSION",
    "build_multimodal_knowledge_bundle",
    "load_multimodal_knowledge_bundle",
    "save_multimodal_knowledge_bundle",
    "validate_multimodal_knowledge_bundle",
]
