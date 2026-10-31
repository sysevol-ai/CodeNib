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
The focused builder/search suite passes 41 cases. The final combined unit tier
passes 8,967 cases with 51 skips, 212 marker deselections and 46 warnings after
pinned formatting. The earlier 8,963-case run remains retained separately.

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
checks, builder regressions and installed before/after migration. The developer
overlay is not an unmodified CI build; current CI admission remains open.
No model SDK question or new cohort runs. Prior scores stay frozen, independent
successes remain 0/2 and external enrollment remains 0/5; public 0.2.4 is unchanged.
