<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Code-graph tool A/B, protocol v3

Status: sealed on 2026-11-03 before any model call. Results are not in this
document yet.

Round v3 tests one change: call chains in CodeNib's exploration results.
In v1 and v2, most rejected diagnoses had found the faulty function but left
out a step of how the code reaches it, such as the command handler that leads
there. In v2, CodeNib delivered its graph, but only a two-step neighbourhood
around the symbol ([results v2](codegraph_ab_results_v2.md)). `explore_context`
now also returns call chains for its leading symbol (#967). Each chain runs from an entry
point, meaning code that nothing in the repository references, down to the
symbol, and names the line of every step. Does that change what the agent
gets right?

Everything else is unchanged from [protocol v2](codegraph_ab_protocol_v2.md):
the agent and model, the four arms, the five repositories and their images,
the 15 tasks, the private tests and rubrics, the time limits, the budget,
CodeNib's graph being built during setup, the harness, and the decision rule.

## What changed from v2

- **CodeNib build.** The candidate wheel is built from the #959–#967 stack,
  so the only behaviour change is the call chains. The first admission
  attempt used #967's first head (`9ceb2294`). Its beets probe landed on an
  override that nothing references directly (`FixedFieldSort.order_clause`)
  and returned no chain. #967 then gained a fix that follows such an override
  to the referenced method of the same name (`58c778eb`). All 20 admissions
  were rerun with that build, before any model call.
- **Admission.** Each CodeNib workflow's first model-free query must also
  return at least one call chain.
- **Run order.** A new seed, `codenib-ab-v3-2026-11-03`, sets the arm order
  within each repository.
- **Focus.** Because call chains target diagnosis, the results report the
  diagnose tasks accepted per arm, strict and looser, alongside the primary
  outcome. They also report the chains CodeNib delivered per task.

## Commitments

All hashes are SHA-256.

| Input | Hash |
|---|---|
| Policy (`ab-policy-v3.json`) | `963c1cbbff917b66bcd2ff5dbb53aa0b53ffeb0caae174ff7fb0fb4ee745c81b` |
| Agent-visible tasks (`ab-tasks-v3.json`, same bytes as v1 and v2) | `105bce1d5639c8d9b6ac5d72dad8f5802c8430f715b8943b7cca5752bbcd3445` |
| Execution binding (every input file, admission receipt, image and source head) | `99ba16436eadc3d080c8e67a434123d09e20242d43402dacb5765c3ad0ec7c54` |
| CodeNib candidate wheel (PR #967 head `58c778eb`) | `310f7d723bc5e03f61a520f190d0ff5892f04b6125fb2d640aba5609b29561d1` |

The private grading files are byte-identical to v1's and v2's.
