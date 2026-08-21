# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from codenib.repository_filters import (
    REPOSITORY_FILTER_POLICY_VERSION,
    count_repository_files,
    repository_path_is_visible,
    walk_repository_files,
)
from codenib.repository_source_selection import (
    REPOSITORY_SOURCE_SELECTION_POLICY_VERSION,
    REPOSITORY_SOURCE_SELECTION_SCHEMA,
    RepositorySourceSelection,
    repository_relative_source_path,
)


def test_selection_normalizes_order_duplicates_and_covered_descendants() -> None:
    selection = RepositorySourceSelection(
        [
            "vendor/generated",
            "ios/Pods/Dependency",
            "src/cache",
            "ios/Pods",
            "vendor",
            "vendorized/generated",
            "src/cache",
        ]
    )

    assert selection.exclude_subtrees == (
        "ios/Pods",
        "src/cache",
        "vendor",
        "vendorized/generated",
    )


def test_selection_matches_only_exact_subtrees() -> None:
    selection = RepositorySourceSelection(("ios/Pods", "src/generated"))

    assert selection.excludes("ios/Pods")
    assert selection.excludes("ios/Pods/Dependency/include/header.h")
    assert not selection.allows("src/generated/output.py")
    assert selection.allows("ios/Podspec")
    assert selection.allows("ios/PodsExtra/file.swift")
    assert selection.allows("nested/ios/Pods/file.swift")
    assert selection.allows("src/generator.py")


def test_selection_matches_non_utf8_observed_paths_without_persisting_them() -> None:
    selection = RepositorySourceSelection(("vendor",))

    assert selection.excludes("vendor/surrogate-\udcff.py")
    assert selection.allows("src/surrogate-\udcff.py")


def test_selection_allows_posix_filenames_with_literal_backslashes() -> None:
    selection = RepositorySourceSelection(("vendor",))

    assert selection.allows("src/odd\\name.py")
    assert repository_path_is_visible("src/odd\\name.py", selection=selection)


@pytest.mark.parametrize(
    "path",
    (
        "",
        ".",
        "..",
        "/absolute",
        "../outside",
        "nested/..",
        "nested/../outside",
        "./nested",
        "nested/.",
        "nested//child",
        "nested/",
        "windows\\path",
        "nul\x00path",
        "surrogate-\udcff",
    ),
)
def test_selection_rejects_noncanonical_exclude_paths(path: str) -> None:
    with pytest.raises(ValueError):
        RepositorySourceSelection((path,))


@pytest.mark.parametrize(
    "path",
    (
        "/".join("a" for _ in range(257)),
        "a" * 4097,
    ),
)
def test_selection_rejects_paths_beyond_source_limits(path: str) -> None:
    with pytest.raises(ValueError, match="supported limits"):
        RepositorySourceSelection((path,))


def test_selection_rejects_too_many_exclusions() -> None:
    with pytest.raises(ValueError, match="too many exclusions"):
        RepositorySourceSelection(f"generated/{index}" for index in range(4097))


def test_selection_rejects_oversized_combined_policy() -> None:
    component = "x" * 250
    values = (
        f"{index:04d}-{component}/{component}/{component}/{component}/{component}"
        for index in range(4096)
    )

    with pytest.raises(ValueError, match="too large"):
        RepositorySourceSelection(values)


@pytest.mark.parametrize("path", ("", ".", "..", "/absolute", "a/../b", "a\x00b"))
def test_selection_rejects_noncanonical_query_paths(path: str) -> None:
    selection = RepositorySourceSelection(("excluded",))

    with pytest.raises(ValueError):
        selection.excludes(path)
    with pytest.raises(ValueError):
        selection.allows(path)


@pytest.mark.parametrize(
    "path",
    ("../generated/x.py", "src/../generated/x.py", "/repo/generated/x.py"),
)
def test_repository_visibility_rejects_noncanonical_paths(path: str) -> None:
    assert not repository_path_is_visible(
        path,
        selection=RepositorySourceSelection(("generated",)),
    )


def test_selection_rejects_non_string_paths_and_scalar_collection() -> None:
    with pytest.raises(TypeError):
        RepositorySourceSelection("ios/Pods")
    with pytest.raises(TypeError):
        RepositorySourceSelection((42,))  # type: ignore[arg-type]


def test_selection_is_immutable_and_does_not_expose_mutable_state() -> None:
    selection = RepositorySourceSelection(("ios/Pods",))
    payload = selection.to_dict()

    with pytest.raises(FrozenInstanceError):
        selection.exclude_subtrees = ()
    payload["exclude_subtrees"].append("src/generated")

    assert selection.exclude_subtrees == ("ios/Pods",)
    assert selection.allows("src/generated")


def test_selection_has_stable_canonical_json_and_digest() -> None:
    first = RepositorySourceSelection(("vendor/subtree", "ios/Pods", "vendor"))
    second = RepositorySourceSelection(("vendor", "ios/Pods", "ios/Pods"))

    expected_json = (
        '{"exclude_subtrees":["ios/Pods","vendor"],'
        '"repository_filter_policy":4,'
        '"schema":"codenib.repository-source-selection.v1"}'
    )
    assert first.canonical_json() == expected_json
    assert first.canonical_bytes() == expected_json.encode("ascii")
    assert first.canonical_json() == second.canonical_json()
    assert first.digest == second.digest
    assert first.digest == (
        "sha256:8b3047a1a7a2d7f7fda0927fb1d0b257" "5d8efa59f8bc277c3d4b7935e763793a"
    )


def test_selection_canonical_json_escapes_non_ascii_paths() -> None:
    selection = RepositorySourceSelection(("生成/缓存",))

    assert "生成" not in selection.canonical_json()
    assert selection.canonical_bytes().isascii()


def test_selection_from_dict_requires_canonical_persisted_payload() -> None:
    selection = RepositorySourceSelection(("vendor", "ios/Pods"))

    assert RepositorySourceSelection.from_dict(selection.to_dict()) == selection

    noncanonical = selection.to_dict()
    noncanonical["exclude_subtrees"] = ["vendor/subtree", "vendor", "ios/Pods"]
    with pytest.raises(ValueError, match="not canonical"):
        RepositorySourceSelection.from_dict(noncanonical)

    class MappingSubclass(dict):
        pass

    with pytest.raises(ValueError, match="canonical fields"):
        RepositorySourceSelection.from_dict(MappingSubclass(selection.to_dict()))


@pytest.mark.parametrize(
    "payload",
    (
        {
            "schema": "codenib.repository-source-selection.v2",
            "repository_filter_policy": 4,
            "exclude_subtrees": [],
        },
        {
            "schema": REPOSITORY_SOURCE_SELECTION_SCHEMA,
            "repository_filter_policy": 3,
            "exclude_subtrees": [],
        },
        {
            "schema": REPOSITORY_SOURCE_SELECTION_SCHEMA,
            "repository_filter_policy": 4,
            "exclude_subtrees": (),
        },
        {
            "schema": REPOSITORY_SOURCE_SELECTION_SCHEMA,
            "repository_filter_policy": 4,
            "exclude_subtrees": [],
            "extra": True,
        },
        {
            "schema": REPOSITORY_SOURCE_SELECTION_SCHEMA,
            "repository_filter_policy": 4,
            "exclude_subtrees": [42],
        },
    ),
)
def test_selection_from_dict_rejects_noncanonical_contract(payload: dict) -> None:
    with pytest.raises(ValueError):
        RepositorySourceSelection.from_dict(payload)


def test_repository_filter_policy_migrates_to_selection_policy_four() -> None:
    assert REPOSITORY_FILTER_POLICY_VERSION == 4
    assert (
        REPOSITORY_FILTER_POLICY_VERSION == REPOSITORY_SOURCE_SELECTION_POLICY_VERSION
    )


def test_repository_filters_apply_selection_on_component_boundaries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "generated").mkdir(parents=True)
    (root / "generated-extra").mkdir()
    (root / "src" / "cache").mkdir(parents=True)
    (root / "generated" / "excluded.py").write_bytes(b"EXCLUDED = 1\n")
    (root / "generated-extra" / "included.py").write_bytes(b"INCLUDED = 1\n")
    (root / "src" / "cache" / "excluded.py").write_bytes(b"EXCLUDED = 2\n")
    (root / "src" / "included.py").write_bytes(b"INCLUDED = 2\n")
    selection = RepositorySourceSelection(("generated", "src/cache"))

    assert not repository_path_is_visible("generated", selection=selection)
    assert not repository_path_is_visible(
        "generated/nested/source.py",
        selection=selection,
    )
    assert repository_path_is_visible(
        "generated-extra/source.py",
        selection=selection,
    )
    assert repository_path_is_visible("src/cache-extra.py", selection=selection)

    walked = {
        path.relative_to(root).as_posix()
        for path in walk_repository_files(root, selection=selection)
    }
    assert walked == {"generated-extra/included.py", "src/included.py"}
    assert count_repository_files(root, selection=selection) == len(walked)


def test_repository_filters_reject_non_selection_policy(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="RepositorySourceSelection"):
        repository_path_is_visible("source.py", selection=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RepositorySourceSelection"):
        list(
            walk_repository_files(
                tmp_path,
                selection=None,  # type: ignore[arg-type]
            )
        )


def test_repository_relative_source_path_accepts_contained_absolute_and_relative(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"

    assert repository_relative_source_path(root, "src/main.py") == "src/main.py"
    assert (
        repository_relative_source_path(
            root,
            str(root / "packages" / "mobile" / "main.py"),
        )
        == "packages/mobile/main.py"
    )


@pytest.mark.parametrize(
    "source",
    (None, "../outside.py", "nested/../outside.py", "sibling.py"),
)
def test_repository_relative_source_path_rejects_invalid_or_outside(
    tmp_path: Path,
    source: object,
) -> None:
    root = tmp_path / "repo"
    value = str(tmp_path / source) if source == "sibling.py" else source

    with pytest.raises((TypeError, ValueError)):
        repository_relative_source_path(root, value)
