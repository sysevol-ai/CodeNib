# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Embedding model defaults and remote-code trust policy."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

DEFAULT_EMBEDDING_MODEL = "nomic-ai/CodeRankEmbed"
DEFAULT_EMBEDDING_DIMENSION = 768
DEFAULT_EMBEDDING_REVISION = "3c4b60807d71f79b43f3c4363786d9493691f8b1"

_IMMUTABLE_REVISION_RE = re.compile(r"[0-9a-f]{40}\Z")
_WINDOWS_DRIVE_RE = re.compile(r"[A-Za-z]:")
_UNSET = object()


@dataclass(frozen=True)
class EmbeddingLoadPolicy:
    """Resolved model-loading identity used for build and runtime reuse."""

    revision: Optional[str]
    trust_remote_code: bool


def _validate_load_policy_options(
    revision: Optional[str],
    trust_remote_code: Optional[bool],
) -> None:
    if revision is not None and not isinstance(revision, str):
        raise TypeError("embedding revision must be a string or None")
    if trust_remote_code is not None and not isinstance(trust_remote_code, bool):
        raise TypeError("trust_remote_code must be a bool or None")


def _resolved_embedding_load_policy(
    model: str,
    *,
    revision: Optional[str],
    trust_remote_code: Optional[bool],
    is_local_model: bool,
) -> EmbeddingLoadPolicy:
    is_bundled_remote_model = model == DEFAULT_EMBEDDING_MODEL and not is_local_model
    bundled_revision = DEFAULT_EMBEDDING_REVISION if is_bundled_remote_model else None
    resolved_revision = revision if revision is not None else bundled_revision
    uses_bundled_revision = (
        is_bundled_remote_model and resolved_revision == DEFAULT_EMBEDDING_REVISION
    )
    resolved_trust = (
        uses_bundled_revision if trust_remote_code is None else trust_remote_code
    )
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


def _filesystem_shaped_artifact_model(model: str) -> bool:
    """Classify local-path spellings without consulting process state.

    URI schemes are case-insensitive, so every capitalization of ``file:`` is
    local-shaped and rejected.
    """

    lower = model.casefold()
    if (
        not model
        or model != model.strip()
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in model
        )
        or model.startswith(("/", "~"))
        or lower.startswith("file:")
        or "\\" in model
        or _WINDOWS_DRIVE_RE.match(model)
    ):
        return True
    return any(part in {"", ".", "..", "~"} for part in model.split("/"))


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

    _validate_load_policy_options(revision, trust_remote_code)

    is_local_model = Path(model).expanduser().is_dir()
    return _resolved_embedding_load_policy(
        model,
        revision=revision,
        trust_remote_code=trust_remote_code,
        is_local_model=is_local_model,
    )


def resolve_embedding_artifact_load_policy(
    model: str,
    *,
    revision: Optional[str] = None,
    trust_remote_code: Optional[bool] = None,
) -> EmbeddingLoadPolicy:
    """Resolve an artifact model identity without filesystem discovery.

    Published artifacts may name only deterministic remote model identities.
    Filesystem-shaped values are rejected lexically, before a caller opens or
    stats any publication or source path.
    """

    _validate_load_policy_options(revision, trust_remote_code)
    if not isinstance(model, str):
        raise TypeError("artifact embedding model must be a string")
    if _filesystem_shaped_artifact_model(model):
        raise ValueError(
            "artifact embedding model must be a remote model identifier, not a "
            "filesystem-shaped path"
        )
    return _resolved_embedding_load_policy(
        model,
        revision=revision,
        trust_remote_code=trust_remote_code,
        is_local_model=False,
    )


def _load_policy_options(options: Mapping[str, Any] | None) -> tuple[Any, Any]:
    values = dict(options or {})
    nested = values.get("model_kwargs") or {}
    if not isinstance(nested, Mapping):
        raise TypeError("embedding model_kwargs must be a mapping")

    def select(name: str) -> Any:
        direct_value = values.get(name, _UNSET)
        nested_value = nested.get(name, _UNSET)
        if (
            direct_value is not _UNSET
            and nested_value is not _UNSET
            and direct_value != nested_value
        ):
            raise ValueError(f"conflicting {name} values in embedding model options")
        if direct_value is not _UNSET:
            return direct_value
        if nested_value is not _UNSET:
            return nested_value
        return None

    return select("revision"), select("trust_remote_code")


def resolve_embedding_load_policy_from_options(
    model: str,
    options: Mapping[str, Any] | None,
) -> EmbeddingLoadPolicy:
    """Resolve identity controls accepted at either wrapper option level."""

    revision, trust_remote_code = _load_policy_options(options)

    return resolve_embedding_load_policy(
        model,
        revision=revision,
        trust_remote_code=trust_remote_code,
    )


def resolve_embedding_artifact_load_policy_from_options(
    model: str,
    options: Mapping[str, Any] | None,
) -> EmbeddingLoadPolicy:
    """Resolve artifact identity controls without filesystem discovery."""

    revision, trust_remote_code = _load_policy_options(options)
    return resolve_embedding_artifact_load_policy(
        model,
        revision=revision,
        trust_remote_code=trust_remote_code,
    )


__all__ = [
    "DEFAULT_EMBEDDING_DIMENSION",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_EMBEDDING_REVISION",
    "EmbeddingLoadPolicy",
    "resolve_embedding_artifact_load_policy",
    "resolve_embedding_artifact_load_policy_from_options",
    "resolve_embedding_load_policy",
    "resolve_embedding_load_policy_from_options",
]
