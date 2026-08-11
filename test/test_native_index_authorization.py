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
    InvalidNativeIndexAuthorizationError,
    MissingNativeIndexAuthorizationError,
    NativeIndexAuthorizationError,
    _mint_trusted_local_admin_authorization,
    native_index_subject,
    require_native_index_authorization,
    require_native_index_authorization_preflight,
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
    authorization = _mint_trusted_local_admin_authorization(
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
    with pytest.raises(
        InvalidNativeIndexAuthorizationError,
        match="does not match captured bytes",
    ):
        require_native_index_authorization(
            authorization,
            changed,
            view_type="vector",
            semantic_contract=semantic,
        )


def test_native_index_authorization_is_immutable_and_pid_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.native_index_authorization as authorization_module

    root = tmp_path / "vector"
    root.mkdir()
    (root / "index.faiss").write_bytes(b"native")
    ownership = capture_directory_ownership(root)
    semantic = {"dimension": 3}
    authorization = _mint_trusted_local_admin_authorization(
        ownership,
        view_type="vector",
        semantic_contract=semantic,
        evidence=("verified-local-boundary",),
    )

    with pytest.raises(AttributeError, match="immutable"):
        authorization.subject_digest = "0" * 64

    monkeypatch.setattr(
        authorization_module.os,
        "getpid",
        lambda: authorization.process_id + 1,
    )
    with pytest.raises(InvalidNativeIndexAuthorizationError, match="another process"):
        require_native_index_authorization(
            authorization,
            ownership,
            view_type="vector",
            semantic_contract=semantic,
        )


def test_trusted_local_issuer_requires_auditable_evidence(tmp_path: Path) -> None:
    root = tmp_path / "vector"
    root.mkdir()
    (root / "index.faiss").write_bytes(b"native")
    ownership = capture_directory_ownership(root)

    with pytest.raises(ValueError, match="evidence"):
        _mint_trusted_local_admin_authorization(
            ownership,
            view_type="vector",
            semantic_contract={"dimension": 3},
            evidence=(),
        )


def test_native_index_authorization_defaults_to_deny(tmp_path: Path) -> None:
    root = tmp_path / "vector"
    root.mkdir()
    (root / "index.faiss").write_bytes(b"native")
    ownership = capture_directory_ownership(root)

    with pytest.raises(
        MissingNativeIndexAuthorizationError,
        match="requires external authorization",
    ):
        require_native_index_authorization(
            None,
            ownership,
            view_type="vector",
            semantic_contract={"dimension": 3},
        )


def test_native_index_authorization_errors_remain_value_errors() -> None:
    assert issubclass(NativeIndexAuthorizationError, ValueError)
    assert issubclass(MissingNativeIndexAuthorizationError, ValueError)
    assert issubclass(InvalidNativeIndexAuthorizationError, ValueError)

    import codenib.native_index_authorization as authorization_module

    assert {
        "NativeIndexAuthorizationError",
        "MissingNativeIndexAuthorizationError",
        "InvalidNativeIndexAuthorizationError",
    } <= set(authorization_module.__all__)


def test_explicit_malformed_authorization_is_not_missing() -> None:
    with pytest.raises(
        InvalidNativeIndexAuthorizationError,
        match="malformed",
    ):
        require_native_index_authorization_preflight(object(), view_type="vector")


def test_wrong_semantic_authorization_is_invalid(tmp_path: Path) -> None:
    root = tmp_path / "vector"
    root.mkdir()
    (root / "index.faiss").write_bytes(b"native")
    ownership = capture_directory_ownership(root)
    authorization = _mint_trusted_local_admin_authorization(
        ownership,
        view_type="vector",
        semantic_contract={"dimension": 3},
        evidence=("verified-local-boundary",),
    )

    with pytest.raises(
        InvalidNativeIndexAuthorizationError,
        match="does not match captured bytes",
    ):
        require_native_index_authorization(
            authorization,
            ownership,
            view_type="vector",
            semantic_contract={"dimension": 4},
        )
