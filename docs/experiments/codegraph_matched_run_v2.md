# Independent four-arm workflow execution, v2

All sixty SDK tasks completed, all twelve captured repairs pass the unchanged
selected native grading, and all 48 read-only tasks preserve source bytes.
The [completed source assessment](codegraph_matched_assessment_v2.md) still
yields insufficient evidence under the frozen benefit rule. SDK completion and
repair passes alone do not establish comparative benefit.

This cohort runs the [prospectively frozen protocol](codegraph_matched_protocol_v2.md)
on three historically exposed Python repositories. It preserves the entire v1
cohort and changes no earlier outcome. The protocol froze at 14:04:01 UTC on
2026-10-28 in [#896](https://github.com/sysevol-ai/CodeNib/pull/896), before the
first new SDK query at 14:12:18 UTC. The batch sealed at 14:50:40 UTC; native
grading began after the launcher exited and ended at 14:55:14 UTC. No grading
feedback reached a later model task, and no task was replaced or rerun.

## Complete workflow costs

Every row contains lookup, flow, impact, repair and post-edit work. Wall time
starts before product installation/indexing/registration and ends after both
SDK sessions close. Common machine/source/project-dependency preparation and
later controller grading are separately excluded by the frozen policy. These
are measured costs on prepared runtimes, not new-machine installation times.

| Source | Arm | Product setup, s | Complete observed workflow, s | Optional MCP attempts | Native repair |
| --- | --- | ---: | ---: | ---: | --- |
| Requests | Serena | 3.942 | 161.559 | 0 | pass |
| Requests | CodeNib | 22.797 | 211.108 | 0 | pass |
| Requests | Codegraph | 6.461 | 165.277 | 2 | pass |
| Requests | Native | 0.000 | 167.054 | 0 | pass |
| Flask | Serena | 3.953 | 243.249 | 0 | pass |
| Flask | Codegraph | 7.008 | 215.156 | 2 | pass |
| Flask | Native | 0.001 | 176.233 | 0 | pass |
| Flask | CodeNib | 24.153 | 246.901 | 0 | pass |
| pytest | CodeNib | 38.367 | 208.996 | 0 | pass |
| pytest | Native | 0.000 | 168.438 | 0 | pass |
| pytest | Codegraph | 6.671 | 146.211 | 2 | pass |
| pytest | Serena | 4.002 | 163.192 | 0 | pass |

All nine optional-product setups succeed. Their cold and return SDK sessions
record connected MCP servers. This removes the v1 pytest setup failures as an
explanation for non-use in v2, without changing v1. Actual source candidate and
provider identities match the pre-inference binding; the public PyPI 0.2.4
release is unchanged.

## Observed choice and accounting

Serena and CodeNib receive no optional MCP calls. Inspection of every original
Bash request in their workflows also finds no product CLI invocation.
Codegraph receives six delivered `codegraph_explore` calls, distributed over
lookup and flow; its original Bash requests likewise contain no product CLI
invocation. Workspace names inside scoped shell commands are not counted as
product use. The [tool-choice audit](codegraph_matched_tool_choices_v2.json)
links each original request and actual delivered reply.

The 24 sessions record exact main assistant `claude-opus-5-5`, medium effort
where hooks expose it, native Max authentication and no API-key source or paid
fallback. Final cumulative main and auxiliary counters are counted once per
session, including input, cache creation, cache read and output: **9,389,337
normalized tokens** and **$9.9928829 in SDK estimates**. These estimates are not
invoices; actual charges and complete HTTP request counts remain unknown. The
user's authorized API ceiling remains $500. Prior matched/recovery estimates
remain separate and unchanged.

## Evidence and preservation

The [execution record](codegraph_matched_execution_v2.json) contains every
terminal, cumulative usage record, native grading outcome and linked original
grading file. The [capture manifest](codegraph_matched_v2_capture_manifest.json)
binds all twelve workflows to exact answers, requests, delivered tool messages,
source snapshots and repair patches. Native grading files use lossless
gzip/base64 parts, verified against every original byte and file hash. Four
original logs contain non-UTF-8 bytes; the initial encoding failure and a
superseded verbose packaging attempt remain recorded separately. No model,
repair grade, source judgment or accounting changed during repackaging.

The [review implementation and integrity receipts](codegraph_source_review_method_v2.json)
retain all post-run analysis sources, source-claim counterexamples and cleanup
checks. All 36 exact workflow/native-grading container identities are absent;
the separate source-audit container is also removed. All 34 frozen input files
and 140 preceding tracked CodeGraph JSON artifacts retain their bytes.

This is one attempt per task on three exposed source snapshots, with one
unblinded maintainer-agent explanation review and no external adjudicator.
Source ground truth and requested shell ranges are not claimed model-read
credit or a complete ContextBench score. No external participant is enrolled.
