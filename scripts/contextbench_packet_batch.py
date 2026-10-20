# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Source-checkout Batch transport for the fixed ContextBench packet reviewer.

One owned run directory permits at most one paid batch-create POST. Exclusive
creation of submission-started.json is the linearization point, before network
dispatch; its intent is never removed. Concurrent or resumed submitters cannot
create another batch. An unacknowledged POST remains billing-unknown and requires
operator reconciliation, never automatic resubmission. A known batch ID permits
only read-only status/results recovery. No locks or product persistence exist.

The operator owns the run and its pinned checkouts through result collection.
Raw terminal results and metering are saved before source validation/prediction.
Only matched, successful assistant inputs with unchanged source/runtime receive
credit. Expired/canceled/error items remain in the results without retry.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import contextbench_packet_reader as reader
from scripts.benchmark_jev_base import digest

ARMS = ("native_packet", "late_physical4096")
PRICING = {name: price * 0.5 for name, price in reader.PRICING.items()}
OUTPUT_TOKENS = 2048
REQUEST_BUDGET_USD = 0.25
BATCH_PATH = "/v1/messages/batches"


def save(path, value):
    """Publish a complete local checkpoint; retain immutable intent separately."""
    data = value if isinstance(value, bytes) else json.dumps(value, indent=2).encode()
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load(path):
    return json.loads(path.read_text())


def check_client(client):
    if str(client.base_url).rstrip("/") != reader.API_URL or client.follow_redirects:
        raise ValueError("Only the first-party Batch endpoint qualifies")


def request_for(item):
    prompt = (
        f"Issue:\n{item['issue']}\n\nNumbered source packet (JSON):\n{item['context']}"
    )
    return {
        "model": reader.MODEL,
        "system": reader.SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [],
        "thinking": {"type": "adaptive"},
        "max_tokens": OUTPUT_TOKENS,
        "output_config": {"effort": "medium"},
        "service_tier": "standard_only",
        "inference_geo": "global",
    }


def verify_source(item):
    repo = Path(item["repo"])
    reader.native.audit_contextbench_snapshot(repo, item["source_commit"])
    return reader.validate_packet(repo, item["context"], item["source_sha256"])


def read_plan(output):
    prepared = load(output / "prepared.json")
    if digest(output / "plan.json") != prepared["plan_sha256"]:
        raise ValueError("Prepared Batch plan changed")
    plan = load(output / "plan.json")
    if plan["pricing"] != PRICING or plan["model"] != reader.MODEL:
        raise ValueError("Batch price/model profile changed")
    for item in plan["items"]:
        if item["request"] != request_for(item):
            raise ValueError("Prepared Batch request differs from fixed profile")
    return plan, prepared


def verify_runtime(output, prepared):
    for name, sha in prepared["implementation"].items():
        if (
            digest(ROOT / name) != sha
            or digest(output / "implementation" / name) != sha
        ):
            raise ValueError("Batch runtime differs from its frozen source")


async def prepare_batch(*, packets, arms, output, budget_usd, client):
    """Free exact-input counts and source validation; no paid batch creation."""
    check_client(client)
    if not math.isfinite(budget_usd) or budget_usd <= 0:
        raise ValueError("Batch reservation must be finite and positive")
    if not arms or len(set(arms)) != len(arms) or set(arms) - set(ARMS):
        raise ValueError("Unknown or repeated packet arm")
    packets_sha256 = digest(packets)
    rows = load(packets)["rows"]
    if not rows or len(rows) * len(arms) > 1000:
        raise ValueError("Batch must contain 1 to 1000 packet requests")
    if len({r["instance_id"] for r in rows}) != len(rows):
        raise ValueError("Repeated packet instance")
    output.mkdir(parents=True, exist_ok=False)
    implementation = reader.native.freeze(output)
    for name in (
        "contextbench_packet_reader.py",
        "contextbench_packet_batch.py",
        "benchmark_jev_base.py",
    ):
        relative = "scripts/" + name
        target = output / "implementation" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
        implementation[relative] = digest(ROOT / relative)
    items = []
    for row in rows:
        for arm in arms:
            item = {
                **{
                    k: row[k] for k in ("instance_id", "repo", "source_commit", "issue")
                },
                "source_sha256": dict(row["source_sha256"]),
                "context": row["contexts"][arm],
                "arm": arm,
            }
            item["custom_id"] = (
                "pkt_" + reader.text_hash(item["instance_id"] + "\0" + arm)[:40]
            )
            geometry = verify_source(item)
            item["request"] = request_for(item)
            common = {
                k: item["request"][k]
                for k in ("model", "system", "messages", "tools", "thinking")
            }
            response = await client.post("/v1/messages/count_tokens", json=common)
            response.raise_for_status()
            count = response.json()
            tokens = count.get("input_tokens")
            if type(tokens) is not int or tokens < 0:
                raise ValueError("Invalid Batch input count")
            margin = max(64, math.ceil(tokens * 0.02))
            forecast = (
                (tokens + margin) * PRICING["input_per_million"]
                + OUTPUT_TOKENS * PRICING["output_per_million"]
            ) / 1_000_000
            if forecast > REQUEST_BUDGET_USD:
                raise ValueError("Packet exceeds the Batch request reservation")
            item.update(
                input_receipt={
                    **geometry,
                    "context_sha256": reader.text_hash(item["context"]),
                    "prompt_sha256": reader.text_hash(
                        item["request"]["messages"][0]["content"]
                    ),
                    "source_sha256": item["source_sha256"],
                    "confirmed": False,
                },
                count_tokens_response=count,
                estimated_max_usd_with_margin=forecast,
            )
            verify_source(item)
            items.append(item)
    forecast = sum(item["estimated_max_usd_with_margin"] for item in items)
    reservation = forecast + max(0.05, forecast * 0.1)
    if reservation > budget_usd:
        raise ValueError("Batch inputs plus capped outputs exceed whole reservation")
    plan = {
        "model": reader.MODEL,
        "pricing": PRICING,
        "budget_usd": budget_usd,
        "estimated_max_usd_with_margin": forecast,
        "reserved_with_headroom_usd": reservation,
        "packets_sha256": packets_sha256,
        "items": items,
    }
    if len(json.dumps(payload_for(plan)).encode()) > 256 * 1024 * 1024:
        raise ValueError("Batch exceeds provider payload limit")
    if digest(packets) != packets_sha256:
        raise ValueError("Packet input changed during Batch preparation")
    save(output / "plan.json", plan)
    save(
        output / "prepared.json",
        {
            "plan_sha256": digest(output / "plan.json"),
            "implementation": implementation,
            "endpoint": reader.API_URL,
            "httpx_version": importlib.metadata.version("httpx"),
            "tiktoken_version": importlib.metadata.version("tiktoken"),
            "http_retries": 0,
            "follow_redirects": False,
            "free_count_requests": len(items),
            "paid_message_requests": 0,
            "api_usage_estimate_usd": 0.0,
        },
    )
    return plan


def payload_for(plan):
    return {
        "requests": [
            {"custom_id": i["custom_id"], "params": i["request"]} for i in plan["items"]
        ]
    }


async def submit_batch(*, output, client):
    check_client(client)
    plan, prepared = read_plan(output)
    verify_runtime(output, prepared)
    for item in plan["items"]:
        verify_source(item)
    # Atomic exclusive intent survives ambiguous network/checkpoint outcomes.
    intent = output / "submission-started.json"
    with intent.open("x") as stream:
        json.dump(
            {
                "plan_sha256": prepared["plan_sha256"],
                "status": "submission_unknown_until_acknowledged",
                "api_usage_estimate_usd": None,
            },
            stream,
        )
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    response = await client.post(BATCH_PATH, json=payload_for(plan))
    save(
        output / "submission-response.json",
        {"http_status": response.status_code, "body": response.text},
    )
    response.raise_for_status()
    batch = response.json()
    if not re.fullmatch(r"msgbatch_[A-Za-z0-9]+", batch.get("id", "")):
        raise ValueError("No acknowledged Batch identity; billing remains unknown")
    save(output / "batch.json", batch)
    return batch


async def batch_status(*, output, client):
    check_client(client)
    response = load(output / "submission-response.json")
    if response["http_status"] != 200:
        raise ValueError("No recoverable acknowledged Batch response")
    acknowledged = json.loads(response["body"])
    path = output / "batch.json"
    if path.exists():
        batch = load(path)
        if batch.get("id") != acknowledged.get("id"):
            raise ValueError("Batch identity differs from the create response")
    else:
        # Recover an acknowledged response if its later checkpoint failed.
        batch = acknowledged
    batch_id = batch["id"]
    if not re.fullmatch(r"msgbatch_[A-Za-z0-9]+", batch_id):
        raise ValueError("Invalid acknowledged Batch identity")
    response = await client.get(f"{BATCH_PATH}/{batch_id}")
    response.raise_for_status()
    current = response.json()
    if current.get("id") != batch_id:
        raise ValueError("Batch status identity differs")
    save(output / "status.json", current)
    return current


def batch_usage_cost(message):
    """Discount only a result retrieved via the acknowledged Batch transport."""
    if not isinstance(message, dict) or message.get("model") != reader.MODEL:
        raise ValueError("Unknown Batch model price")
    usage = message.get("usage", {})
    if not isinstance(usage, dict) or not isinstance(
        usage.get("server_tool_use") or {}, dict
    ):
        raise ValueError("Invalid Batch usage object")
    usage = dict(usage)
    if usage.get("service_tier") not in (None, "standard", "batch"):
        raise ValueError("Unexpected Batch service tier")
    usage.pop("service_tier", None)
    return reader.usage_cost(usage) * 0.5


def confirm(item, message):
    content = message.get("content")
    if (
        message.get("role") != "assistant"
        or not message.get("id")
        or not isinstance(content, list)
        or not content
        or any(
            not isinstance(b, dict)
            or b.get("type") not in {"text", "thinking", "redacted_thinking"}
            for b in content
        )
        or message.get("stop_reason") not in {"end_turn", "max_tokens"}
        or message["usage"]["output_tokens"] > OUTPUT_TOKENS
    ):
        raise ValueError("No bounded, isolated Batch assistant input")
    verify_source(item)
    return reader.native.prediction(
        item["instance_id"], item["context"], Path(item["repo"])
    )


async def collect_batch(*, output, client):
    """Read-only recovery; no resubmission even for errors or expired requests."""
    plan, prepared = read_plan(output)
    current = await batch_status(output=output, client=client)
    if current.get("processing_status") != "ended":
        return {"status": "pending", "batch": current, "confirmed_inputs": 0}
    raw_path = output / "raw-results.jsonl"
    if not raw_path.exists():
        response = await client.get(f"{BATCH_PATH}/{current['id']}/results")
        response.raise_for_status()
        save(raw_path, response.content)
    save(
        output / "meter.json",
        {
            "status": "unknown",
            "api_usage_estimate_usd": None,
            "raw_results_sha256": digest(raw_path),
        },
    )
    entries = []
    for line in raw_path.read_text().splitlines():
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or not isinstance(entry.get("result"), dict):
                raise ValueError("Invalid Batch result record")
            if not isinstance(entry.get("custom_id"), str):
                raise ValueError("Invalid Batch result identity")
            if not isinstance(entry["result"].get("type"), str):
                raise ValueError("Invalid Batch terminal type")
        except (ValueError, TypeError):
            entry = {"custom_id": None, "result": {"type": "malformed"}}
        entries.append(entry)
    expected = {item["custom_id"]: item for item in plan["items"]}
    counts = Counter(entry.get("custom_id") for entry in entries)
    matched = set(counts) == set(expected) and all(n == 1 for n in counts.values())
    actual = Counter(entry["result"].get("type") for entry in entries)
    provider_counts = current.get("request_counts", {})
    if not isinstance(provider_counts, dict):
        provider_counts = {}
    matched = matched and all(
        type(provider_counts.get(kind)) is int
        and provider_counts[kind] == actual.get(kind, 0)
        for kind in ("processing", "succeeded", "errored", "canceled", "expired")
    )
    succeeded = [
        entry["result"].get("message", {})
        for entry in entries
        if entry["result"].get("type") == "succeeded"
    ]
    message_ids = [m.get("id") if isinstance(m, dict) else None for m in succeeded]
    matched = (
        matched
        and all(isinstance(i, str) and i for i in message_ids)
        and len(set(message_ids)) == len(message_ids)
    )
    rows = []
    for entry in entries:
        result = entry.get("result", {})
        row = {
            "custom_id": entry.get("custom_id"),
            "result": result,
            "confirmed_input": False,
            "api_usage_estimate_usd": None,
        }
        if result.get("type") in {"errored", "canceled", "expired"}:
            row["api_usage_estimate_usd"] = 0.0
        elif result.get("type") == "succeeded":
            try:
                row["api_usage_estimate_usd"] = batch_usage_cost(
                    result.get("message", {})
                )
            except (TypeError, ValueError) as exc:
                row["meter_error"] = type(exc).__name__
        rows.append(row)
    metered = matched and all(r["api_usage_estimate_usd"] is not None for r in rows)
    total = sum(r["api_usage_estimate_usd"] or 0 for r in rows)
    meter = {
        "status": "metered" if metered else "unknown",
        "batch_id": current["id"],
        "matched_request_identities": matched,
        "api_usage_estimate_usd": total if metered else None,
        "observed_priced_usage_usd": total,
        "raw_results_sha256": digest(raw_path),
        "rows": rows,
    }
    save(output / "meter.json", meter)  # Before runtime/source/prediction validation.
    if not metered:
        raise ValueError("Incomplete or ambiguous Batch identity/usage accounting")
    verify_runtime(output, prepared)
    for row in rows:
        item = expected[row["custom_id"]]
        row.update(instance_id=item["instance_id"], arm=item["arm"])
        if row["result"]["type"] != "succeeded":
            continue
        try:
            if (
                row["api_usage_estimate_usd"] > REQUEST_BUDGET_USD
                or total > plan["budget_usd"]
            ):
                raise ValueError("Batch actual usage exceeds its reservation")
            message = row["result"]["message"]
            row["packet_prediction"] = confirm(item, message)
            row["confirmed_input"] = True
            row["response_complete"] = message["stop_reason"] == "end_turn"
            row["input_receipt"] = {**item["input_receipt"], "confirmed": True}
            row["messages"] = [*item["request"]["messages"], message]
        except (OSError, TypeError, ValueError) as exc:
            row["validation_error"] = type(exc).__name__
    result = {
        **meter,
        "status": "ok" if all(r["confirmed_input"] for r in rows) else "failed",
        "confirmed_inputs": sum(r["confirmed_input"] for r in rows),
        "rows": rows,
    }
    save(output / "result.json", result)
    return result


def main():
    import httpx

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "submit", "status", "collect"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--packets", type=Path)
    parser.add_argument("--arms", nargs="+", choices=ARMS, default=[ARMS[1]])
    parser.add_argument("--budget-usd", type=float)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not args.live or not key:
        parser.error("Batch operations require --live and ANTHROPIC_API_KEY")
    if args.command == "prepare" and (not args.packets or not args.budget_usd):
        parser.error("prepare requires --packets and --budget-usd")

    async def run():
        async with httpx.AsyncClient(
            base_url=reader.API_URL,
            timeout=180,
            follow_redirects=False,
            transport=httpx.AsyncHTTPTransport(retries=0),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        ) as client:
            if args.command == "prepare":
                plan = await prepare_batch(
                    packets=args.packets,
                    arms=args.arms,
                    output=args.output,
                    budget_usd=args.budget_usd,
                    client=client,
                )
                return {
                    k: plan[k] for k in ("budget_usd", "reserved_with_headroom_usd")
                }
            function = {
                "submit": submit_batch,
                "status": batch_status,
                "collect": collect_batch,
            }[args.command]
            result = await function(output=args.output, client=client)
            return {
                k: result[k]
                for k in (
                    "status",
                    "id",
                    "processing_status",
                    "api_usage_estimate_usd",
                    "confirmed_inputs",
                )
                if k in result
            }

    result = asyncio.run(run())
    print(json.dumps(result))
    if result.get("status") in {"failed", "unknown"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
