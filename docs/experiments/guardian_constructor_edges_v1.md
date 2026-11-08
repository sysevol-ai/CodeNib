<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Guardian check with constructor edges, v1

Adding approximate constructor-reference edges lets the static guardian flag
a broken test in 35 of 68 historical agent regressions (51%) within three
calls, up from 32 (47%). On the 35 clean patches the median remains 3 flagged
tests and the 90th percentile remains 365, although four individual flag sets
grow. This meets the original gain/cost rule. **The change guard
([#984](https://github.com/sysevol-ai/CodeNib/pull/984)) now adds these edges to
its in-memory Python call graph with the same depth-first lookup**, so the
lookup limit below applies to it. Persisted graphs are unchanged, and the
lookup has not been validated against Python's method resolution order (MRO).

The archived protocol and summary retain their original recommendation to add
the edges. Review found that the archived depth-first lookup can select a
different constructor from Python's C3 MRO. This qualification preserves the
original scores, scripts, decision rule and archive; it does not rerun or
regrade the experiment.

The constructor-edge results preserve the [first check's](guardian_regression_check_v1.md)
conclusion. The best static variant reaches 41 of 68 (60%) at any depth, still
below the 70% bar, so a guardian still needs a record of which tests run which
code.

## What changed

A class call can execute construction methods such as `__new__` and
`__init__`, while CodeNib's symbol graph links the call to the class itself.
Missing constructor links can therefore make a constructor change unreachable
from tests that construct the class. This experiment adds approximate reference
edges from calling definitions to candidate constructor methods:

- **Which constructor.** The class's own `__init__` and `__new__`, or else the
  first ones found by a depth-first, left-to-right search of its base classes.
  This is an approximation rather than Python's C3 MRO.
- **Which calls.** The calling definition must reference the class in the graph
  and contain a call by the class's name. Import aliases (`from … import C as
  D`) and module-level aliases (`D = Distance` in Django's GIS code) count.
- **Not covered.** Calls through variables such as `cls(...)` or
  `type(self)(...)`, dataclass and metaclass constructors, and operators.

Everything else is as in the first check: the same patches, broken tests,
mapping from a patch to changed code, depths and import baseline. The new edges
add a median of about 14,000 edges per graph, about 10% of its reference edges.

## Results

| Method | Cases caught (of 68) | Broken tests caught | Flagged on controls, median | Flagged on controls, 90th percentile |
|---|---|---|---|---|
| graph, 3 calls (first check) | 32 (47%) | 21% | 3 | 365 |
| **constructor edges, 3 calls** | **35 (51%)** | **22%** | **3** | **365** |
| constructor edges, 4 calls | 35 (51%) | 24% | 9 | 444 |
| constructor edges, any depth | 41 (60%) | 29% | 24 | 5,431 |
| any reference, 3 calls | 35 (51%) | 22% | 6 | 365 |
| import baseline | 22 (32%) | 15% | 81 | 947 |

"Any reference" is a bound set before the run. It adds the same edges from
every definition that references the class, without checking for a call. It
catches the same cases at three calls, at twice the median cost. At four calls
it catches 38 at a median of 13, against 35 at a median of 9 with the call
check.

Against the decision rule:

- **Gain:** 3 more cases at three calls, against a required 2.
- **Cost:** the median on controls is 0.06% of the repository's test
  functions, against a limit of 1%. The 90th percentile is 365, unchanged and
  within the limit of 1.5 times the first check's.

The archived rule's outcome is to add constructor edges to the graph. It is a
decision under this experiment's approximate resolver, not validation of
runtime constructor identity. No previously caught case was lost, since the
edges only add paths. The guard adopted the resolver as measured here; a
resolver that follows C3 MRO needs its own validation, without replacing this
frozen run.

## Which cases it caught

These are paths in the augmented static graph, not verified runtime traces.

- **xarray-4629, two patches.** The first check found no path. The broken test
  builds its data with a helper that calls `Dataset(...)`. The new edge leads
  into `Dataset.__init__`, which calls into the changed file:
  `test_copy → create_test_data → Dataset.__init__ → merge_data_and_coords`.
  Both patches change module-level code in `merge.py`, so the method treats the
  whole file as changed.
- **astropy-14508, one patch.** The first check reached it only beyond three
  calls. Now it is reached in three:
  `test_floating_point_atol → HeaderDiff.__init__ → Header.fromstring → Card`.
- **sympy-15017, three patches.** This is the `Array(...)` case named in the
  first check. It is now reachable, but only in five calls, through `sympify`,
  which converts array-like input with `Array(...)`. At that depth the guardian flags 7,750 of sympy's 8,788
  test functions, so the catch is of no practical use.

The two remaining constructor-and-operator misses from the first check go
through `==` and indexing, which this round does not cover.

## Cost on individual patches

At three calls, the flag sets grew on 4 of the 35 controls, by at most 15
tests. They grew on 17 of the 68 cases, by a median of 225 tests among those
17 and at most 1,579. The added cost falls mostly on the cases.

## What this means

- **Validate the resolution the guard now uses.** The experiment adds
  about 10% more reference edges. At the primary depth, the control median
  and 90th percentile are unchanged, but four of 35 controls flag more tests.
  The guard's lookup is still the depth-first approximation; validate a
  resolver against C3 MRO and ambiguous or unsupported constructor cases
  before adding the edges to persisted graphs.
- **Static impact still misses these regressions.** The remaining misses are
  framework calls by name and operators. The next step is the coverage check
  in [#975](https://github.com/sysevol-ai/CodeNib/issues/975).
- **Operators are a possible next edge type.** `a == b` runs the `__eq__` of
  a's class, and indexing runs `__getitem__`. Such edges need the operand's
  type, so they were left out here.

## Limits

- **Constructor lookup can select the wrong inherited method.** If `B(A)`
  inherits `A.__init__`, `C(A)` overrides it, and `D(B, C)` defines neither
  constructor, Python's MRO is `D, B, C, A, object` and selects `C.__init__`.
  The archived depth-first search instead selects `A.__init__` through `B`.
  The measured gain belongs to this approximate graph; it does not establish
  that all added edges identify constructors that execute at runtime.
- **The call check is by name.** A definition that references class C and also
  calls a different callable of the same name gets a wrong edge.
- **Calls through variables are missed**, such as `cls(...)`.
- **The data has the same limits as the first check.** The cases come from
  agents of 2024 and early 2025, and Django and sympy make up 55 of the 68.

## Deviations

- **The bound variant's first run did not start.** It was launched from the
  wrong working directory, failed before it started, and was started again
  unchanged.
- **Pre-registration.** As in the first check, the protocol was recorded with
  its hash and time in the working directory, not published before the run. It
  was written after the first check, whose misses motivated it.

## Evidence

[`guardian_constructor_edges_v1.tar.gz`](guardian_constructor_edges_v1.tar.gz)
(SHA-256 `621fce0a1add3d332c6708a029c015987fdf9304460c8a959bbc71661ca22bfc`)
contains:

- the protocol (SHA-256 `2e69ee98…`) and the record of its hash and time, with
  the hashes of the scripts it ran;
- the scripts that add the edges and apply the rule, and the first check's two
  scripts, unchanged;
- per-patch results for both variants, the edges added per repository, the
  summary and the run logs;
- the run on the first check's plumbing instance, which is not a case.

The cases, controls and graphs come from the
[first check's archive](guardian_regression_check_v1.tar.gz).
