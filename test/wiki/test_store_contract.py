# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared behavioral contract for Wiki store implementations."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from codenib.wiki.store import WikiStore, WikiStoredEntry, WikiStoreValidationError


class WikiStoreContract:
    """Backend-neutral tests inherited by each production Wiki store."""

    @pytest.fixture
    def store(self) -> WikiStore:
        raise NotImplementedError

    def test_publish_and_read_return_isolated_envelopes(self, store: WikiStore) -> None:
        source = {"data": {"title": "Overview", "sections": ["one"]}}

        published = store.publish(
            entry_id="outline:repo-a",
            repository_id="repo-a",
            envelope=source,
        )
        source["data"]["title"] = "caller mutation"

        assert published.entry_id == "outline:repo-a"
        assert published.repository_id == "repo-a"
        assert published.envelope == {
            "data": {"title": "Overview", "sections": ["one"]}
        }
        published.envelope["data"]["sections"].append("returned mutation")
        loaded = store.read("outline:repo-a")

        assert loaded is not None
        assert loaded.envelope == {"data": {"title": "Overview", "sections": ["one"]}}

    def test_publish_replaces_payload(self, store: WikiStore) -> None:
        first = store.publish(
            entry_id="page:repo-a:overview",
            repository_id="repo-a",
            envelope={"data": {"body": "first"}},
        )

        second = store.publish(
            entry_id="page:repo-a:overview",
            repository_id="repo-a",
            envelope={"data": {"body": "second"}},
        )

        assert second.envelope == {"data": {"body": "second"}}
        assert second.entry_id == first.entry_id
        assert second.repository_id == first.repository_id

    def test_if_absent_returns_the_persisted_winner(self, store: WikiStore) -> None:
        first = store.publish(
            entry_id="evidence:repo-a:overview",
            repository_id="repo-a",
            envelope={"data": {"citations": [1]}},
        )

        winner = store.publish(
            entry_id="evidence:repo-a:overview",
            repository_id="repo-a",
            envelope={"data": {"citations": [2]}},
            if_absent=True,
        )

        assert winner == first
        assert store.read(first.entry_id) == first

    def test_scan_is_stable_and_supports_repository_filter(
        self, store: WikiStore
    ) -> None:
        for entry_id, repository_id in (
            ("page:z", "repo-b"),
            ("page:a", "repo-a"),
            ("page:m", "repo-a"),
        ):
            store.publish(
                entry_id=entry_id,
                repository_id=repository_id,
                envelope={"data": {"entry_id": entry_id}},
            )

        assert tuple(entry.entry_id for entry in store.scan()) == (
            "page:a",
            "page:m",
            "page:z",
        )
        assert tuple(
            entry.entry_id for entry in store.scan(repository_ids=["repo-a", "repo-a"])
        ) == ("page:a", "page:m")
        assert store.scan(repository_ids=[]) == ()

    def test_entry_identity_cannot_be_rebound(self, store: WikiStore) -> None:
        store.publish(
            entry_id="outline:stable",
            repository_id="repo-a",
            envelope={"data": {}},
        )

        with pytest.raises(WikiStoreValidationError):
            store.publish(
                entry_id="outline:stable",
                repository_id="repo-b",
                envelope={"data": {}},
            )

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        (
            ({"entry_id": ""}, "entry_id"),
            ({"repository_id": ""}, "repository_id"),
            ({"if_absent": 1}, "if_absent"),
            ({"envelope": {"bad": float("nan")}}, "bounded JSON"),
        ),
    )
    def test_publish_rejects_invalid_input(
        self,
        store: WikiStore,
        kwargs: dict[str, object],
        message: str,
    ) -> None:
        arguments: dict[str, object] = {
            "entry_id": "page:valid",
            "repository_id": "repo-a",
            "envelope": {"data": {}},
        }
        arguments.update(kwargs)

        with pytest.raises(WikiStoreValidationError, match=message):
            store.publish(**arguments)

    def test_publish_rejects_an_oversize_envelope(self, store: WikiStore) -> None:
        with pytest.raises(WikiStoreValidationError, match="16777216-byte limit"):
            store.publish(
                entry_id="page:oversize",
                repository_id="repo-a",
                envelope={"data": "x" * (16 * 1024 * 1024)},
            )

    def test_invalid_unicode_identifiers_raise_validation_errors(
        self,
        store: WikiStore,
    ) -> None:
        invalid = "\ud800"

        with pytest.raises(WikiStoreValidationError, match="valid Unicode"):
            store.read(invalid)
        with pytest.raises(WikiStoreValidationError, match="valid Unicode"):
            store.publish(
                entry_id=invalid,
                repository_id="repo-a",
                envelope={"data": {}},
            )
        with pytest.raises(WikiStoreValidationError, match="valid Unicode"):
            store.publish(
                entry_id="page:valid",
                repository_id=invalid,
                envelope={"data": {}},
            )
        with pytest.raises(WikiStoreValidationError, match="valid Unicode"):
            store.scan(repository_ids=[invalid])
        with pytest.raises(WikiStoreValidationError, match="valid Unicode"):
            with store.generation_guard(invalid):
                pass


def test_stored_entry_is_frozen() -> None:
    entry = WikiStoredEntry(
        entry_id="outline:repo-a",
        repository_id="repo-a",
        envelope={"data": {}},
    )

    with pytest.raises(FrozenInstanceError):
        entry.entry_id = "replacement"  # type: ignore[misc]
