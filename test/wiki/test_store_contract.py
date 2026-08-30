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
            kind="outline",
            page_id=None,
            envelope=source,
        )
        source["data"]["title"] = "caller mutation"

        assert published.entry_id == "outline:repo-a"
        assert published.repository_id == "repo-a"
        assert published.kind == "outline"
        assert published.page_id is None
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
            kind="page",
            page_id="overview",
            envelope={"data": {"body": "first"}},
        )

        second = store.publish(
            entry_id="page:repo-a:overview",
            repository_id="repo-a",
            kind="page",
            page_id="overview",
            envelope={"data": {"body": "second"}},
        )

        assert second.envelope == {"data": {"body": "second"}}
        assert second.entry_id == first.entry_id
        assert second.repository_id == first.repository_id

    def test_if_absent_returns_the_persisted_winner(self, store: WikiStore) -> None:
        first = store.publish(
            entry_id="evidence:repo-a:overview",
            repository_id="repo-a",
            kind="evidence",
            page_id="overview",
            envelope={"data": {"citations": [1]}},
        )

        winner = store.publish(
            entry_id="evidence:repo-a:overview",
            repository_id="repo-a",
            kind="evidence",
            page_id="overview",
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
                kind="page",
                page_id=entry_id.removeprefix("page:"),
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
            kind="outline",
            page_id=None,
            envelope={"data": {}},
        )

        with pytest.raises(WikiStoreValidationError):
            store.publish(
                entry_id="outline:stable",
                repository_id="repo-b",
                kind="outline",
                page_id=None,
                envelope={"data": {}},
            )

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        (
            ({"entry_id": ""}, "entry_id"),
            ({"repository_id": ""}, "repository_id"),
            ({"kind": "asset"}, "kind"),
            ({"page_id": ""}, "page_id"),
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
            "kind": "page",
            "page_id": "valid",
            "envelope": {"data": {}},
        }
        arguments.update(kwargs)

        with pytest.raises(WikiStoreValidationError, match=message):
            store.publish(**arguments)

    @pytest.mark.parametrize(
        ("kind", "page_id", "message"),
        (
            ("outline", "overview", "must not have"),
            ("page", None, "require a page_id"),
            ("evidence", None, "require a page_id"),
        ),
    )
    def test_kind_controls_page_identity(
        self,
        store: WikiStore,
        kind: str,
        page_id: str | None,
        message: str,
    ) -> None:
        with pytest.raises(WikiStoreValidationError, match=message):
            store.publish(
                entry_id=f"{kind}:invalid",
                repository_id="repo-a",
                kind=kind,
                page_id=page_id,
                envelope={"data": {}},
            )

    def test_publish_rejects_an_oversize_envelope(self, store: WikiStore) -> None:
        with pytest.raises(WikiStoreValidationError, match="16777216-byte limit"):
            store.publish(
                entry_id="page:oversize",
                repository_id="repo-a",
                kind="page",
                page_id="oversize",
                envelope={"data": "x" * (16 * 1024 * 1024)},
            )


def test_stored_entry_is_frozen() -> None:
    entry = WikiStoredEntry(
        entry_id="outline:repo-a",
        repository_id="repo-a",
        kind="outline",
        page_id=None,
        envelope={"data": {}},
    )

    with pytest.raises(FrozenInstanceError):
        entry.entry_id = "replacement"  # type: ignore[misc]
