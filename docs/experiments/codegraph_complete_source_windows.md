# Complete source windows in exploration

Exploration now keeps a ranked short definition together and reuses unused
excerpt budget. This fixes a source-delivery defect: a natural-language query
could return a function's matching header and surrounding code while dropping
its guard or return statement, even when the complete body fit the total budget.
Model-answer quality and complete-workflow benefit remain unproved.

## Delivery change

A definition with a known multi-line scope that fits the selected line window
gets its own unpadded window. Requested definitions keep their existing priority.
Reference-only results, unresolved ranges and single-point LSP selections retain
their contextual behavior. Long scopes keep the existing bounded live-source
selection and explicit omission metadata.

The composed exploration caller also recycles a short excerpt's unused share
among already admitted excerpts. That can keep a larger function complete
without increasing the aggregate, per-excerpt, line, window or encoded-response
caps. Other users of the shared projector retain its prior default. No ranked
rows are reordered by the allocator, and metadata-only rows remain subject to
the existing admission limit. Window admission can still omit lower-ranked
anchors when the fixed window count is exhausted.

Tool signatures, schemas, descriptions, visibility, preload and instructions
are unchanged, as are source verification, dependency preparation and routing.
Grep/Jev planning, credentials and cost policy are unchanged; its composed
excerpt allocation can use the same budget-reuse path. No grep/Jev result is
used as CodeGraph evidence.

## Development control

The [control packet](codegraph_complete_source_windows_2026_10_30.json)
retains 33 lossless records in two bounded parts. A network-disabled owned
container installs the admitted main wheel, builds one BM25 artifact for the
pinned CodeNib source snapshot, then compares the baseline with an installed
two-module overlay. Every import is from the installed package outside the
analyzed checkout. All 485 Python files and the native extension are verified;
only the two intended module hashes differ. This is an overlay prototype,
not admission of a separately built candidate wheel.

The six development queries include the two exposed maintainer requests from
[#930](https://github.com/sysevol-ai/CodeNib/pull/930) and four additional
source questions. Each query uses a fresh real stdio connection in both
conditions. Source/index/runtime are shared; metadata, instructions, retrieval
plans, route/provider status, dependencies and diagnostics match. All twelve
new observations pass file/version/visible-line checks and the original caps.
The original recorded replies, model answers and scores are unchanged; these
new runs are not byte-for-byte replay oracles for earlier observations.

| Development query | Baseline serialized bytes | Candidate serialized bytes |
| --- | --- | --- |
| Exposed stale-source request | 59,553 | 60,706 |
| Exposed dependency-policy request | 61,153 | 56,521 |
| Source-excerpt limits | 58,041 | 51,535 |
| Source-status verification | 64,086 | 60,084 |
| Required and optional views | 54,564 | 56,890 |
| Wiki chapter persistence | 40,133 | 33,409 |
| Total | 337,530 | 319,145 |

These figures measure the product's canonical MCP call-result serialization,
including structured and compatibility-text copies. The total is 5.4% smaller,
while two responses grow. It does not measure actual SDK input tokens, charges
or complete task time. The three named scopes from the exposed questions
(`indexed_query`, `_indexed_tool_call`, `_prepare_graph_for_call`) are fully
visible in the candidate, versus none in the baseline. That is a bounded
source-delivery diagnostic, not recall, answer correctness or an independent
success confirmation. Both conditions use BM25-only retrieval with graph
routing unavailable; no typed-graph use is established.

## Verification and open gates

The expanded definition regressions reproduce twelve baseline failures and
twelve passes. The smallest updated surface passes 49 tests. Complete MCP and
projector checks pass 455 tests with seven skips under both MCP 2.0.0 and 2.3.0.
The local unit tier passes 8,963 tests with 24 skips. Pinned changed-file hooks
pass.

The initial full-unit environment lacked the workspace-owner extension, and
the optional local native build could not find Python headers. Those failures
are retained. Unit verification then uses the admitted CI ABI3 owner extension
after checking that its native source and build configuration are unchanged;
the reproduced failure passes before the complete unit tier is rerun. This
does not claim a successful local native compilation.

The sole control container is independently absent. All 7,357 prior sealed
inputs remain unchanged. There is no new SDK/model session, grading revision,
reserved-task exposure or external-user observation. The old two-task complete
acceptance remains 0/2.

Admit the exact candidate wheel before any new model test, and freeze new
tasks, criteria, limits and executable identities before inference. Preserve
the correctness/20% complete-time/normalized-usage gates, representative
updates, both independent success confirmations and external consent/seven-day
reuse at 0/5. No outreach is authorized. Current outcomes and open gates belong
to [adoption A0](../adoption_roadmap.md) and
[#874](https://github.com/sysevol-ai/CodeNib/issues/874). Public package 0.2.4
is unchanged.
