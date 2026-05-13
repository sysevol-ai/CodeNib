# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for index resource management."""

from __future__ import annotations

import json
import time

from codeminer.compiler.resources import (
    FileSystemIndexStateStore,
    IndexRequirement,
    IndexState,
    IndexStatus,
    InMemoryIndexStateStore,
    ResourceDecision,
    ResourcePlan,
    ResourceResolver,
    _format_duration,
)


class TestIndexRequirement:
    def test_to_dict(self):
        req = IndexRequirement(index_type="bm25", max_age_seconds=3600)
        d = req.to_dict()
        assert d["index_type"] == "bm25"
        assert d["max_age_seconds"] == 3600
        assert d["scope"] == "current_repo"
        assert d["required"] is True
        assert json.dumps(d)

    def test_defaults(self):
        req = IndexRequirement(index_type="vector")
        assert req.max_age_seconds == 3600.0
        assert req.scope == "current_repo"
        assert req.required is True


class TestInMemoryStateStore:
    def test_get_missing(self):
        store = InMemoryIndexStateStore()
        assert store.get_status("bm25", "repo") is None

    def test_set_and_get(self):
        store = InMemoryIndexStateStore()
        status = IndexStatus(
            index_type="bm25",
            state=IndexState.FRESH,
            last_built=time.time(),
            age_seconds=0.0,
            scope="repo",
        )
        store.set_status(status)
        retrieved = store.get_status("bm25", "repo")
        assert retrieved is not None
        assert retrieved.index_type == "bm25"
        assert retrieved.state == IndexState.FRESH


class TestResourceResolver:
    def test_missing_required_blocks(self):
        store = InMemoryIndexStateStore()
        resolver = ResourceResolver(store)

        plan = resolver.resolve(
            [
                IndexRequirement(index_type="bm25", max_age_seconds=3600),
            ]
        )

        assert not plan.can_execute
        assert "bm25" in plan.blocking_builds
        assert plan.decisions[0].state == IndexState.MISSING
        assert plan.decisions[0].action == "rebuild"
        assert plan.decisions[0].blocking is True

    def test_missing_optional_skipped(self):
        store = InMemoryIndexStateStore()
        resolver = ResourceResolver(store)

        plan = resolver.resolve(
            [
                IndexRequirement(index_type="graph", required=False),
            ]
        )

        assert plan.can_execute
        assert plan.blocking_builds == []
        assert plan.decisions[0].action == "skip"
        assert len(plan.warnings) == 1
        assert "non-required" in plan.warnings[0]

    def test_fresh_index(self):
        store = InMemoryIndexStateStore()
        now = time.time()
        store.set_status(
            IndexStatus(
                index_type="bm25",
                state=IndexState.FRESH,
                last_built=now - 100,  # 100 seconds ago
                age_seconds=100,
                scope="current_repo",
            )
        )
        resolver = ResourceResolver(store)

        plan = resolver.resolve(
            [
                IndexRequirement(index_type="bm25", max_age_seconds=3600),
            ]
        )

        assert plan.can_execute
        assert plan.decisions[0].state == IndexState.FRESH
        assert plan.decisions[0].action == "use"
        assert plan.blocking_builds == []
        assert plan.async_updates == []

    def test_stale_index_triggers_update(self):
        store = InMemoryIndexStateStore()
        now = time.time()
        store.set_status(
            IndexStatus(
                index_type="bm25",
                state=IndexState.FRESH,
                last_built=now - 7200,  # 2 hours ago
                age_seconds=7200,
                scope="current_repo",
            )
        )
        resolver = ResourceResolver(store)

        plan = resolver.resolve(
            [
                IndexRequirement(index_type="bm25", max_age_seconds=3600),
            ]
        )

        assert plan.can_execute  # stale is not blocking
        assert plan.decisions[0].state == IndexState.STALE
        assert plan.decisions[0].action == "incremental_update"
        assert "bm25" in plan.async_updates
        assert len(plan.warnings) == 1
        assert "old" in plan.warnings[0]

    def test_multiple_requirements(self):
        store = InMemoryIndexStateStore()
        now = time.time()
        store.set_status(
            IndexStatus(
                index_type="bm25",
                state=IndexState.FRESH,
                last_built=now - 100,
                age_seconds=100,
                scope="current_repo",
            )
        )
        # vector index missing
        resolver = ResourceResolver(store)

        plan = resolver.resolve(
            [
                IndexRequirement(index_type="bm25", max_age_seconds=3600),
                IndexRequirement(index_type="vector", max_age_seconds=3600),
            ]
        )

        assert not plan.can_execute  # vector is missing + required
        assert "vector" in plan.blocking_builds
        assert len(plan.decisions) == 2


class TestResourcePlanSerialisation:
    def test_to_dict(self):
        plan = ResourcePlan(
            decisions=[
                ResourceDecision(
                    index_type="bm25",
                    state=IndexState.FRESH,
                    action="use",
                ),
                ResourceDecision(
                    index_type="vector",
                    state=IndexState.STALE,
                    action="incremental_update",
                    warning="vector is 2.0h old",
                ),
            ],
            can_execute=True,
            async_updates=["vector"],
            warnings=["vector is 2.0h old"],
        )
        d = plan.to_dict()
        assert json.dumps(d)
        assert d["can_execute"] is True
        assert len(d["decisions"]) == 2
        assert d["decisions"][1]["state"] == "stale"


class TestFileSystemStateStore:
    def test_roundtrip(self, tmp_path):
        store = FileSystemIndexStateStore(str(tmp_path))

        status = IndexStatus(
            index_type="bm25",
            state=IndexState.FRESH,
            last_built=time.time(),
            scope="current_repo",
            metadata={"version": 1},
        )
        store.set_status(status)

        retrieved = store.get_status("bm25", "current_repo")
        assert retrieved is not None
        assert retrieved.index_type == "bm25"
        assert retrieved.last_built is not None
        assert retrieved.age_seconds is not None
        assert retrieved.age_seconds < 5  # just written

    def test_get_missing_returns_none(self, tmp_path):
        store = FileSystemIndexStateStore(str(tmp_path))
        assert store.get_status("nonexistent", "repo") is None


class TestFormatDuration:
    def test_seconds(self):
        assert _format_duration(30) == "30s"

    def test_minutes(self):
        assert _format_duration(300) == "5m"

    def test_hours(self):
        assert _format_duration(7200) == "2.0h"

    def test_days(self):
        assert _format_duration(172800) == "2.0d"
