<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Guardian check with operator and unresolved-call edges, v1

Two more Python edge layers raise the change guard from 38 to **42 of 68**
historical agent regressions within three calls. On the 35 clean patches the
median number of listed tests goes from 11 to 13 (0.1% of the suite) and the
90th percentile from 402 to 446. Both meet the rule constructor and override
edges were admitted under. Three other layers gained nothing and are not
shipped.

The gain is smaller in what the agent actually sees. The guard shows the ten
tests nearest the change, and a broken test was among them in **18** of 68
cases, up from 16; among the twenty its run command uses, 22, up from 20. Most
remaining misses are not missing edges at depth three: eleven reach the change
in four to eight calls, and seven cannot be caught from the data at all.

## What was measured

The cases, controls, patch-to-line mapping, test identification, test matching
and import baseline are those of the
[first guardian check](guardian_regression_check_v1.md): 68 agent patches
that pass SWE-bench Verified's tests but break other tests in the project's
full suite, and 35 patches by the same agents on the same 47 instances that
broke nothing. A case is caught when any broken test is listed.

Graphs were rebuilt at every base commit with CodeNib `main` 2c8a166f and
pyrefly 1.4.0. The rebuild reproduces every published figure exactly:

| Edges | Caught (of 68) | Clean: median | Clean: 90th percentile |
|---|---|---|---|
| graph references | 32 | 3 | 365 |
| + constructor edges | 35 | 3 | 365 |
| + alias edges | 35 | 3 | 365 |
| + override edges (the guard on `main`) | 38 | 11 | 402 |

## Layers tried

Each layer is a general Python rule, derived from the graph and the source
like the constructor edges, and added to the guard's caller relation before
override edges, so overrides inherit the new callers.

| Layer | Rule | Caught | Clean: median | Clean: 90th percentile |
|---|---|---|---|---|
| operator | Code that refers to class C reaches C's special methods, its own or the first inherited ones; constructors, attribute access, pickling and copying hooks excluded | **40** | 13 | 444 |
| unresolved call, at most 3 | `x.name(...)` in a definition with no resolved reference to anything called `name` reaches every non-test method called `name`, when at most 3 exist | **40** | 13 | 444 |
| unresolved call, at most 5 | the same, at most 5 | 40 | 27 | 444 |
| unresolved call, at most 10 | the same, at most 10 | 40 | 28 | 446 |
| unresolved call, at most 20 | the same, at most 20 | 40 | 64 | 940 |
| prefix dispatch | `getattr(self, '_print_' + name)` reaches the methods named `_print_*` in the class family of `self` or `cls`, or everywhere for other receivers | 38 | 11 | 402 |
| pytest fixtures | a test or fixture reaches the fixtures its parameters name, nearest `conftest.py` first | 38 | 11 | 402 |
| unittest set-up | a test method reaches the `setUp`, `setUpClass` and `setUpTestData` defined in test files for its class and bases | 38 | 11 | 403 |
| **operator + unresolved call, at most 3** | both | **42** | **13** | **446** |
| operator + unresolved call, at most 20 + prefix dispatch | all three | 42 | 74 | 1,593 |

The rule from the constructor-edge check: at least two more cases, a median of
at most 1% of the suite, and a 90th percentile of at most 1.5 times the
reference-only 365, that is 547. Operator edges alone, unresolved calls at
most 3, 5 or 10, and both together pass. A limit of 3 is the cheapest at the
same gain and is the one shipped.

The four cases gained:

- **sympy-20801, two patches.** The change is in `Float.__eq__`, reached
  through `==` on floats the test builds. Both cases are one instance.
- **django-13121.** `convert_durationfield_value` is called on a backend's
  operations object whose type the checker does not know.
- **matplotlib-14623.** `set_ylim` is called on an axes object returned by
  `plt.subplots()`.

The cost is concentrated. Of 35 clean patches, 17 list more tests; one sympy
patch that also changes `Float.__eq__` goes from 0 to 6,581 of 10,617 tests,
because nearly every sympy test builds a `Float`. That is what the change can
reach, and the guard shows ten of them.

The product guard itself (`ImpactGraph.impact` on this branch, run through
`product_check.py`) gives the same 42, 13 and 446.

The shipped rule leaves out two kinds of call that the graph cannot tell from
unresolved ones, because it holds no built-in or standard-library methods: a
call on an imported name (`hashlib.sha256()`, `os.path.join()`), and a call
whose name is a method of a built-in container, string or number, or of a
standard-library file, path, logger, pattern or process (`rows.append()`,
`logger.info()`). Without them, every `x.append(...)` in CodeNib's own code
reached the three methods named `append` it defines, and a change to one of
them listed 4,861 tests. With them, CodeNib's graph at 2c8a166f has 1,493
unresolved-call edges instead of 10,213. On the 103 patches the result is
unchanged: 42 caught, 18 among the first ten, a clean-patch median of 13 and a
90th percentile of 446; 19 lists get shorter, the largest drop being 2,402 to
1,948 tests.

Building the guard's in-memory view takes longer: django 7.6 s to 10.0 s, sympy
24.9 s to 31.0 s, xarray 2.0 s to 2.9 s. Base classes are found once for
override and operator edges, and a file is parsed for unresolved calls only
when its text has a call of a candidate name; both give the same edges.

## What the agent sees

The guard orders the listed tests by distance and then by name, shows ten and
puts twenty in its run command. Counting a case only when a broken test is in
that prefix:

| Guard | Caught within 3 calls | Among the first 10 | Among the first 20 |
|---|---|---|---|
| `main` | 38 | 16 | 20 |
| this change | 42 | 18 | 22 |

Many of the 38 caught cases are found among many tests at the same distance:
in 30, fewer than twenty tests are nearer than the broken one, but in nine of
those the broken test ties with 121 to 2,354 others. Three fixed tie-breakers
were tried on the guard of `main`, with ties otherwise broken at random:
tests whose file imports a changed module first, tests whose path is most
like the changed file's first, and path similarity before distance. None
raised recall among the first 20 by more than two cases (20.4 of 68 by
distance alone, 21.8 and 22.2 with the first two), and path similarity before
distance lowered it to 16.9. The broken test is usually not the one whose file
looks most like the change; choosing among reachable tests needs a record of
which tests run the changed lines.

## Why the rest are missed

Of the 26 cases still missed within three calls:

- **7, not catchable from the data.** Three published patches change no source
  file; in four, the broken test does not exist at the base commit.
- **3, a separate process.** Django's `admin_scripts` tests run `manage.py` in a
  subprocess; guessed edges reach them only at 25 calls.
- **11, more than three calls.** Reached in 4 to 8 calls. With both layers, a
  limit of four calls catches 46 at a median of 22, but its 90th percentile of
  554 is just over the 547 limit.
- **5, no path.** Sphinx directives and transforms registered by name, sympy
  assumptions computed by generated properties (`x.is_zero` reaches
  `_eval_is_zero` only at run time), an admin view reached through the test
  client, and one broken test unrelated to its patch.

## Limits

- The 103 patches were used to choose both layers and the limit of 3, after
  constructor and override edges were chosen on the same data. Each further
  selection on the same patches is weaker evidence than the first.
- Operator edges gain on one instance only.
- Operator edges approximate the receiver by the classes a definition refers
  to, and inherited methods are found depth first, not by Python's method
  resolution order, like constructor edges.
- Python only.

## Reproduce

`guardian_recall_edges_v1.tar.gz` (SHA-256
`0be7bc3e51f83b18d8665eb64119f6266885f6d40476417cede43abfc25e314c`) holds the
scripts, every per-patch result, the build log and `summary.json`. `SOURCE`
says how the graphs were built; graph files are not included. Building the 47
graphs takes about 30 minutes with four in parallel, and each layer run 10 to
20 minutes.
