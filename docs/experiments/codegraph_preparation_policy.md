# Reduce repeated source-selection work during graph preparation

CodeNib's graph publication gate now reuses the eligibility decision for each
file within one validation call. On one complete captured CodeNib Python graph,
three fresh-object repeats per implementation reduce median gate time from
27.925s to 2.084s, with identical query-surface, ordered occurrence and quality
report outputs. This is a component result; complete first-query, post-edit and
coding-task savings have not been measured for this candidate.

The [exact evidence packet](codegraph_preparation_policy_2026_10_30.json)
retains 130 text records in three bounded parts, including unsuccessful setup
attempts. Binary graph, SCIP, profiler and wheel artifacts remain local and
hash-bound. Adoption A0 and [the pilot](codegraph_net_benefit.md) continue to
hold promotion: no qualifying whole-workflow advantage, independent success
confirmation or external participant is added.

## Why preparation repeated work

Before a graph is published, CodeNib checks that each node, reference anchor
and indexed occurrence belongs to the selected source. Many records point to
the same file. Normalizing and applying the same immutable file-selection
policy again for each record adds work without adding a different verdict.

A model-free, instrumented installed-wheel query identifies 4,788,451 file
visibility checks in one full preparation. The gate consumes 132.831s under
the profiler. These profiler times include substantial instrumentation
overhead and overlapping call stacks; they are not production timings and
must not be summed into a speedup claim. The earlier uninstrumented
[internal maintenance control](codegraph_internal_maintenance.md) retains its
210.506s first graph request and 219.915s post-edit request unchanged.

The implementation in `constrain_graph_to_source_selection` copies the source
selection, then remembers a plain string's canonical path and selection state
until that call ends. All per-record and required-owner checks remain, as do
directory pruning, occurrence filtering and range-index rebuilding. Invalid,
unhashable and nonplain string values retain their original validation path.
No verdict crosses a policy change or source generation; no stored cache,
graph layout, builder identity, backend or provider changes.

## Fixed-artifact comparison

Both source-checkout workers use the same captured 1,172-document selection:
33,178 nodes, 159,676 edges and all 786,078 ordered occurrences. Each worker
loads fresh objects before each timed call. Baseline and candidate workers
run sequentially on the same Python/igraph environment. Loading and hashing
are excluded from gate timing. The final candidate is measured after pinned
formatting; the earlier prototype measurements remain recorded separately.

| Implementation | Three gate times (seconds) | Median |
| --- | --- | --- |
| Before this change | 28.030, 27.828, 27.925 | 27.925s |
| Final candidate | 2.089, 2.084, 2.083 | 2.084s |

The median reduction is 92.54%. All repeats preserve the complete reported
node/edge query surface, every ordered occurrence and the entire quality
report. Input artifacts remain unchanged. This is one exposed repository
artifact with three repeats, not independent repositories or a paired
whole-preparation benchmark. It supplies no Serena/native comparison.

## Correctness and installed admission

A deterministic regression first reproduces thousands of policy evaluations
for two repeated file paths, then verifies one evaluation per path with all
kept occurrences intact. Other controls cover policy changes between calls,
invalid unhashable paths and absent required definition paths. The targeted
compiler/source/occurrence suite passes 518 cases. The complete local unit
tier passes 8,989 tests, with 24 skips, 221 marker deselections and 46 existing
fork deprecation warnings.

A separately installed local wheel admits all 485 Python files and its native
extension from outside the analyzed checkout, without a source overlay. It
repeats full gate parity and serves a real source-verified persisted-graph MCP
query. The profile and installed replies supply twelve exact source windows;
all match the unchanged 2,390-file profile checkout. All four exact owned
containers are independently absent.

This wheel is locally built, not a published or CI-produced release. Its
frontend assets reuse 106 verified CI-wheel files after unchanged web-source
checks. Installed admission reuses the captured complete index and runs
alongside unit verification; neither its gate duration nor MCP duration is a
performance endpoint.

| Retained unsuccessful attempt | Correction and limit |
| --- | --- |
| Initial profiling entry passes a repository directory to a manifest-only entry | Use the normal MCP CLI in a separate attempt; the first attempt submits no query. |
| Initial unit run lacks the developer native extension | Build the existing native implementation; retain the original guard and failed log. |
| Second unit run uses a task cache root whose basename is not `.codenib` | Correct the environment; preserve the namespace assertion. |
| Initial wheel builds omit native code because Python headers are unavailable | Reject those wheels; extract temporary development headers and build protocol 6. No dependency or global header installation changes. |
| Initial installed admission requests an unrelated full workspace-provisioning capability denied by Docker's default `kcmp` policy | Remove that extra harness assertion in a new attempt. Native import and actual graph directory guards remain required; no product safety check or container privilege changes. |

The next gate is matched complete first-query and post-edit preparation, then
a useful actual coding workflow. Keep all prior outcomes and the unopened
independent confirmation set intact. This iteration submits no model query
and changes neither the authorized budget nor the external enrollment gate.
