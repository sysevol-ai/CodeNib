<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Pre-load-aware agent runtime: scatter → isolate → converge (DRAFT)

> Status: draft for discussion. Sits after PR #248 (Qwen backend + honest eval
> infra). This is the runtime that finally consumes pre-load correctly.

## Update — scatter superseded by eager + LLM gate

Scatter works but is **too expensive**: one verify-subagent PER candidate = N
re-explorations (measured 23 turns vs 4 for a single agent) — it violates the
"save tokens" goal. Replaced by two cheaper ideas, both implemented:

- **eager** (`mode: eager`): trust the ranked hits — read 1–2, answer, STOP;
  fall back to grep only if none fits. Result (Qwen3.5-27B, synthesis, 36 paired):
  **token −43 % vs grep, 5/6 categories equal accuracy**; only `behavioral`
  regresses (candidates hurt explore-only queries with no concrete handle).
- **eager_gated** (`mode: eager_gated`): a one-call LLM gate routes each query to
  eager or blank-slate grep. The discriminator is NOT "has an identifier" — that
  v1 mistake sent traversal queries (no identifier, but candidates on the call
  chain ARE useful) to grep and lost 0.20. The right axis (from per-category
  candidate value: behavioral grep>preload; traversal/symbol preload>=grep) is
  **route to GREP only single-feature behavioral symptoms; EAGER anything that
  names an identifier OR traces a cross-component flow**. Gate verified on n=400
  multilingual: behavioral 73 % GREP, traversal 88 % EAGER, symbol/file ~100 %.
  (Gotcha: Qwen3.5 is a thinking model — disable CoT via `extra_body
  chat_template_kwargs enable_thinking=False`, else the one-word verdict is
  truncated and everything routes EAGER.)
  **Result (Qwen3.5-27B synthesis, 36 paired) — gated v2 is Pareto-dominant:**
  ALL span **0.73** (vs grep/embed 0.70, eager 0.66) at **55k tokens**
  (= the cheapest arm; −43 % vs grep, −27 % vs embed). Per category: behavioral
  0.91 (=grep, rescued), traversal 0.83 (=embed, vs v1's 0.63), specific
  categories equal-accuracy at a fraction of the tokens. Adaptive routing beats
  every single fixed strategy on BOTH axes at once.

The scatter design below is retained as **explored-and-rejected**: the converge
idea is sound, but per-candidate full subagents are the cost mistake.

## Motivation — diagnosed, not guessed

On codeminer-synthesis (Qwen3.5-27B, span answer_rec), pre-load is a *trade*:
it saves cost (−1.7 turns, −7.6 % tokens) at equal pooled accuracy but
**REGRESSES `behavioral`** (Δrec −0.167). We diagnosed the 4 losing queries:

| query | preinj behaviour | GT in candidates? | grep→preinj |
|---|---|---|---|
| astropy-13579 | **7 searches, 9 reads, turns=16** | **yes** | 1.0 → 0.0 |
| xarray-6992 q6 | 5 searches, 10 reads, turns=16 | yes | 1.0 → 0.0 |
| xarray-6992 q7 | 6 searches, 11 reads, turns=16 | no | 1.0 → 0.0 |
| xarray-6992 q2 | 0 searches, 5 reads, turns=5 | no | 1.0 → 0.0 |

**The regress is NOT under-exploration** — the agent searched a lot (7/9,
turns=16) and the GT was even in the candidate set, yet it still answered wrong;
grep_only with no candidates answered cleanly (1.0). Root cause: **a noisy
candidate set injected into a single context pollutes a strong agent's
exploration line** — it diverges across 16 turns toward the wrong place, worse
than a blank-slate grep.

## Why single-context triage can't fix it

The pre-load preamble is *already* triage-aware ("candidates often wrong / may be
in NONE / do not anchor on the first / if none right, IGNORE and grep"), and the
agent still regresses. The agent explored *enough*; the problem is **context
pollution**, not loop logic. You can't un-see the noisy candidates once they're
in the window. The fix must isolate the noise, not re-word the prompt.

## Architecture

Input: `query` + K pre-load candidates.

1. **Scatter.** Dispatch one *minimal verify-subagent* per candidate (or per
   small group). Each gets an isolated context and a narrow brief: "Is THIS
   location the code to change for `<query>`? Read it + directly-related code.
   Return VERDICT yes/no, and if yes the exact Files/Symbols/Locations + a short
   evidence note." Bounded (small `max_turns`, read+grep only).
2. **Isolate.** Each subagent's noisy reading stays in *its* context. The
   orchestrator never sees the dead-end code, only the structured verdict.
3. **Converge.** The orchestrator collects N verdicts:
   - ≥1 `yes` → synthesize the final answer from the confirmed location(s).
   - all `no` → dispatch one *explore-subagent* with a **blank slate** (no
     candidates) — i.e. the clean grep_only path that wins on behavioral.

## Why this is the right pre-load runtime

- **Context amortization** — K candidates' exploration is spread over N isolated
  contexts instead of piling 9 reads + 7 greps into one 16-turn window.
- **Fewer turns (orchestrator)** — the main agent only converges verdicts (~1–2
  turns); exploration turns move into the subagents.
- **Parallel wall-clock** — N subagents run concurrently; faster than one agent
  grinding 16 sequential turns.
- **Noise isolation → kills the REGRESS** — a wrong candidate's dead-end reading
  never reaches the deciding context.
- **Clean fallback** — all-`no` routes to a blank-slate explorer, which is
  exactly the grep_only path that beat preload on behavioral.

This is pre-load consumed correctly: candidates become **seeds for isolated
exploration units**, not prompt filler one agent must digest.

## Why this can work where verify-expand didn't

`verify-expand` was a no-op (0/400): a GT-free *deterministic* resolve check the
agent always passed (it cites real-but-wrong symbols). Here each subagent makes
an **LLM semantic** judgement on a single candidate in a clean context — real
signal, and the noise that fooled the monolithic agent is partitioned away.

## Implementation plan

- New module `scripts/agent_compile/lib/orchestrator.py`:
  `scatter_gather_localize(llm, query, candidates, contexts, repo_path, ...) -> answer`.
  Reuses `AgentRunner` — each subagent is one `runner.run()` with a verify prompt,
  fresh history, small `max_turns`.
- New arm `preinj_scatter` in the config; `run_cell` routes to the orchestrator
  instead of the single runner when the arm requests it.
- **v1 (prove accuracy, serial):** run subagents serially, validate the
  behavioral REGRESS disappears. Ignore latency.
- **v2 (optimize):** run subagents concurrently for wall-clock.

Prove the accuracy claim before paying for concurrency.

## Evaluation

- Dataset: codeminer-synthesis (many-query; reuse is real).
- Arms: `grep_only` vs `preinj_embed` (current single-context) vs
  `preinj_scatter` (new).
- Metrics: span answer_rec@k (paired bootstrap, `pareto_ci.py`) + turns + tokens.
- **Success = behavioral no longer REGRESSES** (Δrec CI_lo ≥ −EPS) while keeping
  the token/turn saving. Watch total tokens: N subagents must not cost more than
  the single agent's polluted 16-turn run.

## Open questions

- Total token cost of N subagents vs one monolithic run (the key trade to verify).
- Converge strategy when multiple subagents say `yes` (merge vs pick-most-confident).
- Group candidates (fewer, cheaper subagents) vs one-per-candidate (max isolation)?
- Does it help weak models (Qwen2.5) too, or mainly strong agents?
