# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Out-of-band authorization for native index parsers.

Artifact bytes and manifests can describe an index, but they can never mint this
capability.  A trusted caller first binds a captured tree and semantic contract,
then passes the resulting process-local token to the exact native consumer.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from ._atomic_directory import (
    directory_ownership_digest,
    directory_ownership_file_records,
    directory_ownership_root_identity,
)

NATIVE_INDEX_SUBJECT_SCHEMA = "codenib.native-index-subject.v1"
FAISS_PARSER_CONTRACT = "codenib.faiss-read-index.v1"
NativeTrustClass = Literal["trusted-local"]
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_SEAL = object()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _semantic_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class NativeIndexSubject:
    parser_contract: str
    view_type: str
    tree_digest: str
    semantic_contract_digest: str
    files: tuple[tuple[str, int, int, str], ...]

    def canonical_bytes(self) -> bytes:
        return _canonical_json(
            {
                "schema": NATIVE_INDEX_SUBJECT_SCHEMA,
                "parser_contract": self.parser_contract,
                "view_type": self.view_type,
                "tree_digest": self.tree_digest,
                "semantic_contract_digest": self.semantic_contract_digest,
                "files": [
                    {
                        "path": path,
                        "mode": mode,
                        "size": size,
                        "sha256": digest,
                    }
                    for path, mode, size, digest in self.files
                ],
            }
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class NativeIndexAuthorization:
    """Non-serializable, process-local capability for one native subject."""

    __slots__ = (
        "_seal",
        "trust_class",
        "view_type",
        "subject_digest",
        "root_identity",
        "evidence",
    )

    def __init__(
        self,
        seal: object,
        *,
        trust_class: NativeTrustClass,
        view_type: str,
        subject_digest: str,
        root_identity: tuple[int, ...] | None,
        evidence: tuple[str, ...],
    ) -> None:
        if seal is not _TOKEN_SEAL:
            raise TypeError("native index authorization cannot be constructed directly")
        self._seal = seal
        self.trust_class = trust_class
        self.view_type = view_type
        self.subject_digest = subject_digest
        self.root_identity = root_identity
        self.evidence = evidence

    def __reduce__(self):
        raise TypeError(
            "native index authorization is process-local and not serializable"
        )

    def __repr__(self) -> str:
        return (
            "NativeIndexAuthorization("
            f"trust_class={self.trust_class!r}, view_type={self.view_type!r}, "
            f"subject_digest={self.subject_digest[:12]}...)"
        )


def native_index_subject(
    ownership: object,
    *,
    view_type: str,
    semantic_contract: Mapping[str, Any],
    parser_contract: str = FAISS_PARSER_CONTRACT,
) -> NativeIndexSubject:
    if not isinstance(view_type, str) or not view_type:
        raise ValueError("native index view type must be non-empty")
    if not isinstance(semantic_contract, Mapping):
        raise TypeError("native index semantic contract must be a mapping")
    records = directory_ownership_file_records(ownership)  # type: ignore[arg-type]
    return NativeIndexSubject(
        parser_contract=parser_contract,
        view_type=view_type,
        tree_digest=directory_ownership_digest(ownership),  # type: ignore[arg-type]
        semantic_contract_digest=_semantic_digest(dict(semantic_contract)),
        files=tuple(
            (record.path, record.mode, record.size, record.sha256) for record in records
        ),
    )


def mint_trusted_local_authorization(
    ownership: object,
    *,
    view_type: str,
    semantic_contract: Mapping[str, Any],
    evidence: Sequence[str] = (),
) -> NativeIndexAuthorization:
    """Mint after the caller's local lock/source/config checks have succeeded."""

    subject = native_index_subject(
        ownership,
        view_type=view_type,
        semantic_contract=semantic_contract,
    )
    normalized_evidence = tuple(evidence)
    if any(not isinstance(item, str) or not item for item in normalized_evidence):
        raise ValueError(
            "native index authorization evidence must be non-empty strings"
        )
    return NativeIndexAuthorization(
        _TOKEN_SEAL,
        trust_class="trusted-local",
        view_type=view_type,
        subject_digest=subject.digest,
        root_identity=directory_ownership_root_identity(ownership),  # type: ignore[arg-type]
        evidence=normalized_evidence,
    )


def require_native_index_authorization(
    authorization: NativeIndexAuthorization | None,
    ownership: object,
    *,
    view_type: str,
    semantic_contract: Mapping[str, Any],
) -> NativeIndexSubject:
    if (
        not isinstance(authorization, NativeIndexAuthorization)
        or authorization._seal is not _TOKEN_SEAL
    ):
        raise ValueError("native index parsing requires external authorization")
    subject = native_index_subject(
        ownership,
        view_type=view_type,
        semantic_contract=semantic_contract,
    )
    if (
        authorization.view_type != view_type
        or authorization.subject_digest != subject.digest
        or not _DIGEST_RE.fullmatch(authorization.subject_digest)
    ):
        raise ValueError("native index authorization does not match captured bytes")
    if (
        authorization.trust_class == "trusted-local"
        and authorization.root_identity
        != (directory_ownership_root_identity(ownership))  # type: ignore[arg-type]
    ):
        raise ValueError("trusted-local authorization does not match the root identity")
    if authorization.trust_class != "trusted-local":
        raise ValueError("native index authorization has an unsupported trust class")
    return subject


__all__ = [
    "FAISS_PARSER_CONTRACT",
    "NATIVE_INDEX_SUBJECT_SCHEMA",
    "NativeIndexAuthorization",
    "NativeIndexSubject",
    "mint_trusted_local_authorization",
    "native_index_subject",
    "require_native_index_authorization",
]
