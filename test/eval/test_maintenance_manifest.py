# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy

import pytest

from codeminer.eval.maintenance_manifest import (
    build_manifest,
    load_manifest,
    manifest_id,
    validate_manifest,
    write_manifest,
)


def _chain():
    return [
        {
            "instance_id": "owner__repo-1",
            "language": "python",
            "step": 1,
            "from_commit": "base",
            "to_commit": "target-1",
            "delta_lines": 12,
            "delta_files": 2,
            "delta_lines_test_excluded": 10,
            "delta_files_test_excluded": 1,
            "delta_symbols": 3,
            "delta_symbols_test_excluded": 2,
        },
        {
            "instance_id": "owner__repo-1",
            "language": "python",
            "step": 2,
            "from_commit": "target-1",
            "to_commit": "target-2",
            "delta_lines": 4,
            "delta_files": 1,
            "delta_lines_test_excluded": 4,
            "delta_files_test_excluded": 1,
            "delta_symbols": 1,
            "delta_symbols_test_excluded": 1,
        },
    ]


def _manifest():
    instances = [("owner__repo-1", "python", "base")]
    return build_manifest(
        instances,
        {"owner__repo-1": _chain()},
        selection={"method": "first-parent"},
    )


def test_manifest_roundtrip_has_stable_identity(tmp_path):
    payload = _manifest()
    path = tmp_path / "transitions.json"

    written_id = write_manifest(path, payload)
    instances, chains, loaded_id = load_manifest(path)

    assert written_id == loaded_id == manifest_id(payload)
    assert instances == [("owner__repo-1", "python", "base")]
    assert chains == {"owner__repo-1": _chain()}


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda payload: payload.update(schema_version=99), "unsupported"),
        (
            lambda payload: payload["instances"][0]["transitions"][1].update(step=3),
            "non-contiguous",
        ),
        (
            lambda payload: payload["instances"][0]["transitions"][1].update(
                from_commit="other"
            ),
            "does not continue",
        ),
    ],
)
def test_manifest_rejects_protocol_drift(mutate, match):
    payload = copy.deepcopy(_manifest())
    mutate(payload)
    with pytest.raises(ValueError, match=match):
        validate_manifest(payload)
