"""Mocked Batch lifecycle, exact input delivery and complete billing boundaries."""

import asyncio
import copy
import hashlib
import json

import httpx
import pytest

from scripts import contextbench_packet_batch as batch
from scripts import contextbench_packet_reader as reader


@pytest.fixture
def run(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.py").write_text("seen\nunseen\n")
    hashes = {"a.py": hashlib.sha256((source / "a.py").read_bytes()).hexdigest()}
    packet = {
        "instance_id": "case",
        "repo": str(source),
        "source_commit": "pinned",
        "issue": "Locate the missing behavior",
        "source_sha256": hashes,
        "contexts": {
            "native_packet": json.dumps([{"file": "a.py", "content": "1 | seen\n"}]),
            "late_physical4096": json.dumps(
                [{"file": "a.py", "content": "1 | seen\n2 | unseen\n"}]
            ),
        },
    }
    packets = tmp_path / "packets.json"
    packets.write_text(json.dumps({"rows": [packet]}))
    monkeypatch.setattr(reader.native, "audit_contextbench_snapshot", lambda *a: {})
    monkeypatch.setattr(reader.native, "freeze", lambda *a: {})
    state = {
        "packets": packets,
        "output": tmp_path / "run",
        "source": source,
        "requests": [],
        "records": None,
        "ended": True,
    }

    def message(custom_id):
        return {
            "id": "msg_" + custom_id,
            "model": reader.MODEL,
            "role": "assistant",
            "content": [{"type": "text", "text": "a.py:2"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1000, "output_tokens": 100},
        }

    def records():
        if state["records"] is not None:
            return state["records"]
        return [
            {
                "custom_id": item["custom_id"],
                "result": {"type": "succeeded", "message": message(item["custom_id"])},
            }
            for item in reversed(state["plan"]["items"])
        ]

    def handler(request):
        state["requests"].append(request)
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(
                200, json={"input_tokens": state.get("input_tokens", 1000)}
            )
        if request.method == "POST":
            return httpx.Response(
                200, json={"id": "msgbatch_known", "processing_status": "in_progress"}
            )
        if request.url.path.endswith("/results"):
            return httpx.Response(
                200, content="\n".join(json.dumps(r) for r in records())
            )
        counts = {
            k: 0 for k in ("processing", "succeeded", "errored", "canceled", "expired")
        }
        for record in records():
            kind = record.get("result", {}).get("type")
            if kind in counts:
                counts[kind] += 1
        return httpx.Response(
            200,
            json={
                "id": "msgbatch_known",
                "processing_status": "ended" if state["ended"] else "in_progress",
                "request_counts": state.get("counts", counts),
                "results_url": "https://untrusted.invalid/never-use-this-url",
            },
        )

    async def call(function, **kwargs):
        async with httpx.AsyncClient(
            base_url=reader.API_URL,
            transport=httpx.MockTransport(state.get("handler", handler)),
        ) as client:
            return await function(output=state["output"], client=client, **kwargs)

    state.update(
        call=call, handler_default=handler, message=message, records_default=records
    )
    state["plan"] = asyncio.run(
        call(batch.prepare_batch, packets=packets, arms=list(batch.ARMS), budget_usd=1)
    )
    return state


def finish(run):
    asyncio.run(run["call"](batch.submit_batch))
    return asyncio.run(run["call"](batch.collect_batch))


def test_shuffled_batch_matches_direct_prompt_predictions_and_half_cost(run):
    result = finish(run)
    assert result["status"] == "ok" and result["confirmed_inputs"] == 2
    assert result["api_usage_estimate_usd"] == pytest.approx(0.006)
    post = next(r for r in run["requests"] if r.url.path == batch.BATCH_PATH)
    sent = {r["custom_id"]: r["params"] for r in json.loads(post.content)["requests"]}
    for item in run["plan"]["items"]:
        direct = {"instance_id": item["instance_id"]}
        captured = []

        def handler(request, item=item, captured=captured):
            if request.url.path.endswith("count_tokens"):
                return httpx.Response(200, json={"input_tokens": 1000})
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=run["message"](item["custom_id"]))

        async def inspect(item=item, direct=direct, handler=handler):
            async with httpx.AsyncClient(
                base_url=reader.API_URL, transport=httpx.MockTransport(handler)
            ) as client:
                await reader.inspect_packet(
                    repo=run["source"],
                    commit="pinned",
                    context=item["context"],
                    source_hashes=item["source_sha256"],
                    issue=item["issue"],
                    row=direct,
                    checkpoint=lambda: None,
                    client=client,
                )

        asyncio.run(inspect())
        observed = next(
            r for r in result["rows"] if r["custom_id"] == item["custom_id"]
        )
        assert captured == [sent[item["custom_id"]]]
        assert observed["packet_prediction"] == direct["packet_prediction"]
        assert observed["api_usage_estimate_usd"] == pytest.approx(
            direct["api_usage_estimate_usd"] / 2
        )
    assert all(r.url.host == "api.anthropic.com" for r in run["requests"])


def test_submission_and_counting_alone_never_confirm_source(run):
    assert not (run["output"] / "submission-started.json").exists()
    assert all(not i["input_receipt"]["confirmed"] for i in run["plan"]["items"])
    run["ended"] = False
    result = finish(run)
    assert result["status"] == "pending" and result["confirmed_inputs"] == 0
    assert not (run["output"] / "result.json").exists()
    assert not any(r.url.path.endswith("/results") for r in run["requests"])


def test_concurrent_submitters_have_exactly_one_create_post(run):
    async def check():
        started, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            if request.method == "POST":
                started.set()
                await release.wait()
            return run["handler_default"](request)

        run["handler"] = handler
        first = asyncio.create_task(run["call"](batch.submit_batch))
        await started.wait()
        with pytest.raises(FileExistsError):
            await run["call"](batch.submit_batch)
        release.set()
        await first

    asyncio.run(check())
    assert sum(r.url.path == batch.BATCH_PATH for r in run["requests"]) == 1


def test_unacknowledged_submission_cannot_retry_or_assume_zero_bill(run):
    def handler(request):
        run["requests"].append(request)
        raise httpx.ReadTimeout("acknowledgement lost")

    run["handler"] = handler
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(run["call"](batch.submit_batch))
    assert (
        batch.load(run["output"] / "submission-started.json")["api_usage_estimate_usd"]
        is None
    )
    with pytest.raises(FileExistsError):
        asyncio.run(run["call"](batch.submit_batch))
    assert sum(r.url.path == batch.BATCH_PATH for r in run["requests"]) == 1


def test_acknowledged_response_recovers_after_checkpoint_failure_without_post(
    run, monkeypatch
):
    save = batch.save

    def fail(path, value):
        if path.name == "batch.json":
            raise OSError("ack checkpoint failure")
        return save(path, value)

    monkeypatch.setattr(batch, "save", fail)
    with pytest.raises(OSError):
        asyncio.run(run["call"](batch.submit_batch))
    result = asyncio.run(run["call"](batch.collect_batch))
    assert result["status"] == "ok"
    assert sum(r.url.path == batch.BATCH_PATH for r in run["requests"]) == 1


@pytest.mark.parametrize("kind", ["errored", "canceled", "expired"])
def test_unbilled_terminal_items_get_no_source_credit_or_retry(run, kind):
    records = copy.deepcopy(run["records_default"]())
    records[0]["result"] = {"type": kind, "error": {"type": "example"}}
    run["records"] = records
    result = finish(run)
    assert result["status"] == "failed" and result["confirmed_inputs"] == 1
    failed = next(r for r in result["rows"] if r["result"]["type"] == kind)
    assert failed["api_usage_estimate_usd"] == 0 and "packet_prediction" not in failed
    assert result["api_usage_estimate_usd"] == pytest.approx(0.003)
    assert sum(r.url.path == batch.BATCH_PATH for r in run["requests"]) == 1


@pytest.mark.parametrize(
    "failure", ["missing", "duplicate", "unknown", "same_message", "counts"]
)
def test_ambiguous_result_identity_retains_meter_without_predictions(run, failure):
    records = copy.deepcopy(run["records_default"]())
    if failure == "missing":
        records.pop()
    elif failure == "duplicate":
        records[1] = copy.deepcopy(records[0])
    elif failure == "unknown":
        records[0]["custom_id"] = "unknown"
    elif failure == "same_message":
        records[1]["result"]["message"]["id"] = records[0]["result"]["message"]["id"]
    else:
        run["counts"] = {
            "processing": 1,
            "succeeded": 1,
            "errored": 0,
            "canceled": 0,
            "expired": 0,
        }
    run["records"] = records
    with pytest.raises(ValueError, match="identity/usage"):
        finish(run)
    meter = batch.load(run["output"] / "meter.json")
    assert meter["api_usage_estimate_usd"] is None
    assert meter["observed_priced_usage_usd"] > 0
    assert all(
        not r["confirmed_input"] and "packet_prediction" not in r for r in meter["rows"]
    )


@pytest.mark.parametrize("failure", ["model", "cache", "geo", "tier", "usage"])
def test_unpriced_success_is_billing_unknown_with_raw_receipts(run, failure):
    records = copy.deepcopy(run["records_default"]())
    message = records[0]["result"]["message"]
    if failure == "model":
        message["model"] = "unknown-model"
    elif failure == "usage":
        message["usage"].pop("output_tokens")
    else:
        key, value = {
            "cache": ("cache_read_input_tokens", 1),
            "geo": ("inference_geo", "us"),
            "tier": ("service_tier", "priority"),
        }[failure]
        message["usage"][key] = value
    run["records"] = records
    with pytest.raises(ValueError, match="identity/usage"):
        finish(run)
    meter = batch.load(run["output"] / "meter.json")
    assert meter["api_usage_estimate_usd"] is None
    assert (run["output"] / "raw-results.jsonl").exists()


def test_source_changes_after_submission_keep_cost_but_no_credit(run):
    asyncio.run(run["call"](batch.submit_batch))
    (run["source"] / "a.py").write_text("changed\nunseen\n")
    result = asyncio.run(run["call"](batch.collect_batch))
    assert result["status"] == "failed" and result["confirmed_inputs"] == 0
    assert result["api_usage_estimate_usd"] == pytest.approx(0.006)
    assert all("packet_prediction" not in r for r in result["rows"])


def test_changed_plan_prevents_paid_submission(run):
    plan_path = run["output"] / "plan.json"
    plan_path.write_text(plan_path.read_text() + " ")
    with pytest.raises(ValueError, match="plan changed"):
        asyncio.run(run["call"](batch.submit_batch))
    assert not (run["output"] / "submission-started.json").exists()


def test_capped_thinking_reply_confirms_input_not_completed_reasoning(run):
    records = copy.deepcopy(run["records_default"]())
    message = records[0]["result"]["message"]
    message.update(
        stop_reason="max_tokens",
        content=[{"type": "thinking", "thinking": "", "signature": "opaque"}],
    )
    run["records"] = records
    result = finish(run)
    assert result["status"] == "ok" and result["confirmed_inputs"] == 2
    assert sum(r["response_complete"] for r in result["rows"]) == 1


def test_whole_batch_reservation_failure_never_creates_paid_request(run, tmp_path):
    run["output"] = tmp_path / "too-small"
    with pytest.raises(ValueError, match="whole reservation"):
        asyncio.run(
            run["call"](
                batch.prepare_batch,
                packets=run["packets"],
                arms=list(batch.ARMS),
                budget_usd=0.001,
            )
        )
    assert not any(r.url.path == batch.BATCH_PATH for r in run["requests"])


def test_malformed_results_retain_known_sibling_meter_and_raw_bytes(run):
    ordinary = run["handler_default"]

    def handler(request):
        if request.url.path.endswith("/results"):
            good = run["records_default"]()[0]
            return httpx.Response(200, content="not-json\n" + json.dumps(good))
        return ordinary(request)

    run["handler"] = handler
    with pytest.raises(ValueError, match="identity/usage"):
        finish(run)
    meter = batch.load(run["output"] / "meter.json")
    assert meter["api_usage_estimate_usd"] is None
    assert meter["observed_priced_usage_usd"] == pytest.approx(0.003)
    assert (run["output"] / "raw-results.jsonl").read_text().startswith("not-json")


def test_runtime_mismatch_cannot_erase_terminal_meter_or_publish_credit(
    run, monkeypatch
):
    asyncio.run(run["call"](batch.submit_batch))

    def fail(*args):
        raise ValueError("frozen runtime changed")

    monkeypatch.setattr(batch, "verify_runtime", fail)
    with pytest.raises(ValueError, match="runtime changed"):
        asyncio.run(run["call"](batch.collect_batch))
    meter = batch.load(run["output"] / "meter.json")
    assert meter["api_usage_estimate_usd"] == pytest.approx(0.006)
    assert not (run["output"] / "result.json").exists()


def test_swapped_batch_identity_cannot_query_another_batch(run):
    asyncio.run(run["call"](batch.submit_batch))
    batch.save(run["output"] / "batch.json", {"id": "msgbatch_other"})
    with pytest.raises(ValueError, match="create response"):
        asyncio.run(run["call"](batch.collect_batch))
    assert not any(r.url.path.endswith("msgbatch_other") for r in run["requests"])


def test_changed_input_during_free_counts_prevents_prepared_plan(run, tmp_path):
    run["output"] = tmp_path / "input-changed"
    ordinary = run["handler_default"]

    def handler(request):
        if request.url.path.endswith("count_tokens"):
            run["packets"].write_text(run["packets"].read_text() + " ")
        return ordinary(request)

    run["handler"] = handler
    with pytest.raises(ValueError, match="input changed"):
        asyncio.run(
            run["call"](
                batch.prepare_batch,
                packets=run["packets"],
                arms=list(batch.ARMS),
                budget_usd=1,
            )
        )
    assert not (run["output"] / "prepared.json").exists()
    assert not any(r.url.path == batch.BATCH_PATH for r in run["requests"])


@pytest.mark.parametrize("failure", ["tool", "output_cap", "stop"])
def test_nonconforming_messages_keep_known_cost_without_input_credit(run, failure):
    records = copy.deepcopy(run["records_default"]())
    for record in records:
        message = record["result"]["message"]
        if failure == "tool":
            message["content"] = [{"type": "tool_use", "name": "bash"}]
        elif failure == "output_cap":
            message["usage"]["output_tokens"] = 2049
        else:
            message["stop_reason"] = "tool_use"
    run["records"] = records
    result = finish(run)
    assert result["status"] == "failed" and result["confirmed_inputs"] == 0
    assert result["api_usage_estimate_usd"] > 0
    assert all("packet_prediction" not in r for r in result["rows"])
