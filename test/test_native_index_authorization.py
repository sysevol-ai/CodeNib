# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pickle
from pathlib import Path

import pytest

from codenib._atomic_directory import capture_directory_ownership
from codenib.native_index_authorization import (
    NATIVE_INDEX_SUBJECT_SCHEMA,
    mint_trusted_local_authorization,
    native_index_subject,
    require_native_index_authorization,
)


def test_native_index_subject_commits_tree_files_and_semantics(tmp_path: Path) -> None:
    root = tmp_path / "vector"
    root.mkdir()
    (root / "index.faiss").write_bytes(b"native")
    ownership = capture_directory_ownership(root)

    subject = native_index_subject(
        ownership,
        view_type="vector",
        semantic_contract={"dimension": 3, "index_metric": "ip"},
    )

    assert NATIVE_INDEX_SUBJECT_SCHEMA.encode("ascii") in subject.canonical_bytes()
    assert subject.files[0][0] == "index.faiss"
    assert len(subject.digest) == 64


def test_native_index_authorization_is_process_local_and_exact(tmp_path: Path) -> None:
    root = tmp_path / "vector"
    root.mkdir()
    payload = root / "index.faiss"
    payload.write_bytes(b"native")
    ownership = capture_directory_ownership(root)
    semantic = {"dimension": 3, "index_metric": "ip"}
    authorization = mint_trusted_local_authorization(
        ownership,
        view_type="vector",
        semantic_contract=semantic,
        evidence=("compiler-lock-and-checkout",),
    )

    require_native_index_authorization(
        authorization,
        ownership,
        view_type="vector",
        semantic_contract=semantic,
    )
    with pytest.raises(TypeError, match="process-local"):
        pickle.dumps(authorization)

    payload.write_bytes(b"changed")
    changed = capture_directory_ownership(root)
    with pytest.raises(ValueError, match="does not match captured bytes"):
        require_native_index_authorization(
            authorization,
            changed,
            view_type="vector",
            semantic_contract=semantic,
        )


def test_native_index_authorization_defaults_to_deny(tmp_path: Path) -> None:
    root = tmp_path / "vector"
    root.mkdir()
    (root / "index.faiss").write_bytes(b"native")
    ownership = capture_directory_ownership(root)

    with pytest.raises(ValueError, match="requires external authorization"):
        require_native_index_authorization(
            None,
            ownership,
            view_type="vector",
            semantic_contract={"dimension": 3},
        )
