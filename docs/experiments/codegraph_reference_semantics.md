<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Describe dependency results as static references

CodeNib dependency neighborhoods contain indexed references, including fields,
type uses and function values. An edge alone does not establish invocation or
complete runtime call coverage. The model-facing guidance and response now
state that meaning, addressing
[#891](https://github.com/sysevol-ai/CodeNib/issues/891) before another benefit
cohort. Promotion remains held under [A0](../adoption_roadmap.md).

## Observed interpretation gap

The [frozen recovery probe](codegraph_agent_recovery_v1.md) met its bounded
recovery criteria on all three sources. Its Flask answer nevertheless called
a table of references "direct calls", including `ResponseReturnValue` from a
parameter annotation. Its pytest answer also observed missing external and
dynamic call edges. Those answers and their original recovery assessments stay
unchanged; the probe does not certify complete answer correctness.

The query already walks `reference` edges. Its old description promised a
call graph, and its legacy result directions said `callers` and `callees`.
That wording overstated what the stored relationships establish. Filtering
out fields or type uses would lose useful dependency facts and still would
not distinguish a function-value reference from an invocation.

## Current response contract

Keep the existing graph, direction labels, node/edge values, source locations,
limits and errors. Successful graph responses add `semantics` before their
data. It identifies the `reference` edge type and the incoming/outgoing
reference direction, explains that calls, fields, types and function values
can contribute references, and states possible missing external/dynamic calls.
The description, server instructions and guide direct the caller to source
call sites before interpreting invocations.

| Request | Reference direction | Retained result label |
| --- | --- | --- |
| `impact` | Incoming | `callers` |
| `dependencies` | Outgoing | `callees` |
| `both` | Both, within `depth` | `both` |

Existing direct-implementation aliases remain compatible; the public MCP
request enum is unchanged. Graph traversal code is unchanged apart from
docstrings/comments, verified by comparing runtime ASTs with the prior main
commit. No persisted vertex/edge layout or decoder changes, schema bump,
provider expansion or new indexing work is introduced. Reads remain subject
to the existing source-identity guard.

## Executed verification

The [retained control](codegraph_reference_semantics_2026_10_28.json) builds a
fresh strict Python SCIP/BM25 index on the pinned, historically exposed Flask
source. Six real MCP 2.3 stdio calls cover the three directions, the method
annotation, the type-alias source and current source status. The delivered
legacy payload equals analysis of the saved graph in every direction; the
`ResponseReturnValue` field/reference remains present with its source location.
The new meaning and coverage accompany the actual reply. Repository bytes,
HEAD and Git status remain unchanged.

Nine deterministic regressions retain implementation aliases and exercise
mixed call, callback-value, field and type references through modern and legacy
clients. The targeted 39-test set passes under both MCP 2.0 and 2.3. The MCP
2.3 unit surface passes 447 tests, with seven local prerequisite skips and
seven heavier tests deselected. The full local unit tier passes 8,845 tests,
with 24 skips, 221 deselections and 46 existing warnings. Earlier matched and
agent-recovery artifacts retain their exact bytes.

## Remaining benefit gate

This is a source-only correctness-of-description fix. It does not show that
models interpret every reference correctly or that complete task time improves.
No new SDK/model inference ran. The published PyPI 0.2.4 response format and
earlier frozen outcomes remain unchanged.

Next, finish exact unattended peer setup and source-rubric admission, then
freeze the next complete matched protocol before inference. Retain every setup,
update, failure and optional-tool-use outcome. The 20% net-benefit gate remains
unmet, and the five-user trial still has no enrolled participant.
