# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest

from codenib.index.embedding.model_policy import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_REVISION,
    resolve_embedding_artifact_load_policy,
    resolve_embedding_load_policy,
)


def test_bundled_model_uses_pinned_trusted_revision_by_default():
    policy = resolve_embedding_load_policy(DEFAULT_EMBEDDING_MODEL)

    assert policy.revision == DEFAULT_EMBEDDING_REVISION
    assert policy.trust_remote_code is True


def test_arbitrary_model_remains_untrusted_by_default():
    policy = resolve_embedding_load_policy("vendor/custom-embedding")

    assert policy.revision is None
    assert policy.trust_remote_code is False


def test_custom_revision_of_bundled_model_is_not_implicitly_trusted():
    policy = resolve_embedding_load_policy(
        DEFAULT_EMBEDDING_MODEL,
        revision="caller-controlled-revision",
    )

    assert policy.revision == "caller-controlled-revision"
    assert policy.trust_remote_code is False


def test_caller_can_explicitly_override_remote_code_policy():
    enabled = resolve_embedding_load_policy(
        "vendor/custom-embedding",
        revision="a" * 40,
        trust_remote_code=True,
    )
    disabled = resolve_embedding_load_policy(
        DEFAULT_EMBEDDING_MODEL,
        trust_remote_code=False,
    )

    assert enabled.trust_remote_code is True
    assert disabled.revision == DEFAULT_EMBEDDING_REVISION
    assert disabled.trust_remote_code is False


@pytest.mark.parametrize(
    "revision",
    [None, "main", "a" * 39, "g" * 40],
)
def test_remote_code_requires_full_immutable_revision(revision):
    with pytest.raises(ValueError, match="full 40-character lowercase commit SHA"):
        resolve_embedding_load_policy(
            "vendor/custom-embedding",
            revision=revision,
            trust_remote_code=True,
        )


def test_local_model_can_trust_local_code_without_revision(tmp_path):
    model_path = tmp_path / "model"
    model_path.mkdir()

    policy = resolve_embedding_load_policy(
        str(model_path),
        trust_remote_code=True,
    )

    assert policy.revision is None
    assert policy.trust_remote_code is True


def test_local_path_named_like_bundled_model_is_not_implicitly_trusted(
    tmp_path, monkeypatch
):
    model_path = tmp_path / DEFAULT_EMBEDDING_MODEL
    model_path.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    policy = resolve_embedding_load_policy(DEFAULT_EMBEDDING_MODEL)

    assert policy.revision is None
    assert policy.trust_remote_code is False


def test_artifact_policy_treats_model_id_as_remote_without_filesystem_probe(
    tmp_path, monkeypatch
):
    local_collision = tmp_path / "vendor" / "custom-embedding"
    local_collision.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    def fail_is_dir(_path: Path) -> bool:
        pytest.fail("artifact embedding policy probed the ambient filesystem")

    monkeypatch.setattr(Path, "is_dir", fail_is_dir)

    policy = resolve_embedding_artifact_load_policy("vendor/custom-embedding")

    assert policy.revision is None
    assert policy.trust_remote_code is False


def test_artifact_policy_keeps_bundled_remote_identity_deterministic(
    tmp_path, monkeypatch
):
    local_collision = tmp_path / DEFAULT_EMBEDDING_MODEL
    local_collision.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    def fail_is_dir(_path: Path) -> bool:
        pytest.fail("artifact embedding policy probed the ambient filesystem")

    monkeypatch.setattr(Path, "is_dir", fail_is_dir)

    policy = resolve_embedding_artifact_load_policy(DEFAULT_EMBEDDING_MODEL)

    assert policy.revision == DEFAULT_EMBEDDING_REVISION
    assert policy.trust_remote_code is True


@pytest.mark.parametrize(
    "model",
    [
        "/opt/models/embedding",
        "C:/models/embedding",
        "C:\\models\\embedding",
        "C:embedding",
        "//server/share/embedding",
        "\\\\server\\share\\embedding",
        "./embedding",
        "../embedding",
        "vendor/../embedding",
        "~/embedding",
        "~other/embedding",
        "vendor\\embedding",
        "vendor//embedding",
        "file:/opt/models/embedding",
        "FILE:relative-model",
        "vendor/model name",
        "vendor/model\x7f",
    ],
)
def test_artifact_policy_rejects_filesystem_shaped_models(model):
    with pytest.raises(ValueError, match="filesystem-shaped path"):
        resolve_embedding_artifact_load_policy(model)


@pytest.mark.parametrize("value", [1, "false"])
def test_remote_code_option_requires_boolean(value):
    with pytest.raises(TypeError, match="must be a bool or None"):
        resolve_embedding_load_policy(
            "vendor/custom-embedding",
            trust_remote_code=value,
        )
