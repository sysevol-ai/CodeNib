<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Pre-load-aware agent harness (DRAFT / research direction)

> Status: draft for discussion. Sits after PR #248 (Qwen backend + honest
> eval infra). Motivated by the Qwen3.5 findings below.

## Motivation — what the data showed

Pre-load today = inject retrieval candidates into the opening prompt, then run
the **generic** grep/read/answer loop over them. Two failure modes, both
measured on Qwen3.5:

- **Candidates good (symbol_hint, base):** the agent still explores from scratch
  instead of confirming the candidate — reuse value is small.
- **Candidates bad (behavioral, synthesis):** the agent **over-trusts** the
  candidates — fewer turns (−2.9) but answers wrong (Δrec@5 −0.167, a significant
  REGRESS). It picks the "most plausible" candidate instead of falling back to
  exploration.

Net (27B, span answer_rec): pre-load is a **trade, not a free win** — it saves
turns/tokens (−1.7 turns, −7.6 % tokens on synthesis) but costs accuracy on
explore-heavy queries. The candidates and the loop are two separate things that
were never fused. "We found nails, but the agent still swings a generic hammer
from scratch."

## Core idea — candidate triage, not linear consumption

Replace "candidates in the prompt + generic loop" with a loop whose first-class
job is to *process the candidate set*:

1. **Rule-out (cheap):** one pass scoring each candidate plausible/implausible
   from its signature (symbol + 1 line of context) — batch-eliminate most
   without deep reads.
2. **Verify (focused):** read-confirm only the 1–3 survivors → converge early.
3. **Fallback (the REGRESS fix):** if *all* candidates look implausible
   (common on behavioral), explicitly switch back to autonomous grep instead of
   committing the least-bad candidate.

The novelty is an **explicit "trust-candidates vs explore" branch**, decided by
the agent's own assessment of candidate quality — not blind consumption.

### Possible primitives (make candidates first-class actions)

- `rule_out(ids, reason)` — drop candidates
- `verify(id)` — read + confirm a survivor
- `fallback_search(query)` — autonomous exploration when candidates fail

## Why this can work where verify-expand didn't

`verify-expand` (the prior harness customization) was a **no-op (0/400)** because
it was a *GT-free deterministic* check (does the cited symbol resolve in the
graph?) — the agent always cites a real-but-possibly-wrong symbol, which a
deterministic check can't catch. Triage is **LLM semantic judgement**
(plausible/implausible distinguishes right-file from wrong-file), so it has
signal where resolution checking had none.

## How to evaluate

- **Dataset:** codeminer-synthesis (many-query; where reuse value is real).
- **Arms:** `grep_only` vs `preinj_embed` (current) vs `preinj_triage` (new).
- **Metric:** span answer_rec@k (symbol@k is unusable for agents — string match
  vs free-form output scores ≈0; see `docs/experiments/qwen_backend.md`), plus
  turns/tokens. Paired bootstrap CI (`pareto_ci.py`).
- **Key question:** does triage keep the token/turn savings while removing the
  behavioral REGRESS? Target verdict = SAVE on behavioral, not just symbol_hint.

## Open questions

- Cost of the rule-out pass vs the turns it saves (must net positive).
- Does triage help weak models (Qwen2.5) too, or only strong agents?
- Is rule-out better as one LLM pass or N cheap parallel checks?
