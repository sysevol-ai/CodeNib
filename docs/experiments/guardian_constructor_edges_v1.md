<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Guardian check with constructor edges, v1

Linking constructor calls to the constructor they run lets the static guardian
catch 3 more of the 68 real agent regressions, at the same typical cost on
patches that broke nothing. Within three calls it now flags a broken test in 35 cases (51%),
up from 32 (47%). On the clean patches it still flags a median of 3 tests, with
a 90th percentile of 365. That meets the rule set before the run, so **constructor
edges should be added to CodeNib's graph**.

They do not change the [first check's](guardian_regression_check_v1.md)
conclusion. The best static variant reaches 41 of 68 (60%) at any depth, still
below the 70% bar, so a guardian still needs a record of which tests run which
code.

## What changed

A call `C(...)` runs C's `__init__` and `__new__`, but CodeNib's symbol graph
links the call to the class C only. A change inside a constructor was therefore
unreachable from the tests that construct the class. This round adds an edge
from each definition that calls a class to the constructor methods that the
call runs:

- **Which constructor.** The class's own `__init__` and `__new__`, or else the
  first ones found in its base classes, searched left to right.
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

The outcome is to add constructor edges to the graph. No case was lost, since
the edges only add paths.

## Which cases it caught

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

- **Add the edges to the graph build.** They cost about 10% more reference
  edges and nothing on clean patches at the primary depth. The same resolution
  rules can run where the graph is built, rather than on a stored copy.
- **Static impact still misses these regressions.** The remaining misses are
  framework calls by name and operators. The next step is the coverage check
  in [#975](https://github.com/sysevol-ai/CodeNib/issues/975).
- **Operators are a possible next edge type.** `a == b` runs the `__eq__` of
  a's class, and indexing runs `__getitem__`. Such edges need the operand's
  type, so they were left out here.

## Limits

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
