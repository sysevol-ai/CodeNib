# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the layered parameter resolution system."""

from __future__ import annotations

import json

from codeminer.compiler.params import (
    DefaultSessionRules,
    ResolvedParams,
    SessionContext,
    resolve_params,
)


class TestResolveParams:
    def test_defaults_only(self):
        result = resolve_params(defaults={"top_k": 20, "level": "l2"})
        assert result.params == {"top_k": 20, "level": "l2"}
        assert result.provenance["top_k"] == "defaults"
        assert result.provenance["level"] == "defaults"

    def test_query_params_override_defaults(self):
        result = resolve_params(
            defaults={"top_k": 20, "level": "l2"},
            query_params={"top_k": 50},
        )
        assert result.params["top_k"] == 50
        assert result.params["level"] == "l2"
        assert result.provenance["top_k"] == "query"
        assert result.provenance["level"] == "defaults"

    def test_query_params_add_new_keys(self):
        result = resolve_params(
            defaults={"top_k": 20},
            query_params={"query": "hello", "top_k": 10},
        )
        assert result.params == {"top_k": 10, "query": "hello"}
        assert result.provenance["query"] == "query"

    def test_session_adjusts_params(self):
        ctx = SessionContext(repo_size=20_000)
        result = resolve_params(
            defaults={"top_k": 20},
            session_ctx=ctx,
        )
        # DefaultSessionRules doubles top_k for repos > 10k files
        assert result.params["top_k"] == 40
        assert result.provenance["top_k"] == "session"

    def test_session_no_adjustment_for_small_repo(self):
        ctx = SessionContext(repo_size=500)
        result = resolve_params(
            defaults={"top_k": 20},
            session_ctx=ctx,
        )
        assert result.params["top_k"] == 20
        assert result.provenance["top_k"] == "defaults"

    def test_query_params_override_session(self):
        ctx = SessionContext(repo_size=20_000)
        result = resolve_params(
            defaults={"top_k": 20},
            session_ctx=ctx,
            query_params={"top_k": 5},
        )
        # query overrides session adjustment
        assert result.params["top_k"] == 5
        assert result.provenance["top_k"] == "query"

    def test_empty_layers(self):
        result = resolve_params(defaults={})
        assert result.params == {}
        assert result.provenance == {}

    def test_none_session_and_query(self):
        result = resolve_params(
            defaults={"x": 1},
            session_ctx=None,
            query_params=None,
        )
        assert result.params == {"x": 1}

    def test_custom_session_rules(self):
        class LanguageRules:
            def adjust(self, base_params, session):
                adjusted = dict(base_params)
                if session.primary_language == "java":
                    adjusted["top_k"] = adjusted.get("top_k", 10) * 3
                return adjusted

        ctx = SessionContext(primary_language="java")
        result = resolve_params(
            defaults={"top_k": 10},
            session_ctx=ctx,
            session_rules=LanguageRules(),
        )
        assert result.params["top_k"] == 30
        assert result.provenance["top_k"] == "session"

    def test_provenance_tracking_all_layers(self):
        ctx = SessionContext(repo_size=20_000)
        result = resolve_params(
            defaults={"top_k": 20, "level": "l2", "filter_test": False},
            session_ctx=ctx,
            query_params={"level": "l0", "query": "find me"},
        )
        assert result.provenance["top_k"] == "session"  # doubled by session
        assert result.provenance["level"] == "query"  # overridden by query
        assert result.provenance["filter_test"] == "defaults"  # untouched
        assert result.provenance["query"] == "query"  # new from query


class TestResolvedParamsSerialisation:
    def test_to_dict(self):
        rp = ResolvedParams(
            params={"top_k": 20, "level": "l2"},
            provenance={"top_k": "defaults", "level": "query"},
        )
        d = rp.to_dict()
        assert json.dumps(d)  # must be JSON-serialisable
        assert d["params"]["top_k"] == 20
        assert d["provenance"]["level"] == "query"


class TestSessionContext:
    def test_to_dict(self):
        ctx = SessionContext(
            repo_path="/tmp/test",
            repo_size=1000,
            primary_language="python",
        )
        d = ctx.to_dict()
        assert json.dumps(d)
        assert d["repo_size"] == 1000
        assert d["primary_language"] == "python"


class TestDefaultSessionRules:
    def test_doubles_top_k_for_large_repo(self):
        rules = DefaultSessionRules()
        ctx = SessionContext(repo_size=15_000)
        result = rules.adjust({"top_k": 10, "level": "l2"}, ctx)
        assert result["top_k"] == 20
        assert result["level"] == "l2"  # unchanged

    def test_no_change_for_small_repo(self):
        rules = DefaultSessionRules()
        ctx = SessionContext(repo_size=100)
        result = rules.adjust({"top_k": 10}, ctx)
        assert result["top_k"] == 10

    def test_no_change_without_top_k(self):
        rules = DefaultSessionRules()
        ctx = SessionContext(repo_size=20_000)
        result = rules.adjust({"level": "l2"}, ctx)
        assert result == {"level": "l2"}

    def test_no_change_without_repo_size(self):
        rules = DefaultSessionRules()
        ctx = SessionContext()
        result = rules.adjust({"top_k": 10}, ctx)
        assert result["top_k"] == 10
