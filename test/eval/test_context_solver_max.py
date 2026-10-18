"""Per-call included subscription receipts and terminal fees without providers."""

import copy
import dataclasses
import sys
from types import SimpleNamespace

import pytest

from scripts.benchmark_context_solver import SubscriptionModel


@pytest.mark.parametrize(
    "scenario",
    [
        "before",
        "after_assistant",
        "after_result",
        "org_disabled",
        "missing",
        "missing_init",
        "paid",
        "using_overage",
        "unknown",
        "rejected",
        "api_source",
        "late_unsafe",
        "iterator_failure",
    ],
)
def test_solver_max_receipts_gate_actions_and_keep_terminal_fees(
    monkeypatch, tmp_path, scenario
):
    def event(kind, **fields):
        return dataclasses.make_dataclass(kind, [(name, object) for name in fields])(
            **fields
        )

    safe = {
        "status": "allowed",
        "raw": {"overageEnabled": False, "isUsingOverage": False},
    }
    info = copy.deepcopy(safe)
    if scenario == "org_disabled":
        info = {
            "status": "allowed_warning",
            "overage_status": "rejected",
            "overage_disabled_reason": "org_level_disabled",
            "raw": {
                "overageStatus": "rejected",
                "overageDisabledReason": "org_level_disabled",
                "isUsingOverage": False,
            },
        }
    if scenario == "paid":
        info["raw"]["overageEnabled"] = True
    if scenario == "using_overage":
        info["raw"]["isUsingOverage"] = True
    if scenario == "unknown":
        del info["raw"]["overageEnabled"]
    if scenario == "rejected":
        info["status"] = "rejected"
    requests, checkpoints = [], []

    async def query(**kwargs):
        requests.append(kwargs)
        if scenario != "missing_init":
            yield event(
                "SystemMessage",
                subtype="init",
                data={
                    "tools": [],
                    "apiKeySource": (
                        "ANTHROPIC_API_KEY" if scenario == "api_source" else "none"
                    ),
                },
            )
        rate = event("RateLimitEvent", rate_limit_info=info)
        if scenario not in {"missing", "after_assistant", "after_result"}:
            yield rate
        yield event(
            "AssistantMessage",
            content=[{"text": "```bash\nMUST_NOT_EXECUTE_UNTIL_VERIFIED\n```"}],
            model="fixed-model",
        )
        if scenario == "after_assistant":
            yield rate
        yield event(
            "ResultMessage",
            total_cost_usd=0.02,
            is_error=False,
            subtype="success",
            session_id="one-actual-session",
        )
        if scenario == "after_result":
            yield rate
        if scenario == "late_unsafe":
            yield event(
                "RateLimitEvent",
                rate_limit_info={
                    "status": "allowed",
                    "raw": {"overageEnabled": True, "isUsingOverage": True},
                },
            )
        if scenario == "iterator_failure":
            raise RuntimeError("stream failed after metered result")

    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(ClaudeAgentOptions=lambda **kwargs: kwargs, query=query),
    )
    calls = []
    model = SubscriptionModel(
        cli="fixture",
        model="fixed-model",
        cwd=tmp_path,
        max_meter_usd=1,
        calls=calls,
        checkpoint=lambda: checkpoints.append(copy.deepcopy(calls)),
        max_included_only=True,
    )
    messages = [
        {"role": "system", "content": "shell"},
        {"role": "user", "content": "task"},
    ]
    accepted = scenario in {"before", "after_assistant", "after_result", "org_disabled"}
    if accepted:
        assert model.query(messages)["content"].startswith("```bash")
        assert calls[0]["max_included_rate_confirmed"]
    else:
        with pytest.raises(RuntimeError):
            model.query(messages)
        assert not calls[0]["max_included_rate_confirmed"]
        with pytest.raises(RuntimeError, match="Previous solver included Max check"):
            model.query(messages)
    assert len(requests) == model.n_calls == len(calls) == 1
    assert model.cost == calls[0]["sdk_reported_cost_usd"] == 0.02
    assert checkpoints[-1][0]["sdk_reported_cost_usd"] == 0.02
    assert calls[0]["input_delivery_confirmed"]


def test_solver_max_policy_rejects_api_route_before_dispatch(tmp_path):
    with pytest.raises(ValueError, match="subscription route"):
        SubscriptionModel(
            cli="fixture",
            model="fixed-model",
            cwd=tmp_path,
            max_meter_usd=1,
            calls=[],
            checkpoint=lambda: None,
            billing_route="anthropic-api",
            max_included_only=True,
        )
