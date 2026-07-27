# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from codenib.index.embedding.model_policy import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_REVISION,
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
        revision="immutable-revision",
        trust_remote_code=True,
    )
    disabled = resolve_embedding_load_policy(
        DEFAULT_EMBEDDING_MODEL,
        trust_remote_code=False,
    )

    assert enabled.trust_remote_code is True
    assert disabled.revision == DEFAULT_EMBEDDING_REVISION
    assert disabled.trust_remote_code is False
