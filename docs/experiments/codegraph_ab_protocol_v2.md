<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Code-graph tool A/B, protocol v2

Status: sealed on 2026-11-02 before any model call. Results are not in this
document yet.

Round v1 tested CodeNib's source search more than its graph. Of 12 CodeNib
results, only 3 contained graph relationships, because the graph was still
being built or had failed ([results v1](codegraph_ab_results_v1.md)). Round v2 asks the same
question as [protocol v1](codegraph_ab_protocol_v1.md) once CodeNib's graph
is ready when the agent first asks for it. Does an agent that may use
CodeNib finish the same tasks better or faster than the same agent with
native tools only, with Serena, or with colbymchenry/codegraph?

Everything not listed below is unchanged from v1: the agent and model, the
four arms, the five repositories and their images, the 15 tasks, the private
tests and rubrics, the time limits, the budget, and the decision rule.

## What changed from v1

- **CodeNib build.** The candidate wheel is built from the #959–#965 stack.
  It adds two fixes for defects that v1 exposed:
  - #964 keeps the graph when Python 3.12 generic syntax appears.
  - #965 refreshes retrieval instead of failing every call when a source
    edit follows a failed graph build.
- **When CodeNib builds its graph.** `codenib codegraph init` now builds the
  graph during setup instead of deferring it to the first query. Setup time
  counts toward workflow wall time, as before. The MCP server command is
  unchanged (`--defer-graph`), so after the agent edits source, CodeNib
  refreshes retrieval and rebuilds the graph in the background.
- **Admission.** Each CodeNib workflow must have a fresh graph after setup,
  and its first model-free query must return graph relationships.
- **Harness.** Three changes fix the harness problems found in v1:
  - after each task, files the agent's commands created inside the container
    are handed back to the host user;
  - a file the host cannot read is recorded, not fatal;
  - `.hypothesis` caches are left out of captured patches.
- **Run order.** A new seed, `codenib-ab-v2-2026-11-02`, sets the arm order
  within each repository.
- **Grading.** The graders see shuffled letters with no arm names or traces,
  as in v1, which the v1 protocol did not specify. Results report the strict
  count and the count under the looser readings the graders name.

The tasks file is byte-identical to v1's. Its grading material was published
with the v1 results. The agent works in a snapshot with no history, is never
pointed at that material, and has no web search or fetch tool.

## Commitments

All hashes are SHA-256.

| Input | Hash |
|---|---|
| Policy (`ab-policy-v2.json`) | `a81cd48321437524e24fa4392cf55455e4010b9814e148766bff01bdc38950fc` |
| Agent-visible tasks (`ab-tasks-v2.json`, same bytes as v1) | `105bce1d5639c8d9b6ac5d72dad8f5802c8430f715b8943b7cca5752bbcd3445` |
| Execution binding (every input file, admission receipt, image and source head) | `7c932e1ecd18f667af086ccdb548aafbbb2c62a0d285d37b6e6b033ba7ab17e0` |
| CodeNib candidate wheel (PR #965 head `8854295b`) | `4f59e22baf75266705ea311b53ec4da7277d5d425b1ecc30bf8415d66df5a84e` |

The private grading files are byte-identical to v1's and appear in the
binding with the same hashes as in the v1 archive.
