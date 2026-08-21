# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Canonical, backend-neutral repository source-selection policy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

REPOSITORY_SOURCE_SELECTION_SCHEMA = "codenib.repository-source-selection.v1"
REPOSITORY_SOURCE_SELECTION_POLICY_VERSION = 4

_PAYLOAD_FIELDS = frozenset(
    {
        "schema",
        "repository_filter_policy",
        "exclude_subtrees",
    }
)
_MAX_SELECTION_PATH_BYTES = 4096
_MAX_SELECTION_PATH_COMPONENTS = 256
_MAX_SELECTION_EXCLUSIONS = 4096
_MAX_SELECTION_TOTAL_BYTES = 1024 * 1024


def _canonical_observed_relative_path(value: object) -> str:
    if type(value) is not str:
        raise TypeError("repository source-selection paths must be strings")
    if not value or "\x00" in value:
        raise ValueError(
            "repository source-selection paths must be non-empty, NUL-free "
            "repository-relative POSIX paths"
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value in {".", ".."}
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise ValueError(
            "repository source-selection paths must be canonical "
            "repository-relative POSIX paths"
        )
    return value


def _canonical_relative_path(value: object) -> str:
    value = _canonical_observed_relative_path(value)
    if "\\" in value:
        raise ValueError("repository source-selection paths must use POSIX separators")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "repository source-selection paths must contain valid Unicode"
        ) from exc
    if (
        len(encoded) > _MAX_SELECTION_PATH_BYTES
        or len(PurePosixPath(value).parts) > _MAX_SELECTION_PATH_COMPONENTS
    ):
        raise ValueError(
            "repository source-selection paths exceed the supported limits"
        )
    return value


def _canonical_exclude_subtrees(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("exclude_subtrees must be an iterable of paths")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise TypeError("exclude_subtrees must be an iterable of paths") from exc
    canonical: set[str] = set()
    total_bytes = 0
    for count, value in enumerate(iterator, start=1):
        if count > _MAX_SELECTION_EXCLUSIONS:
            raise ValueError("repository source selection has too many exclusions")
        normalized_value = _canonical_relative_path(value)
        total_bytes += len(normalized_value.encode("utf-8"))
        if total_bytes > _MAX_SELECTION_TOTAL_BYTES:
            raise ValueError("repository source selection is too large")
        canonical.add(normalized_value)
    normalized = sorted(canonical)

    selected: list[str] = []
    selected_set: set[str] = set()
    for value in normalized:
        parts = PurePosixPath(value).parts
        if any(
            "/".join(parts[:component_count]) in selected_set
            for component_count in range(1, len(parts))
        ):
            continue
        selected.append(value)
        selected_set.add(value)
    return tuple(selected)


def repository_relative_source_path(
    repository_root: str | Path,
    source_path: object,
) -> str:
    """Return one canonical lexical source path relative to *repository_root*.

    Builder outputs historically carry either absolute checkout paths or
    repository-relative POSIX paths. This adapter never resolves the source
    path: absolute values must already be component-contained in the supplied
    lexical root, and the resulting relative value must satisfy the persisted
    source-selection grammar.
    """

    if type(source_path) is not str:
        raise TypeError("repository source paths must be strings")
    root = Path(repository_root).expanduser().absolute()
    candidate = Path(source_path)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                "repository source path is outside the repository root"
            ) from exc
    return _canonical_observed_relative_path(candidate.as_posix())


@dataclass(frozen=True, slots=True, init=False)
class RepositorySourceSelection:
    """Immutable exact-subtree exclusions for one repository source view.

    Paths are canonical root-relative POSIX paths. An exclusion matches its
    exact path and descendants separated by ``/``; similarly named siblings do
    not match.
    """

    exclude_subtrees: tuple[str, ...] = ()
    _exclude_index: frozenset[str] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __init__(self, exclude_subtrees: Iterable[str] = ()) -> None:
        normalized = _canonical_exclude_subtrees(exclude_subtrees)
        object.__setattr__(self, "exclude_subtrees", normalized)
        object.__setattr__(self, "_exclude_index", frozenset(normalized))

    def excludes(self, relative_path: str) -> bool:
        """Return whether *relative_path* is an excluded subtree member."""

        value = _canonical_observed_relative_path(relative_path)
        parts = PurePosixPath(value).parts
        return any(
            "/".join(parts[:component_count]) in self._exclude_index
            for component_count in range(1, len(parts) + 1)
        )

    def allows(self, relative_path: str) -> bool:
        """Return whether *relative_path* survives the exact-subtree policy."""

        return not self.excludes(relative_path)

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible selection payload."""

        return {
            "schema": REPOSITORY_SOURCE_SELECTION_SCHEMA,
            "repository_filter_policy": (REPOSITORY_SOURCE_SELECTION_POLICY_VERSION),
            "exclude_subtrees": list(self.exclude_subtrees),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RepositorySourceSelection:
        """Load a canonical persisted selection payload without coercion."""

        if type(payload) is not dict or set(payload) != _PAYLOAD_FIELDS:
            raise ValueError("repository source selection must use canonical fields")
        if payload["schema"] != REPOSITORY_SOURCE_SELECTION_SCHEMA:
            raise ValueError("repository source selection schema is unsupported")
        if (
            type(payload["repository_filter_policy"]) is not int
            or payload["repository_filter_policy"]
            != REPOSITORY_SOURCE_SELECTION_POLICY_VERSION
        ):
            raise ValueError("repository source selection policy is unsupported")
        values = payload["exclude_subtrees"]
        if type(values) is not list:
            raise ValueError("repository source exclusions must be a JSON array")
        try:
            selection = cls(values)
        except (TypeError, ValueError) as exc:
            raise ValueError("repository source exclusions are invalid") from exc
        if list(selection.exclude_subtrees) != values:
            raise ValueError("repository source exclusions are not canonical")
        return selection

    def canonical_json(self) -> str:
        """Return stable, whitespace-free ASCII JSON for this selection."""

        return json.dumps(
            self.to_dict(),
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    def canonical_bytes(self) -> bytes:
        """Return the canonical serialized selection bytes."""

        return self.canonical_json().encode("ascii")

    @property
    def digest(self) -> str:
        """Return the canonical SHA-256 identity of this selection."""

        return f"sha256:{hashlib.sha256(self.canonical_bytes()).hexdigest()}"


DEFAULT_REPOSITORY_SOURCE_SELECTION = RepositorySourceSelection()


__all__ = [
    "DEFAULT_REPOSITORY_SOURCE_SELECTION",
    "REPOSITORY_SOURCE_SELECTION_POLICY_VERSION",
    "REPOSITORY_SOURCE_SELECTION_SCHEMA",
    "RepositorySourceSelection",
    "repository_relative_source_path",
]
