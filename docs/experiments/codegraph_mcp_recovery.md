<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Deliver index-recovery instructions to MCP clients

An agent needs the recovery action when a source change invalidates its index.
CodeNib already rejects stale queries and reloads explicitly rebuilt indexes
in the same MCP connection. With MCP 2.3, an ordinary raised exception hides
that action behind `Error executing tool dependency_subgraph`.

[#890](https://github.com/sysevol-ai/CodeNib/pull/890) returns known source and
rebuild rejections as explicit failed tool results.
This fixes delivery of the recovery instruction; it does not establish that an
agent will follow it or that rebuilding improves complete task time. The source
fix is unreleased, and [A0](../adoption_roadmap.md) and
[#874](https://github.com/sysevol-ai/CodeNib/issues/874) remain active.

## Observed failure and repair

The [executed controls](codegraph_mcp_recovery_2026_10_28.json) reproduce the
hidden instruction on the pinned Requests, Flask and pytest sources from the
completed matched study. Each original dependency query succeeds. After a
tracked edit, all three return `isError: true` with only the generic execution
failure text. The internal exception contains the rebuild instruction, but
the actual client reply does not. Older direct-function tests cannot detect
that transport-level loss; the supported MCP 2.0 environment retains exception
text while MCP 2.3 masks unexpected failures.

The repaired boundary recognizes a dedicated indexed-query exception, including
the SDK wrapper's cause chain. It returns only its explicit recovery message
as a failed MCP tool result. Source-verification details remain diagnostic
state, and unrelated exceptions keep the SDK's existing error path. Busy
compiler and reconfigured-context rejections use the same bounded error path.
The [current MCP specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#error-handling)
defines failed tool results with actionable content for client recovery.

The client now receives:

```text
Indexed source changed. Run `codenib codegraph init` for this repository and retry;
this MCP connection reloads rebuilt indexes.
```

The source guard still checks before and after provider execution. A source
edit during a query discards its completed facts and delivers the same recovery
action. Reads do not start indexing. Rebuilding remains an explicit CLI action;
the existing compiler, source and session ownership rules are unchanged.

## Bounded real-source verification

Each candidate control builds BM25 and a strict Python graph, repeats unchanged
initialization, and opens one real stdio MCP process. A controller wraps a
selected production return value with a new identity helper in the same file.
It confirms stale rejection, explicitly rebuilds, and retries through the same
MCP process. The returned graph must contain the exact function-to-helper
edge; source reads, BM25 and manifest authentication must match the edited
version. Source bytes and Git state remain equal to the recorded controller
edit after rebuilding and queries.

| Source function | Fresh-state initialization | Unchanged initialization | Explicit rebuild after edit |
| --- | ---: | ---: | ---: |
| Requests `Session.prepare_request` | 10.819 s | 0.730 s | 10.367 s |
| Flask `Flask.make_response` | 10.454 s | 0.889 s | 9.831 s |
| pytest `CallInfo.from_call` | 24.597 s | 1.059 s | 23.447 s |

These are descriptive developer-control timings with warm installed providers
and packages; other local verification ran concurrently. They are not cold
installation measurements or comparative whole-task endpoints. Client
registration uses isolated stand-ins. The CLI, compiler, SCIP 0.6.6,
basedpyright 1.39.8, serial graph decoder, BM25, MCP and source checks are real.
Each network-disabled container has two CPUs, 4 GiB and 256 processes, one host
checkout mount, and a verified empty temporary mask over an inherited image
workspace. The executed controllers, exact source/image bindings and earlier
preparation failures are retained separately from product outcomes.

All three candidate controls pass: 21 actual MCP calls contain 18 successful
results and three expected stale-query errors. Each original MCP process
survives the rebuild. All six baseline/candidate observation containers are
removed. The archived source snapshots retain the original failures, rather
than replacing them with candidate results.

Deterministic validation covers all eleven indexed tools through modern and
legacy clients, same-connection retry, a mid-query edit and compiler contention.
The focused 97-test set passes under both MCP 2.0 and 2.3. The MCP 2.3 unit
surface passes 438 tests, with seven local prerequisite skips and seven heavier
tests deselected. The full local unit tier passes 8,836 tests, with 24 skips,
221 deselections and 46 existing warnings.

## Remaining decision gates

No SDK/model inference ran for this repair. The original 22 matched-study files,
acceptance, timing and usage records remain byte-for-byte unchanged. Their
insufficient-evidence decision and held promotion still apply; the original
Claude Max cohort's $9.880642 SDK estimate remains separate from unknown invoice
charges. No external participant is enrolled.

Next, freeze and execute a separate prompted agent-recovery probe after this
source fix. It must retain failed queries, actual command/model delivery,
complete recovery time, retries and native fallback. Such a probe cannot count
as voluntary tool use, a prospective 20% benefit result, or external repeat use.
Broader source coverage, representative update/agent-use cost, a valid future
matched benefit gate and five-user repeat use remain open.
