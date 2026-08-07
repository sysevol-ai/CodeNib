# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Embedding model defaults and remote-code trust policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_EMBEDDING_MODEL = "nomic-ai/CodeRankEmbed"
DEFAULT_EMBEDDING_DIMENSION = 768
DEFAULT_EMBEDDING_REVISION = "3c4b60807d71f79b43f3c4363786d9493691f8b1"

_IMMUTABLE_REVISION_RE = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True)
class EmbeddingLoadPolicy:
    """Resolved model-loading identity used for build and runtime reuse."""

    revision: Optional[str]
    trust_remote_code: bool


def resolve_embedding_load_policy(
    model: str,
    *,
    revision: Optional[str] = None,
    trust_remote_code: Optional[bool] = None,
) -> EmbeddingLoadPolicy:
    """Resolve a deterministic, least-privilege model-loading policy.

    CodeNib's bundled embedding model requires Hugging Face remote code. The
    default path trusts only the immutable revision audited with this release.
    Other models and caller-supplied revisions remain untrusted unless the
    caller explicitly opts in.
    """

    if revision is not None and not isinstance(revision, str):
        raise TypeError("embedding revision must be a string or None")
    if trust_remote_code is not None and not isinstance(trust_remote_code, bool):
        raise TypeError("trust_remote_code must be a bool or None")

    bundled_revision = (
        DEFAULT_EMBEDDING_REVISION if model == DEFAULT_EMBEDDING_MODEL else None
    )
    resolved_revision = revision if revision is not None else bundled_revision
    uses_bundled_revision = (
        model == DEFAULT_EMBEDDING_MODEL
        and resolved_revision == DEFAULT_EMBEDDING_REVISION
    )
    resolved_trust = (
        uses_bundled_revision if trust_remote_code is None else trust_remote_code
    )
    is_local_model = Path(model).expanduser().is_dir()
    if (
        resolved_trust
        and not is_local_model
        and not (
            resolved_revision and _IMMUTABLE_REVISION_RE.fullmatch(resolved_revision)
        )
    ):
        raise ValueError(
            "trust_remote_code=True for a remote embedding model requires "
            "revision to be a full 40-character lowercase commit SHA"
        )
    return EmbeddingLoadPolicy(
        revision=resolved_revision,
        trust_remote_code=resolved_trust,
    )


__all__ = [
    "DEFAULT_EMBEDDING_DIMENSION",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_EMBEDDING_REVISION",
    "EmbeddingLoadPolicy",
    "resolve_embedding_load_policy",
]
