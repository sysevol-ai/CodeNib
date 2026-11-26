# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codenib.llm.litellm_chat import LiteLLMChat
from codenib.llm.usage import (
    UsageTracker,
    current_wiki_page_context,
    format_usage_markdown,
    format_usage_summary,
)


def _make_response(
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    cost: float | None = None,
) -> SimpleNamespace:
    usage_dict = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if cost is not None:
        usage_dict["cost"] = cost
    usage = SimpleNamespace(**usage_dict)
    return SimpleNamespace(
        usage=usage,
        choices=[SimpleNamespace(message=SimpleNamespace(content="test answer"))],
    )


class TestUsageTrackerThreadSafety:
    def test_concurrent_recording(self):
        tracker = UsageTracker()
        num_threads = 8
        calls_per_thread = 50

        def worker(thread_id: int):
            for i in range(calls_per_thread):
                token = current_wiki_page_context.set(f"page-{thread_id}")
                try:
                    if i % 5 == 0:
                        tracker.record_failure(
                            model="test-model",
                            duration_ms=10.0,
                            turn=i,
                            error="temporary failure",
                        )
                    else:
                        resp = _make_response(prompt_tokens=10, completion_tokens=5)
                        tracker.record_response(
                            resp,
                            model="test-model",
                            duration_ms=20.0,
                            turn=i,
                        )
                finally:
                    current_wiki_page_context.reset(token)

        threads = [
            threading.Thread(target=worker, args=(t,)) for t in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expected_failures = num_threads * 10
        expected_successes = num_threads * 40
        assert len(tracker.failures) == expected_failures
        assert len(tracker.records) == expected_successes

        totals = tracker.totals()
        assert totals.prompt_tokens == expected_successes * 10
        assert totals.completion_tokens == expected_successes * 5
        assert totals.total_tokens == expected_successes * 15

        summ = tracker.summary()
        assert summ["calls"] == expected_successes
        assert summ["failed_calls"] == expected_failures
        assert len(summ["by_page"]) == num_threads


class TestPricingFallbackChain:
    def test_billed_cost_from_response_preferred(self):
        tracker = UsageTracker()
        resp = _make_response(prompt_tokens=1000, completion_tokens=500, cost=0.042)
        rec = tracker.record_response(resp, model="openrouter/deepseek/deepseek-v4.1-flash")

        assert rec.usage.cost_usd == pytest.approx(0.042)
        assert rec.cost_provenance["kind"] == "billed_cost"
        assert rec.cost_provenance["calculator"] == "response.usage.cost"

    def test_litellm_completion_cost_fallback(self):
        tracker = UsageTracker()
        resp = _make_response(prompt_tokens=1000, completion_tokens=500)
        with patch("litellm.completion_cost", return_value=0.015):
            rec = tracker.record_response(resp, model="custom-provider/custom-model")

        assert rec.usage.cost_usd == pytest.approx(0.015)
        assert rec.cost_provenance["kind"] == "runtime_estimate"
        assert rec.cost_provenance["calculator"] == "litellm.completion_cost"

    def test_visitor_models_catalog_fallback_for_deepseek_flash(self):
        tracker = UsageTracker()
        # 1,000,000 in, 1,000,000 out
        resp = _make_response(prompt_tokens=1_000_000, completion_tokens=1_000_000)
        # Force litellm.completion_cost to fail (unpriced in LiteLLM)
        with patch("litellm.completion_cost", side_effect=Exception("unpriced model")):
            rec = tracker.record_response(
                resp, model="openrouter/deepseek/deepseek-v4.1-flash"
            )

        # In WIKI_MODEL_CHOICES: deepseek-v4.1-flash is input_usd=0.051, output_usd=0.567 per million
        expected = 0.051 + 0.567
        assert rec.usage.cost_usd == pytest.approx(expected)
        assert rec.cost_provenance["kind"] == "catalog_fallback"
        assert "WIKI_MODEL_CHOICES" in rec.cost_provenance["calculator"]

    def test_unpriced_unknown_model_returns_none(self):
        tracker = UsageTracker()
        resp = _make_response(prompt_tokens=50, completion_tokens=50)
        with patch("litellm.completion_cost", side_effect=Exception("unknown")):
            rec = tracker.record_response(resp, model="unknown-org/completely-unknown-model")

        assert rec.usage.cost_usd is None
        assert rec.cost_provenance["kind"] == "unavailable"


class TestContextVarPageTagging:
    def test_context_var_automatic_tagging(self):
        tracker = UsageTracker()
        resp = _make_response(10, 10)

        token = current_wiki_page_context.set("overview")
        try:
            rec = tracker.record_response(resp, model="test-model")
            fail = tracker.record_failure(model="test-model", error="err")
        finally:
            current_wiki_page_context.reset(token)

        assert rec.page == "overview"
        assert fail.page == "overview"

    def test_explicit_page_overrides_context_var(self):
        tracker = UsageTracker()
        resp = _make_response(10, 10)

        token = current_wiki_page_context.set("default-page")
        try:
            rec = tracker.record_response(resp, model="test-model", page="explicit-page")
        finally:
            current_wiki_page_context.reset(token)

        assert rec.page == "explicit-page"


class TestLiteLLMChatTrackerIntegration:
    def test_default_usage_tracker_on_chat(self):
        tracker = UsageTracker()
        chat = LiteLLMChat(model="gpt-4o", usage_tracker=tracker)

        fake_resp = _make_response(100, 50)
        with patch("litellm.completion", return_value=fake_resp):
            with patch("litellm.completion_cost", return_value=0.001):
                chat.complete([{"role": "user", "content": "hello"}])

        assert len(tracker.records) == 1
        assert tracker.records[0].usage.total_tokens == 150

    def test_failure_recorded_on_exception(self):
        tracker = UsageTracker()
        chat = LiteLLMChat(model="gpt-4o", usage_tracker=tracker)

        with patch("litellm.completion", side_effect=RuntimeError("connection dropped")):
            with pytest.raises(RuntimeError, match="connection dropped"):
                chat.complete([{"role": "user", "content": "hello"}])

        assert len(tracker.failures) == 1
        assert "connection dropped" in tracker.failures[0].error


class TestSummaryAndFormatting:
    def test_summary_structure_and_formatting(self):
        tracker = UsageTracker()
        resp1 = _make_response(300_000, 50_000, cost=0.075)
        resp2 = _make_response(98_214, 11_530, cost=0.016)

        tracker.record_response(
            resp1, model="openrouter/deepseek/deepseek-v4.1-flash", page="page-1"
        )
        tracker.record_response(
            resp2, model="openrouter/deepseek/deepseek-v4.1-flash", page="page-failed"
        )
        tracker.record_failure(
            model="openrouter/deepseek/deepseek-v4.1-flash",
            page="page-failed",
            error="Rate limit",
        )

        summ = tracker.summary(
            wall_clock_seconds=558.0,  # 9m 18s
            rewritten_pages=5,
            total_pages=32,
            left_out_pages=["page-failed"],
        )

        assert summ["calls"] == 2
        assert summ["failed_calls"] == 1
        assert summ["prompt_tokens"] == 398_214
        assert summ["completion_tokens"] == 61_530
        assert summ["cost_usd"] == pytest.approx(0.091)
        assert summ["wall_clock_seconds"] == 558.0
        assert summ["rewritten_pages"] == 5
        assert summ["total_pages"] == 32
        assert summ["left_out"]["count"] == 1
        assert summ["left_out"]["calls"] == 1
        assert summ["left_out"]["cost_usd"] == pytest.approx(0.016)

        # Test terminal formatting
        text = format_usage_summary(summ)
        assert "Model usage: openrouter/deepseek/deepseek-v4.1-flash" in text
        assert "Calls:     2 (1 failed)" in text
        assert "Tokens:    398,214 in, 61,530 out" in text
        assert "Cost:      $0.091" in text
        assert "Time:      9m 18s writing pages" in text
        assert "Pages:     rewrote 5 of 32" in text
        assert "Left out:  1 calls, $0.016 on 1 unpublished page" in text

        # Test markdown formatting
        md = format_usage_markdown(summ)
        assert "### CodeNib Wiki Build Usage" in md
        assert "`openrouter/deepseek/deepseek-v4.1-flash`" in md
        assert "9m 18s writing pages" in md
        assert "Rewrote 5 of 32 pages" in md
        assert "1 calls, $0.016 across 1 unpublished pages" in md


class TestCLIUsageJsonExport:
    def test_cli_usage_json_flag(self, tmp_path):
        from codenib.cli import _audit_local_wiki

        tracker = UsageTracker()
        tracker.record_response(
            _make_response(50, 25, cost=0.005),
            model="test-model",
            page="overview",
        )

        mock_builder = MagicMock()
        mock_builder._llm.usage_tracker = tracker
        mock_bundle = MagicMock()
        mock_bundle.entry.repo = "owner/repo"
        mock_bundle.entry.base_commit = "123456"

        with patch("codenib.wiki.quality.audit_wiki", return_value={
            "expected_pages": 1,
            "generated_pages": 1,
            "ready_pages": 1,
            "details": [{"id": "overview", "ready": True}],
            "wall_clock_seconds": 12.5,
        }):
            report = _audit_local_wiki(
                MagicMock(),
                builder_parts=(mock_bundle, "test-model", mock_builder),
            )

        assert "usage" in report
        assert report["usage"]["calls"] == 1
        assert report["usage"]["cost_usd"] == 0.005

        out_file = tmp_path / "usage.json"
        out_file.write_text(json.dumps(report["usage"]), encoding="utf-8")
        loaded = json.loads(out_file.read_text(encoding="utf-8"))
        assert loaded["calls"] == 1
        assert loaded["model"] == "test-model"
