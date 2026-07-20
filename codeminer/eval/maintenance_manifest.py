# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Versioned commit-transition manifests for maintenance experiments."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

SCHEMA_VERSION = 1

_TRANSITION_FIELDS = (
    "instance_id",
    "language",
    "step",
    "from_commit",
    "to_commit",
    "delta_lines",
    "delta_files",
    "delta_lines_test_excluded",
    "delta_files_test_excluded",
    "delta_symbols",
    "delta_symbols_test_excluded",
)


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def manifest_id(payload: Mapping[str, Any]) -> str:
    """Return the stable SHA-256 identity of a manifest payload."""
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def build_manifest(
    instances: Sequence[Tuple[str, str, str]],
    chains: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    selection: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build and validate a serializable maintenance manifest."""
    entries: List[Dict[str, Any]] = []
    for instance_id, language, base_commit in instances:
        transitions = [dict(row) for row in chains.get(instance_id, [])]
        entries.append(
            {
                "instance_id": instance_id,
                "language": language,
                "base_commit": base_commit,
                "transitions": transitions,
            }
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "selection": dict(selection),
        "instances": entries,
    }
    validate_manifest(payload)
    return payload


def validate_manifest(payload: Mapping[str, Any]) -> None:
    """Reject malformed, discontinuous, or ambiguous transition chains."""
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            "unsupported maintenance manifest schema: "
            f"{payload.get('schema_version')!r}"
        )
    entries = payload.get("instances")
    if not isinstance(entries, list) or not entries:
        raise ValueError("maintenance manifest must contain at least one instance")

    seen_instances = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("manifest instance entries must be objects")
        instance_id = entry.get("instance_id")
        language = entry.get("language")
        base_commit = entry.get("base_commit")
        if not all(
            isinstance(value, str) and value
            for value in (instance_id, language, base_commit)
        ):
            raise ValueError(
                "instance_id, language, and base_commit must be nonempty strings"
            )
        if instance_id in seen_instances:
            raise ValueError(f"duplicate manifest instance: {instance_id}")
        seen_instances.add(instance_id)

        transitions = entry.get("transitions")
        if not isinstance(transitions, list) or not transitions:
            raise ValueError(f"manifest instance {instance_id} has no transitions")
        expected_from = base_commit
        for expected_step, transition in enumerate(transitions, start=1):
            if not isinstance(transition, dict):
                raise ValueError(f"{instance_id} step {expected_step} is not an object")
            missing = [field for field in _TRANSITION_FIELDS if field not in transition]
            if missing:
                raise ValueError(
                    f"{instance_id} step {expected_step} missing fields: {missing}"
                )
            if transition["instance_id"] != instance_id:
                raise ValueError(
                    f"{instance_id} step {expected_step} changes instance_id"
                )
            if transition["language"] != language:
                raise ValueError(f"{instance_id} step {expected_step} changes language")
            if transition["step"] != expected_step:
                raise ValueError(
                    f"{instance_id} has non-contiguous step {transition['step']!r}; "
                    f"expected {expected_step}"
                )
            if transition["from_commit"] != expected_from:
                raise ValueError(
                    f"{instance_id} step {expected_step} does not continue the chain"
                )
            to_commit = transition["to_commit"]
            if not isinstance(to_commit, str) or not to_commit:
                raise ValueError(
                    f"{instance_id} step {expected_step} has no target commit"
                )
            expected_from = to_commit


def write_manifest(path: Path, payload: Mapping[str, Any]) -> str:
    """Validate and write a manifest, returning its stable identity."""
    validate_manifest(payload)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return manifest_id(payload)


def load_manifest(
    path: Path,
) -> Tuple[List[Tuple[str, str, str]], Dict[str, List[Dict[str, Any]]], str]:
    """Load a manifest as instance tuples, per-instance chains, and identity."""
    payload = json.loads(Path(path).read_text())
    validate_manifest(payload)
    instances = []
    chains: Dict[str, List[Dict[str, Any]]] = {}
    for entry in payload["instances"]:
        instance_id = entry["instance_id"]
        instances.append((instance_id, entry["language"], entry["base_commit"]))
        chains[instance_id] = [dict(row) for row in entry["transitions"]]
    return instances, chains, manifest_id(payload)
