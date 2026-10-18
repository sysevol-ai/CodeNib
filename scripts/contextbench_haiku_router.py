# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CodeNib agent policy: cheap evidence gate before an optional strong review.

The native search agent retains its live history and tools. A tool-free Haiku
gate reviews confirmed prior evidence; only concrete cited gaps dispatch Opus.
This reuses CodeNib source delivery without an embedding/graph prerequisite.
Both gate and strong-model fees are visible, including unsuccessful requests.
"""

import hashlib
import json

from scripts.contextbench_evidence_review import HAIKU, MODEL, EvidenceReviewGuard


class HaikuRouterGuard(EvidenceReviewGuard):
    """One Stop attempt, at most one request at each of two ordered stages.

    The inherited pre-await attempt latch and SDK session identity own dispatch;
    child reviewers never receive independent Stop callbacks. They run serially,
    outside the existing view -> reader lease. The caller owns context/client
    cleanup and checkpoint recovery; a failed stage is never retried or routed
    around. No additional locks, detached requests or persistent state exist.
    """

    hook_timeout_seconds = 300

    def __init__(self, ctx, *, router_budget_usd=0.015, **kwargs):
        super().__init__(ctx, **kwargs)
        self.router = EvidenceReviewGuard(
            ctx,
            issue=self.issue,
            observed_messages=self.observed_messages,
            client=self.client,
            checkpoint=self._stage_checkpoint,
            request_budget_usd=router_budget_usd,
            max_output_tokens=self.max_output_tokens,
            max_packet_bytes=self.max_packet_bytes,
            review_model=HAIKU,
        )
        self.strong = EvidenceReviewGuard(
            ctx,
            issue=self.issue,
            observed_messages=self.observed_messages,
            client=self.client,
            checkpoint=self._stage_checkpoint,
            request_budget_usd=self.request_budget_usd,
            max_output_tokens=self.max_output_tokens,
            max_packet_bytes=self.max_packet_bytes,
            review_model=MODEL,
        )
        self.ledger.pop("model")
        self.ledger.update(
            policy="haiku_gate_optional_opus_review_v1",
            system_under_test="CodeNib agent",
            models=[HAIKU, MODEL],
            router=self.router.ledger,
            strong_review=self.strong.ledger,
            escalation_basis="one_or_two_concrete_gaps_citing_confirmed_evidence",
            strong_dispatched=False,
        )

    def _stage_checkpoint(self):
        stages = [self.router.ledger, self.strong.ledger]
        self.ledger["api_http_requests"] = sum(s["api_http_requests"] for s in stages)
        self.ledger["paid_message_requests"] = sum(
            s["paid_message_requests"] for s in stages
        )
        fees = [s["api_usage_estimate_usd"] for s in stages]
        self.ledger["api_usage_estimate_usd"] = (
            sum(fees) if all(f is not None for f in fees) else None
        )
        self.checkpoint()

    async def _stage(self, reviewer, packet):
        try:
            return await reviewer._review(packet)
        except Exception as exc:
            reviewer.ledger.update(
                failed=True, status="failed", error=f"{type(exc).__name__}: {exc}"
            )
            raise
        finally:
            self._stage_checkpoint()

    async def _review(self, packet):
        self.ledger["packet"] = packet
        self.ledger["prompt_sha256"] = hashlib.sha256(
            json.dumps(packet, ensure_ascii=True).encode()
        ).hexdigest()
        self.ledger["status"] = "routing"
        decision = await self._stage(self.router, packet)
        self.ledger["input_confirmed"] = self.router.ledger.get(
            "input_confirmed", False
        )
        if decision is None:
            self.ledger["status"] = "router_" + self.router.ledger["status"]
            return None
        self.ledger["router_decision"] = decision
        if decision["decision"] == "stop":
            self.ledger.update(status="router_stop", review_decision=decision)
            return decision
        self.ledger.update(status="strong_review_pending", strong_dispatched=True)
        # Same verified packet; no unread source or model-authored source is
        # inserted. Opus makes an independent evidence judgment, without taking
        # the router's guess as established behavior.
        decision = await self._stage(self.strong, packet)
        self.ledger["status"] = "strong_" + self.strong.ledger["status"]
        if decision is not None:
            self.ledger["review_decision"] = decision
        return decision
