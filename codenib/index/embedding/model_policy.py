# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Embedding model defaults and remote-code trust policy."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cmp_to_key
from pathlib import Path
from typing import Any, Optional

DEFAULT_EMBEDDING_MODEL = "nomic-ai/CodeRankEmbed"
DEFAULT_EMBEDDING_DIMENSION = 768
DEFAULT_EMBEDDING_REVISION = "3c4b60807d71f79b43f3c4363786d9493691f8b1"

_IMMUTABLE_REVISION_RE = re.compile(r"[0-9a-f]{40}\Z")
_WINDOWS_DRIVE_RE = re.compile(r"[A-Za-z]:")
_UNSET = object()
_POLICY_SCAN_CHARS = 64 * 1024


def _compare_policy_text_interruptibly(
    left: str,
    right: str,
    *,
    check_cancelled: Callable[[], None],
) -> int:
    if left is right:
        return 0
    shared_length = min(len(left), len(right))
    for offset in range(0, shared_length, _POLICY_SCAN_CHARS):
        end = min(shared_length, offset + _POLICY_SCAN_CHARS)
        left_piece = left[offset:end]
        right_piece = right[offset:end]
        if left_piece < right_piece:
            return -1
        if left_piece > right_piece:
            return 1
        if end < shared_length:
            check_cancelled()
    return (len(left) > len(right)) - (len(left) < len(right))


def _sorted_policy_text_items_interruptibly(
    value: dict[Any, Any],
    *,
    check_cancelled: Callable[[], None],
) -> list[tuple[str, Any]] | None:
    items: list[tuple[str, Any]] = []
    item_count = dict.__len__(value)
    for index, (key, item) in enumerate(dict.items(value)):
        if type(key) is not str:
            return None
        items.append((key, item))
        if index + 1 < item_count:
            check_cancelled()

    def compare(left: tuple[str, Any], right: tuple[str, Any]) -> int:
        return _compare_policy_text_interruptibly(
            left[0],
            right[0],
            check_cancelled=check_cancelled,
        )

    runs: list[list[tuple[str, Any]]] = []
    for start in range(0, len(items), 256):
        end = min(len(items), start + 256)
        run = items[start:end]
        run.sort(key=cmp_to_key(compare))
        runs.append(run)
        if end < len(items):
            check_cancelled()
    while len(runs) > 1:
        check_cancelled()
        merged_runs: list[list[tuple[str, Any]]] = []
        for run_index in range(0, len(runs), 2):
            left = runs[run_index]
            if run_index + 1 == len(runs):
                merged_runs.append(left)
                continue
            right = runs[run_index + 1]
            merged: list[tuple[str, Any]] = []
            left_index = 0
            right_index = 0
            merged_count = len(left) + len(right)
            while left_index < len(left) and right_index < len(right):
                if compare(left[left_index], right[right_index]) <= 0:
                    merged.append(left[left_index])
                    left_index += 1
                else:
                    merged.append(right[right_index])
                    right_index += 1
                if len(merged) < merged_count:
                    check_cancelled()
            for remaining, offset in (
                (left, left_index),
                (right, right_index),
            ):
                for index in range(offset, len(remaining)):
                    merged.append(remaining[index])
                    if len(merged) < merged_count:
                        check_cancelled()
            merged_runs.append(merged)
        runs = merged_runs
    return runs[0] if runs else []


def _policy_values_equal_interruptibly(
    left: Any,
    right: Any,
    *,
    check_cancelled: Callable[[], None],
) -> bool:
    if left is right and type(left) in {
        type(None),
        bool,
        int,
        str,
        bytes,
        list,
        tuple,
        dict,
    }:
        return True
    if (type(left) is str and type(right) is str) or (
        type(left) is bytes and type(right) is bytes
    ):
        left_value = left
        right_value = right
        if len(left_value) != len(right_value):
            return False
        value_length = len(left_value)
        for offset in range(0, value_length, _POLICY_SCAN_CHARS):
            end = min(value_length, offset + _POLICY_SCAN_CHARS)
            if left_value[offset:end] != right_value[offset:end]:
                return False
            if end < value_length:
                check_cancelled()
        return True
    if (type(left) is list and type(right) is list) or (
        type(left) is tuple and type(right) is tuple
    ):
        sequence_type = type(left)
        left_length = sequence_type.__len__(left)
        right_length = sequence_type.__len__(right)
        if left_length != right_length:
            return False
        item_count = left_length
        for index in range(item_count):
            left_item = sequence_type.__getitem__(left, index)
            right_item = sequence_type.__getitem__(right, index)
            if left_item is not right_item and not _policy_values_equal_interruptibly(
                left_item,
                right_item,
                check_cancelled=check_cancelled,
            ):
                return False
            if index + 1 < item_count:
                check_cancelled()
        return True
    if type(left) is dict and type(right) is dict:
        if dict.__len__(left) != dict.__len__(right):
            return False
        item_count = dict.__len__(left)
        if not item_count:
            return True
        aligned = True
        for index, ((left_key, left_value), (right_key, right_value)) in enumerate(
            zip(dict.items(left), dict.items(right), strict=True)
        ):
            if left_key is not right_key and not _policy_values_equal_interruptibly(
                left_key,
                right_key,
                check_cancelled=check_cancelled,
            ):
                aligned = False
                break
            if left_value is not right_value and not (
                _policy_values_equal_interruptibly(
                    left_value,
                    right_value,
                    check_cancelled=check_cancelled,
                )
            ):
                return False
            if index + 1 < item_count:
                check_cancelled()
        if aligned:
            return True
        left_items = _sorted_policy_text_items_interruptibly(
            left,
            check_cancelled=check_cancelled,
        )
        right_items = (
            None
            if left_items is None
            else _sorted_policy_text_items_interruptibly(
                right,
                check_cancelled=check_cancelled,
            )
        )
        if left_items is not None and right_items is not None:
            for index, ((left_key, left_value), (right_key, right_value)) in enumerate(
                zip(left_items, right_items, strict=True)
            ):
                if (
                    left_key is not right_key
                    and not _policy_values_equal_interruptibly(
                        left_key,
                        right_key,
                        check_cancelled=check_cancelled,
                    )
                ):
                    return False
                if left_value is not right_value and not (
                    _policy_values_equal_interruptibly(
                        left_value,
                        right_value,
                        check_cancelled=check_cancelled,
                    )
                ):
                    return False
                if index + 1 < item_count:
                    check_cancelled()
            return True
        for index, (key, left_value) in enumerate(dict.items(left)):
            check_cancelled()
            try:
                right_value = dict.__getitem__(right, key)
            except KeyError:
                return False
            if (
                left_value is not right_value
                and not _policy_values_equal_interruptibly(
                    left_value,
                    right_value,
                    check_cancelled=check_cancelled,
                )
            ):
                return False
            if index + 1 < item_count:
                check_cancelled()
        return True
    if type(left) in {list, tuple, dict} and type(right) in {list, tuple, dict}:
        return False
    if type(left) not in {type(None), bool, int, float, str, bytes} or type(
        right
    ) not in {
        type(None),
        bool,
        int,
        float,
        str,
        bytes,
    }:
        check_cancelled()
    return bool(left == right)


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
            resolved_revision
            and str.__len__(resolved_revision) == 40
            and _IMMUTABLE_REVISION_RE.fullmatch(resolved_revision)
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


def _filesystem_shaped_artifact_model(
    model: str,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> bool:
    """Classify local-path spellings without consulting process state.

    URI schemes are case-insensitive, so every capitalization of ``file:`` is
    local-shaped and rejected.
    """

    def has_file_scheme_prefix_interruptibly() -> bool:
        target = "file:"
        matched = 0
        for index, character in enumerate(model):
            for folded in character.casefold():
                if folded != target[matched]:
                    return False
                matched += 1
                if matched == len(target):
                    return True
            if (index + 1) % _POLICY_SCAN_CHARS == 0:
                assert check_cancelled is not None
                check_cancelled()
        return False

    if check_cancelled is not None:
        if not model or model[0].isspace() or model[-1].isspace():
            return True
        if (
            model.startswith(("/", "~"))
            or has_file_scheme_prefix_interruptibly()
            or _WINDOWS_DRIVE_RE.match(model)
        ):
            return True
        component_length = 0
        component_prefix = ""
        model_length = len(model)
        for index, character in enumerate(model):
            if (
                character.isspace()
                or ord(character) < 32
                or ord(character) == 127
                or character == "\\"
            ):
                return True
            if character == "/":
                if component_length == 0 or (
                    component_length <= 2 and component_prefix in {".", "..", "~"}
                ):
                    return True
                component_length = 0
                component_prefix = ""
            else:
                component_length += 1
                if component_length <= 2:
                    component_prefix += character
            if index + 1 < model_length and (index + 1) % _POLICY_SCAN_CHARS == 0:
                check_cancelled()
        return component_length == 0 or (
            component_length <= 2 and component_prefix in {".", "..", "~"}
        )

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
    check_cancelled: Callable[[], None] | None = None,
) -> EmbeddingLoadPolicy:
    """Resolve an artifact model identity without filesystem discovery.

    Published artifacts may name only deterministic remote model identities.
    Filesystem-shaped values are rejected lexically, before a caller opens or
    stats any publication or source path.
    """

    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError("artifact cancellation check must be callable")
    _validate_load_policy_options(revision, trust_remote_code)
    if not isinstance(model, str):
        raise TypeError("artifact embedding model must be a string")
    filesystem_shaped = (
        _filesystem_shaped_artifact_model(model)
        if check_cancelled is None
        else _filesystem_shaped_artifact_model(
            model,
            check_cancelled=check_cancelled,
        )
    )
    if filesystem_shaped:
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


def _load_policy_options(
    options: Mapping[str, Any] | None,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[Any, Any]:
    callback_errors: list[BaseException] = []
    if check_cancelled is not None:
        untracked_check_cancelled = check_cancelled

        def tracked_check_cancelled() -> None:
            try:
                untracked_check_cancelled()
            except BaseException as error:  # noqa: B036 - exact provenance
                callback_errors.append(error)
                raise

        check_cancelled = tracked_check_cancelled

    recognized_values: dict[str, Any] = {}
    recognized_values_are_safe = True
    outer_lookup_needs_poll = False

    def attest_prefix() -> None:
        nested_candidate = recognized_values.get("model_kwargs", _UNSET)
        if nested_candidate is not _UNSET and type(nested_candidate) in {
            bool,
            int,
            float,
            str,
            bytes,
            tuple,
            list,
            set,
            frozenset,
        }:
            if nested_candidate:
                raise TypeError("embedding model_kwargs must be a mapping")
        revision = recognized_values.get("revision", None)
        trust_remote_code = recognized_values.get("trust_remote_code", None)
        _validate_load_policy_options(revision, trust_remote_code)

    def copy_mapping(
        source: Mapping[Any, Any],
    ) -> dict[Any, Any]:
        copied: dict[Any, Any] = {}
        assert check_cancelled is not None

        def copy_current(key: Any, value: Any) -> None:
            nonlocal outer_lookup_needs_poll, recognized_values_are_safe
            copied[key] = value
            if type(key) is not str:
                # A caller key may compare equal to any recognized text key and
                # overwrite the effective dict value.  Once that happens, do
                # not use stale exact-key side state to reconcile cancellation.
                recognized_values.clear()
                recognized_values_are_safe = False
                outer_lookup_needs_poll = True
            elif recognized_values_are_safe:
                if key in {"model_kwargs", "revision", "trust_remote_code"}:
                    recognized_values[key] = value

        def poll_after_current() -> None:
            try:
                check_cancelled()
            except BaseException:  # noqa: B036 - preserve exact stop
                attest_prefix()
                raise

        if type(source) is dict:
            item_count = len(source)
            for index, (key, value) in enumerate(source.items()):
                copy_current(key, value)
                if index + 1 < item_count:
                    poll_after_current()
            return copied

        check_cancelled()
        keys = source.keys()
        check_cancelled()
        iterator = iter(keys)
        check_cancelled()
        while True:
            try:
                key = next(iterator)
            except StopIteration:
                break
            check_cancelled()
            value = source[key]
            copy_current(key, value)
            poll_after_current()
        return copied

    if check_cancelled is None:
        values = dict(options or {})
    else:
        if options is None:
            source: Any = {}
        elif type(options) is dict:
            source = options if options else {}
        else:
            # Preserve ``options or {}`` while polling before caller-owned
            # truthiness and letting a falsey current result terminate without
            # a trailing callback.
            check_cancelled()
            source = options if options else {}
        if not isinstance(source, Mapping):
            raise TypeError("embedding options must be a mapping")
        values = copy_mapping(source)
        if outer_lookup_needs_poll:
            try:
                check_cancelled()
            except BaseException:  # noqa: B036 - exact stop reconciliation
                attest_prefix()
                raise
    nested_candidate = values.get("model_kwargs")
    if check_cancelled is None:
        nested = nested_candidate or {}
        if not isinstance(nested, Mapping):
            raise TypeError("embedding model_kwargs must be a mapping")
    else:
        if type(nested_candidate) not in {
            type(None),
            bool,
            int,
            float,
            str,
            bytes,
            tuple,
            list,
            dict,
        }:
            check_cancelled()
        nested = nested_candidate or {}
        if not isinstance(nested, Mapping):
            raise TypeError("embedding model_kwargs must be a mapping")

    nested_lookup_needs_poll = type(nested) is not dict
    if type(nested) is dict:
        nested_count = dict.__len__(nested)
        nested_lookup_needs_poll = nested_count > 16 or any(
            type(key) is not str for key in dict.keys(nested)
        )

    def select(name: str) -> Any:
        if check_cancelled is not None and outer_lookup_needs_poll:
            try:
                check_cancelled()
            except BaseException:  # noqa: B036 - exact stop reconciliation
                attest_prefix()
                raise
        direct_value = values.get(name, _UNSET)
        if check_cancelled is not None and nested_lookup_needs_poll:
            try:
                check_cancelled()
            except BaseException:  # noqa: B036 - current direct value wins
                if direct_value is not _UNSET:
                    if name == "revision":
                        _validate_load_policy_options(direct_value, None)
                    else:
                        _validate_load_policy_options(None, direct_value)
                raise
        nested_value = nested.get(name, _UNSET)

        if direct_value is _UNSET or nested_value is _UNSET:
            values_differ = direct_value is not nested_value
        elif check_cancelled is not None:
            exact_policy_types = {
                type(None),
                bool,
                int,
                float,
                str,
                bytes,
                list,
                tuple,
                dict,
            }
            if (
                type(direct_value) in exact_policy_types
                and type(nested_value) in exact_policy_types
            ):
                values_differ = not _policy_values_equal_interruptibly(
                    direct_value,
                    nested_value,
                    check_cancelled=check_cancelled,
                )
            else:
                check_cancelled()
                values_differ = bool(direct_value != nested_value)
        else:
            values_differ = bool(direct_value != nested_value)
        if direct_value is not _UNSET and nested_value is not _UNSET and values_differ:
            raise ValueError(f"conflicting {name} values in embedding model options")
        if direct_value is not _UNSET:
            return direct_value
        if nested_value is not _UNSET:
            return nested_value
        return None

    revision = select("revision")
    try:
        trust_remote_code = select("trust_remote_code")
    except BaseException as error:  # noqa: B036 - reconcile only exact callback
        if any(error is callback_error for callback_error in callback_errors):
            _validate_load_policy_options(revision, None)
        raise
    return revision, trust_remote_code


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
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> EmbeddingLoadPolicy:
    """Resolve artifact identity controls without filesystem discovery."""

    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError("artifact cancellation check must be callable")
    if check_cancelled is None:
        revision, trust_remote_code = _load_policy_options(options)
        return resolve_embedding_artifact_load_policy(
            model,
            revision=revision,
            trust_remote_code=trust_remote_code,
        )
    revision, trust_remote_code = _load_policy_options(
        options,
        check_cancelled=check_cancelled,
    )
    return resolve_embedding_artifact_load_policy(
        model,
        revision=revision,
        trust_remote_code=trust_remote_code,
        check_cancelled=check_cancelled,
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
