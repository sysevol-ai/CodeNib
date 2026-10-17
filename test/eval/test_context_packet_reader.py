"""Exact source confirmation and billing boundaries through mock HTTP, never API."""

import asyncio
import copy
import hashlib
import json

import httpx
import pytest

from scripts import contextbench_packet_reader as reader


def packet(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("visible\nunseen\n")
    return json.dumps([{"file": "a.py", "content": "2 | unseen\n"}]), {
        "a.py": hashlib.sha256(path.read_bytes()).hexdigest()
    }


def message(**overrides):
    return {
        "id": "response",
        "role": "assistant",
        "model": reader.MODEL,
        "content": [{"type": "text", "text": "a.py:2"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1000, "output_tokens": 100},
        **overrides,
    }


def run(tmp_path, monkeypatch, handler, row=None, checkpoint=lambda: None, **kwargs):
    context, hashes = packet(tmp_path)
    context = kwargs.pop("context_override", context)
    monkeypatch.setattr(reader.native, "audit_contextbench_snapshot", lambda *a: {})
    if row is None:
        row = {"instance_id": "case"}

    async def inspect():
        async with httpx.AsyncClient(
            base_url=reader.API_URL, transport=httpx.MockTransport(handler)
        ) as client:
            await reader.inspect_packet(
                repo=tmp_path,
                commit="pinned",
                context=context,
                source_hashes=hashes,
                issue="Find the missing behavior",
                row=row,
                checkpoint=checkpoint,
                client=client,
                **kwargs,
            )

    asyncio.run(inspect())
    return row


def test_real_http_shapes_match_exact_recorded_source_before_credit(
    tmp_path, monkeypatch
):
    row = {"instance_id": "case"}
    requests, saved = [], []

    def handler(request):
        data = json.loads(request.content)
        requests.append(data)
        assert str(request.url).startswith(reader.API_URL)
        assert data["messages"][0] == row["messages"][0]
        assert (
            reader.text_hash(data["messages"][0]["content"])
            == row["input_receipt"]["prompt_sha256"]
        )
        assert not row["input_receipt"]["confirmed"] and "packet_prediction" not in row
        assert data["tools"] == [] and data["thinking"] == {"type": "adaptive"}
        if request.url.path.endswith("count_tokens"):
            assert not row["api_attempted"] and row["api_usage_estimate_usd"] == 0
            return httpx.Response(200, json={"input_tokens": 1000})
        assert data["max_tokens"] == 2048 and data["output_config"] == {
            "effort": "medium"
        }
        assert (
            data["service_tier"] == "standard_only"
            and data["inference_geo"] == "global"
        )
        assert row["paid_message_requests"] == 1
        return httpx.Response(200, json=message())

    run(
        tmp_path,
        monkeypatch,
        handler,
        row,
        checkpoint=lambda: saved.append(copy.deepcopy(row)),
    )
    assert len(requests) == 2 and requests[0]["messages"] == requests[1]["messages"]
    assert row["status"] == "ok" and row["input_receipt"]["confirmed"]
    assert row["api_usage_estimate_usd"] == pytest.approx(0.006)
    assert row["packet_prediction"]["traj_data"]["pred_spans"] == {
        "a.py": [{"start": 2, "end": 2}]
    }
    assert all("packet_prediction" not in item for item in saved[:-1])


def test_count_only_cannot_confirm_input_or_spend_paid_budget(tmp_path, monkeypatch):
    calls = []
    row = {
        "instance_id": "case",
        "packet_prediction": {"stale": True},
        "input_receipt": {"confirmed": True},
        "actual_models": ["stale"],
    }

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"input_tokens": 200000})

    with pytest.raises(ValueError, match="reserved request budget"):
        run(tmp_path, monkeypatch, handler, row)
    assert calls == ["/v1/messages/count_tokens"]
    assert not row["api_attempted"] and row["api_usage_estimate_usd"] == 0
    assert not row["input_receipt"]["confirmed"] and "packet_prediction" not in row
    assert "actual_models" not in row


def test_source_change_after_count_prevents_paid_dispatch(tmp_path, monkeypatch):
    calls = []
    row = {"instance_id": "case"}

    def handler(request):
        calls.append(request.url.path)
        (tmp_path / "a.py").write_text("changed\nunseen\n")
        return httpx.Response(200, json={"input_tokens": 1000})

    with pytest.raises(ValueError, match="version changed"):
        run(tmp_path, monkeypatch, handler, row)
    assert calls == ["/v1/messages/count_tokens"] and not row["api_attempted"]


@pytest.mark.parametrize("failure", ["source", "tool", "stop", "output_cap"])
def test_post_response_failures_preserve_known_usage_without_credit(
    tmp_path, monkeypatch, failure
):
    row = {"instance_id": "case"}

    def handler(request):
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 1000})
        data = message()
        if failure == "source":
            (tmp_path / "a.py").write_text("changed\nunseen\n")
        if failure == "tool":
            data["content"] = [{"type": "tool_use", "name": "bash"}]
        if failure == "stop":
            data["stop_reason"] = "tool_use"
        if failure == "output_cap":
            data["usage"]["output_tokens"] = 2049
        return httpx.Response(200, json=data)

    with pytest.raises(ValueError):
        run(tmp_path, monkeypatch, handler, row)
    assert (
        row["response"]["usage"] == row["usage"] and row["api_usage_estimate_usd"] > 0
    )
    assert "packet_prediction" not in row


@pytest.mark.parametrize("failure", ["model", "usage", "caching", "geo"])
def test_unpriced_response_retains_usage_and_stops_credit(
    tmp_path, monkeypatch, failure
):
    row = {"instance_id": "case"}

    def handler(request):
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 1000})
        data = message()
        if failure == "model":
            data["model"] = "unexpected-model"
        if failure == "usage":
            data["usage"].pop("output_tokens")
        if failure == "caching":
            data["usage"]["cache_creation_input_tokens"] = 100
        if failure == "geo":
            data["usage"]["inference_geo"] = "us"
        return httpx.Response(200, json=data)

    with pytest.raises(ValueError):
        run(tmp_path, monkeypatch, handler, row)
    assert row["api_usage_estimate_usd"] is None and row["usage"]
    assert "packet_prediction" not in row


def test_capped_response_is_confirmed_source_not_complete_answer(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 1000})
        return httpx.Response(
            200,
            json=message(
                stop_reason="max_tokens",
                content=[{"type": "thinking", "thinking": "", "signature": "opaque"}],
            ),
        )

    row = run(tmp_path, monkeypatch, handler)
    assert row["status"] == "ok" and row["input_receipt"]["confirmed"]
    assert not row["response_complete"] and row["observed_thinking_blocks"] == 1
    assert len(calls) == 2 and row["paid_message_requests"] == 1


def test_http_error_has_no_retry_and_keeps_usage_unknown(tmp_path, monkeypatch):
    calls = []
    row = {"instance_id": "case"}

    def handler(request):
        calls.append(request.url.path)
        return (
            httpx.Response(200, json={"input_tokens": 1000})
            if request.url.path.endswith("count_tokens")
            else httpx.Response(429, json={"error": {"type": "rate_limit_error"}})
        )

    with pytest.raises(httpx.HTTPStatusError):
        run(tmp_path, monkeypatch, handler, row)
    assert len(calls) == 2 and row["api_usage_estimate_usd"] is None
    assert not row["input_receipt"]["confirmed"] and "packet_prediction" not in row


@pytest.mark.parametrize("content", ["2 | uns", "2 | unseen\n3 | invented\n", "unseen"])
def test_partial_or_fabricated_source_never_reaches_http(
    tmp_path, monkeypatch, content
):
    row = {"instance_id": "case"}

    def handler(request):
        pytest.fail("Invalid source must never be dispatched")

    with pytest.raises(ValueError):
        run(
            tmp_path,
            monkeypatch,
            handler,
            row,
            context_override=json.dumps([{"file": "a.py", "content": content}]),
        )
    assert not row["api_attempted"] and row["api_http_requests"] == 0
    assert "packet_prediction" not in row


def test_post_response_checkpoint_failure_keeps_terminal_meter(tmp_path, monkeypatch):
    row = {"instance_id": "case"}

    def handler(request):
        data = (
            {"input_tokens": 1000}
            if request.url.path.endswith("count_tokens")
            else message()
        )
        return httpx.Response(200, json=data)

    def checkpoint():
        if "response" in row:
            raise OSError("checkpoint failed after terminal response")

    with pytest.raises(OSError, match="after terminal"):
        run(tmp_path, monkeypatch, handler, row, checkpoint=checkpoint)
    assert row["api_usage_estimate_usd"] == pytest.approx(0.006)
    assert row["usage"] and "packet_prediction" not in row
