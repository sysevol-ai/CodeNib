<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Code-graph tool A/B, protocol v1

Status: sealed on 2026-11-02 before any model call. Results are not in this
document yet.

This protocol asks one question: once CodeNib's MCP server is fixed so that
agents actually call it (#959–#962), does an agent that may use CodeNib finish
unfamiliar cross-file tasks better or faster than the same agent with native
tools only, with Serena, or with colbymchenry/codegraph?

The design is committed here by hash. The hidden material (the injected
regressions, the private tests and the grading rubrics) stays unpublished
until every workflow has finished. It will be published with the results so
anyone can check it against the hashes below.

## What is compared

Each arm is the same agent with the same native tools. The arms differ only in
which code-graph MCP server, if any, is connected. No arm is told to call its
server; every call is voluntary.

| Arm | MCP server | How it runs |
|---|---|---|
| native | none | Read, Grep, Glob, Edit, Write and Bash only |
| serena | `serena-agent==2.1.0` | `--context claude-code --agent-interface TOOLS` (direct symbol tools, not the REPL default) |
| codegraph | `@colbymchenry/codegraph@1.6.2` | `codegraph init`, then `codegraph serve --mcp` |
| codenib | CodeNib 0.2.5 wheel built from #962 | `codenib mcp --defer-graph --tool-surface explore`, text output |

The agent is Claude Code 2.1.316 through `claude-agent-sdk` 0.2.174, model
`claude-opus-5-5`, effort `medium`, at most 24 turns per query. Tool search is
off (`ENABLE_TOOL_SEARCH=false`), so MCP tools are loaded up front rather
than discovered on demand. The system
prompt is the unmodified Claude Code preset plus the same repository context
for every arm; each MCP server keeps its own instructions and schemas.

## Tasks

Five Python repositories, each pinned at an upstream commit from
2026-11-01/02. Each gets one synthetic cross-file
regression. The agent sees a one-commit snapshot with no history, so `git log`
and `git diff` cannot reveal the change.

| Repository | Upstream commit | Snapshot the agent sees |
|---|---|---|
| beetbox/beets | `e4fdcf5efec8` | `9aac0a998c36` |
| sqlfluff/sqlfluff | `4339c0759040` | `34e59805d69f` |
| strawberry-graphql/strawberry | `02770ca43398` | `bd45a8de09d6` |
| pdm-project/pdm | `a4965a8f116d` | `4eb70c4ab397` |
| litestar-org/litestar | `a6cd13587b3b` | `d4e3c4621e3d` |

Each repository is one workflow of three dependent tasks:

| Task | What the agent does | Time limit |
|---|---|---|
| diagnose | Explain the reported symptom and trace it to its cause in source, without editing | 300 s |
| repair | Fix it with a small compatible change and regression tests, and run the relevant tests | 480 s |
| review | Trace the repaired paths with source citations and name the tests that cover them, without editing | 300 s |

Diagnosis runs in a fresh session. Repair and review run together in a
second session. No conversation is shared across arms. Five repositories times
four arms gives 20 workflows and 60 tasks.

Every regression was checked before sealing. On the snapshot, the private
tests fail; with the regression reverted, all private tests and all selected
existing tests pass in the case's Docker image.

## Order and admission

Repositories run in the fixed order beets, sqlfluff, strawberry, pdm,
litestar. Within a repository the arms run in the order of
`SHA256("codenib-ab-v1-2026-11-02" NUL repository NUL arm)`, computed once
before any model call.

Before sealing, all 20 workflows passed a model-free admission. Each one
installed its server, listed its advertised tools, answered one symbol lookup
on its case and was torn down. Admission fixed the expected environment
and product versions, and the run refuses to start a workflow whose inputs
differ from the sealed hashes.

## Grading

- **Repair** is accepted only if every private test and every selected
  existing test passes on the captured source, graded in the case image.
  Tests the agent added are recorded separately and do not count.
- **Diagnosis and review** are reviewed once against the captured answer and
  source, using a per-task rubric with mandatory criteria. Material errors are
  kept and reported, not averaged away.
- A timed-out task keeps its partial trace, is charged its session's budget
  cap and fails.

## Decision rule

The primary outcome is accepted tasks per arm (out of 15) and total
workflow wall time per arm.

CodeNib shows a benefit only if it accepts at least as many tasks as the best
other arm, has no additional serious error, completes the five workflows at
least 20% faster than that arm in total, and uses no more than 10% more
normalized model tokens. Anything else is reported as no demonstrated benefit.

Secondary outcomes are voluntary MCP calls per arm and task, the first tool
used, and the characters of product results delivered to the model. Five
repositories support per-case reporting only; no population interval is
claimed.

## Budget and stopping

The planned SDK cost estimate is about $15 for the round. No new
workflow starts once the observed estimate reaches $25. A session that ends
without final usage is charged its $3 cap. A quota or billing change stops the
round; the account, model and authentication route are never switched
mid-round.

## Commitments

All hashes are SHA-256.

| Input | Hash |
|---|---|
| Policy (`ab-policy-v1.json`) | `26e0df4aafc29bb7e4688ee4baae619f016b0c66fa9edf071d17c441996fed1b` |
| Agent-visible tasks (`ab-tasks-v1.json`) | `105bce1d5639c8d9b6ac5d72dad8f5802c8430f715b8943b7cca5752bbcd3445` |
| Execution binding (every input file, admission receipt, image and source head) | `f776915fb3ddddfa360c7f6034c2b83e7afd5d47f240c6b7fe2b1680499eca46` |
| CodeNib candidate wheel | `7079080196edc53f0175b0eb29680399fa5303384cb445d96a0e803ba351ce2e` |
| Recorded rubric edits (`reviewer-edits.json`) | `c4abe4e81f58feb3a35302eb4058362883a6afb4e2c381fd391f75997c2621e0` |

| Repository | Regression patch | Rubric | Private test files | Selected existing tests |
|---|---|---|---|---|
| beets | `de29df0915e6f882…` | `324e623adccbab96…` | 2 | 53 |
| sqlfluff | `659075e2812ff791…` | `b9ccb018679d0a13…` | 1 | 52 |
| strawberry | `91defc33fa7dcfb0…` | `128d2587fd4c5772…` | 1 | 50 |
| pdm | `753fea525c55e2af…` | `42303bf69623569c…` | 1 | 53 |
| litestar | `1857d88e830570b3…` | `79801e09515953fb…` | 2 | 37 |

The execution binding lists the full hash of every private test file,
regression patch and rubric; the short hashes above are its prefixes.
