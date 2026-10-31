# Discover validation code in BM25 source queries

The current BM25 candidate keeps selected test files in the index and applies
test filtering when a query asks for it. The existing default had removed tests
during construction, so `filter_test=false` could not find validation code in
a real maintenance task. This fixes source discovery; it does not establish
whole-workflow savings or typed-graph benefit.

## Maintenance observation

An internal audit of [#867](https://github.com/sysevol-ai/CodeNib/issues/867)
needed to locate the existing swapped-root safety test. The admitted CI product
indexed 655 CodeNib source files and zero test files. Its actual MCP query for
the exact test name/path returned unrelated implementation files, while a
direct source read of the test succeeded. The source was accessible but absent
from ranked discovery. Existing ARB controls pass 27 tests; #867's unsupported
concurrent nesting remains a separate, open issue.

Both BM25 construction paths inherited the standalone chunker's test-skipping
default. The repair sets `filter_tests=false` in the normal and retained-source
builders, preserving explicit repository exclusions. Builder identity 10 records
the coverage policy so a version-9 view rebuilds on the next initialization,
even when the source has not changed. The standalone chunker default, graph
layout and provider preparation policy are unchanged. Implementation-only
repository search also excludes tests before filling its bounded candidate
pool; explicitly including supporting evidence still admits them.

## Verification

Four regressions covering directory/suffix test names through both builders
fail on unchanged production code and pass after repair. Two explicit-exclusion
controls continue to pass. Two additional bounded-pool regressions fail on the
old search executor, while both supporting-evidence opt-in controls pass.
The first focused builder/search suite passes 41 cases, and its unit tier
passes 8,967 cases. The subsequent real-ranker repair below passes 356 related
cases with one skip and seven marker deselections. Its final combined unit tier
passes 8,976 cases with 51 skips, 212 marker deselections and 46 warnings after
pinned formatting. Both earlier unit runs remain retained separately.

The same installed MCP driver runs before and after a real recipe update on
one unchanged CodeNib checkout and cache location. The baseline uses actual CI
wheel `53b58f6b`. The first developer wheel `c17d324f` changes only the BM25
builder and retains its later-discovered ranking regression. The final developer
wheel `a1a1c9db` changes the builder and repository-search executor plus wheel
`RECORD`; all other packaged Python/native/worker bytes agree.

| Observation on the same selected source | Before | After |
| --- | --- | --- |
| Indexed source files | 655 | 1,161 |
| Indexed test files | 0 | 506 |
| Exact requested safety test discovered | No | Yes, complete verified function |
| `filter_test=true` omits test matches | Yes | Yes |
| Direct test source read | Available | Available |
| Selected source identity after querying | Unchanged | Unchanged |

The selected view retains 2,549 files and fingerprint `e3099e23`. The after
phase calls the ordinary update path with `rebuild=false`; its version-9
manifest becomes version 10 without editing source. The original index is
archived before replacement. Both contexts close and graph preparation stays
deferred. Observed costs remain in the receipts without a timing-comparison
claim.

The unchanged three-case deterministic Ask check passes 2/3 cases on the old
index. The first coverage candidate loses the Wiki grounding case and passes
1/3; that failure remains retained. Filtering implementation-only test hits
before the candidate limit restores the original 2/3 pass set in the installed
second candidate. The stale-semantic-view case fails before and after, with
the same missing required groups. This is parity on the existing checks, not
a green overall Ask gate. Queries, required paths and `top_k` are unchanged.

The [exact evidence packet](codegraph_bm25_test_coverage_2026_10_31.json)
retains the maintenance replies, original discovery miss, source/version
checks, builder regressions and installed before/after migration.

## Preserve implementation hits beyond the inner limit

The first PR head's unmodified CI wheel `737d768c` matches all 486 Python files,
the native module and worker bytes of developer candidate `a1a1c9db`. Its fresh
installed migration and unchanged Ask checks reproduce the discovery gain and
2/3 parity. All 17 executed source-head checks pass with eight explicit skips.
The [CI packet](codegraph_bm25_test_coverage_ci_2026_10_31.json) also retains a
valid unresolved review: BM25 applied test filtering after its own maximum of
128 candidates. Sufficient test hits could still hide all implementation code;
the original mocked candidate-pool tests did not exercise that inner limit.

Six real-ranker regressions reproduce this failure in that exact installed CI
product, while three preservation controls pass. They cover candidate limits
3 and 128, eager and reloaded indexes, and the real implementation executor.
The correction selects eligible documents from the original complete BM25
score/tie order before applying its limit. It does not change the shared limit,
unfiltered ranking, query budget or index serialization. All nine cases now
pass, and unfiltered behavior stays unchanged after filtered queries.

The [inner-limit packet](codegraph_bm25_inner_cap_2026_10_31.json) retains the
review reproduction, initial fixture import errors, installed baseline and
final source/related/full-tier verification. The unchanged source Ask check
still passes the same 2/3 cases.

The revised unmodified CI wheel `8b3ca291` matches all 486 Python files of
reviewed source `bcc056fe`; its native module and worker remain byte-identical.
The unchanged installed real-ranker controls now pass all nine cases, including
all six failures reproduced in the prior CI product. The same fresh installed
MCP driver also preserves unchanged-source migration, complete test discovery,
test exclusion and closed contexts. Its unchanged Ask check retains exactly
the baseline 2/3 pass set, including the same missing required groups.
The [revised CI packet](codegraph_bm25_inner_cap_ci_2026_10_31.json) retains
these actual replies, the original index archive, updated manifest, resolved
review and negative diagnosis of the remaining Ask miss. All 17 executed
source-head checks pass with eight explicit skips. Exact final-head checks and
reviews are tracked separately in [#951](https://github.com/sysevol-ai/CodeNib/pull/951).
No model SDK question or new cohort runs. Prior scores stay frozen, independent
successes remain 0/2 and external enrollment remains 0/5; public 0.2.4 is unchanged.
