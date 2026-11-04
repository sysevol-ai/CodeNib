<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Guardian check on real agent regressions, v1

A guardian built only on the static code graph caught about half of these
regressions. It was given each patch, found the changed functions in CodeNib's
code graph, and flagged the tests that reach them within three calls. In 32 of
68 real agent regressions (47%), that flag set included a test the patch broke.
On patches that broke nothing, it flagged a median of 3 tests. The rule set
before the analysis required 70%, so the result is that **static impact misses
these regressions**.

The graph still did much better than choosing tests by import. Flagging every
test file that imports a changed module caught 22 of 68 (32%) and flagged a
median of 81 tests. Most of the graph's misses go through calls that a static
graph candidate did not resolve: framework code that calls methods by name, and operators
and constructors.

## Question and data

Some agent patches pass SWE-bench's own tests but break other tests in the
project's full suite. The study compares benchmark-selected tests with a broader
developer test suite, exposing regressions outside that selection. A guardian would read the
patch before it lands and point at the tests at risk.

- **Cases.** The [study by Wang, Pradel and Liu](https://arxiv.org/abs/2503.15223)
  and its [replication package](https://github.com/ZJU-CTAG/PatchDiff) ran the full suite on
  plausible SWE-bench Verified patches from three agents: CodeStory,
  LearnByInteract and OpenHands. Its published results list each patch that
  fails a test which passes with the developer's patch. Of these, 68 are
  functional failures, on 47 instances; 34 are Django and 21 are sympy.
- **Controls.** The same agents' other plausible patches on those 47 instances
  that broke nothing, 35 in all.

## Method

- **Changed code.** For each hunk in a non-test file, the innermost function
  that contains the change, in the code before the patch. A change outside any
  function maps to its class, and a change outside any class maps to the file.
- **Graph guardian.** It flags the test functions from which the changed code
  can be reached in at most k reference edges of CodeNib's symbol graph, built
  with the pyrefly provider. The graph is built at the instance's base commit,
  which is what an agent has when it submits. The primary depth is k = 3.
- **Baseline.** It flags every test in each test file that imports a changed
  module directly.
- **Measures.**
  - **Case recall:** the share of cases in which a broken test is flagged.
  - **Cost:** the number of tests flagged. The median over the controls is the
    usual price of a warning.

The protocol, with the decision rule, was written and hashed before any flag
set was computed for a regressing patch. It is in the evidence archive (SHA-256
`743aca56…`).

## Results

| Method | Cases caught (of 68) | Broken tests caught | Flagged on controls, median | Flagged on controls, 90th percentile |
|---|---|---|---|---|
| graph, 1 call | 19 (28%) | 9% | 0 | 189 |
| graph, 2 calls | 25 (37%) | 16% | 1 | 362 |
| **graph, 3 calls** | **32 (47%)** | **21%** | **3** | **365** |
| graph, 4 calls | 33 (49%) | 23% | 8 | 365 |
| graph, any depth | 36 (53%) | 28% | 13 | 5,026 |
| import baseline | 22 (32%) | 15% | 81 | 947 |

The repositories have about 10,000 to 15,000 test functions for Django and
7,000 to 11,000 for sympy, so a median of 3 is a tiny fraction of the suite.

Against the decision rule:

1. **Useful (failed).** It needed 70% case recall at a median control cost of
   at most 1% of the suite. The cost met that bar, but recall reached 47%.
2. **Better than files (held).** At 3 tests against 81, the graph costs less
   than half the baseline, and its recall is higher, not lower.

No method reached 70%, so the result is that static impact misses these
regressions. The graph beat the baseline, but neither is enough on its own.

| Repository | Cases | graph, 3 calls | graph, any depth | import |
|---|---|---|---|---|
| django | 34 | 17 | 19 | 11 |
| sympy | 21 | 9 | 10 | 7 |
| sphinx | 5 | 2 | 2 | 3 |
| xarray | 3 | 1 | 1 | 0 |
| scikit-learn | 2 | 2 | 2 | 0 |
| astropy, matplotlib, requests | 1 each | 1 of 3 | 2 of 3 | 1 of 3 |

## Why it missed

The graph found no path at all in 32 cases. Grouped by the changed function
and the broken test:

- **Framework calls by name, 18 cases.**
  - Django management commands run through `call_command` or `manage.py`.
  - ORM hooks that the query compiler calls (`as_sql`, `get_prep_lookup`,
    `convert_*_value`).
  - Admin methods reached through the test client.
  - Sympy methods called by name: `_eval_evalf`, `_eval_is_zero`, `_print_*`.
  - Sphinx hooks and transforms that the application runs.
- **Constructors and operators, 5 cases.** Changes to `__new__` reached through
  `Array(...)`, to `Float.__eq__` reached through `==`, and to a matrix entry
  method reached through indexing. The graph links a constructor call to the
  class, not to `__new__`.
- **Bad patch data, 3 cases.** For three CodeStory instances, the patch in the
  published data has no source change, only a reproduction script or nothing.
  The archived patch does not establish which source change caused the recorded
  failure. They count as misses here;
  without them, the graph caught 32 of 65.
- **Other, 6 cases.** In two, the broken tests do not exist at the base commit.
  In four, the check found no call path and the cause was not traced further.

On 13 of the 35 controls the graph flagged no test. That is cheap, but on these
projects it often means the changed code is reached only through such calls.

## What this means for a guardian

- **Static impact is a good first filter but not a guardian by itself.** It
  points at half the hidden regressions for a few dozen tests. The import rule
  catches fewer and costs far more.
- **Recall needs a record of which tests execute which code.** Calls by name
  and operators could be captured by a per-test execution map. Whether a map
  maintained across changes improves recall and cost is an untested hypothesis.
- **One graph gap can be closed directly.** A constructor call could count as
  reaching the class's `__init__` and `__new__`.

## Limits

- The cases come from agents of 2024 and early 2025 on SWE-bench Verified, and
  Django and sympy make up 55 of them.
- A case counts as caught when any broken test is flagged. A guardian that
  flags a test helps only if the test is then run, or the warning is read.
- No coverage-based guardian was measured. That needs each project's test
  environment, which this check did not use.
- This evaluates the historical v4 candidate recorded in `SOURCE`, before the
  current provider and path-query repairs. It does not measure the final merged
  product. Frozen scores and denominators remain unchanged.
- Of 507 broken-test labels, 413 match exact graph names, 63 match docstrings,
  11 use a same-file/name fallback and 20 remain unmatched. A fallback need not
  identify a unique physical test; unmatched labels remain in the denominator.

## Deviations

- **Diff parsing.** The `unidiff` library rejected three agent patches whose
  hunks are shorter than their headers say. A tolerant parser replaced it for
  all patches. It gives the same changed lines on every patch `unidiff` parses.
- **Plumbing check.** Before the cases ran, the scripts were checked on one
  Django instance outside the cases (django__django-10097), using its developer
  patch.
- **Pre-registration.** The protocol was recorded with its hash and time in the
  working directory, not published before the analysis.

## Evidence

[`guardian_regression_check_v1.tar.gz`](guardian_regression_check_v1.tar.gz)
(SHA-256 `e5aaf594a9fe54715299778c9714c283b438e45d52ac23a9b5d49e37c3eadc77`)
contains:

- the protocol and its hash record;
- the scripts that built the graphs, ran the check and applied the rule;
- every case and control with its patch and broken tests, from the study's
  CC BY 4.0 data on Zenodo (record 17074796);
- per-patch results, the summary and the build log, with each base commit and
  graph build time.

The graphs themselves are not included. Each was built in a median of 73 s.

### Replay the archived summary

The archived scripts use their own directory as the evidence root, although
the archive stores them under `scripts/`. Stage byte-identical copies beside
the data before running them. This packaging repair leaves the original
archive, protocol, scripts and results unchanged:

```bash
scratch=$(mktemp -d)
tar -xzf docs/experiments/guardian_regression_check_v1.tar.gz -C "$scratch"
evidence="$scratch/guardian_regression_check_v1"
cp "$evidence/scripts/build_graph.py" "$evidence/scripts/guardian.py" \
   "$evidence/scripts/summarize.py" "$evidence/"
cp "$evidence/summary.json" "$evidence/summary.recorded.json"
python "$evidence/summarize.py"
cmp "$evidence/summary.recorded.json" "$evidence/summary.json"
```

This replay regenerates the recorded summary byte for byte from all 103
per-patch records, without indexing or model calls. Rebuilding graph-derived
records is a separate operation: it needs the historical candidate identified
in `SOURCE`, the pinned base checkouts under `wt/`, and their repository mirrors
under `repos/`. Run the staged `build_graph.py` and `guardian.py` from that
candidate only after preparing those inputs. The archive alone supplies the
summary replay; it does not supply the 47 graphs or those checkouts.
