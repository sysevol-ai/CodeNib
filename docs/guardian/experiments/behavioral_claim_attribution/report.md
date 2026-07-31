# Behavioral-claim attribution across 120 solo trials

## Verdict

The expanded experiment does **not** support the narrow hypothesis that most
failures are caused by the agent choosing the wrong normative contract.

It supports a broader claim-centered hypothesis: agents often fail because
their model of behavior is wrong, but the dominant error is an
**implementation-satisfaction claim**—the agent has a substantially correct
contract and incorrectly concludes that its patch realizes that contract
across all relevant paths.

After correcting a verifier name-matching artifact:

- normative-contract errors account for **108/514 (21.0%)** failed F2P checks;
- implementation-satisfaction errors account for **406/514 (79.0%)**;
- implementation-satisfaction errors account for **3,428/3,428 (100%)**
  failed P2P checks.

At exact-trial level, 70 of 120 trials failed:

- 15 were purely normative-contract failures;
- 47 were purely implementation-satisfaction failures;
- 8 contained both.

Thus normative errors were causally present in 23/70 failed trials, while
implementation errors were present in 55/70. These incidence counts overlap
for the eight mixed trials and should not be added.

## Scope

This audit covers every original, no-context-injection solo result currently in
the workspace:

- 60 Python trials: five tasks, three models, four trials per setting;
- 60 language-stratified non-Python trials: five tasks, three models, four
  trials per setting;
- models: `gpt-5.6-luna`, `gpt-5.6-terra`, and `gpt-5.6-sol`;
- reasoning effort: medium.

All 120 metadata files and all 120 `pier_result.json` files are present. Both
matrices completed without launcher errors.

The trial-level attribution ledger is
[trial_attributions.csv](trial_attributions.csv).

## Attribution rules

### Normative-contract error

The agent adopted the wrong answer to “what should the system do?”

This label requires affirmative evidence in the patch, authored tests, or
trace—not merely a failed held-out test. Examples:

- rejecting IGEL include/exclude overlap as inherently invalid;
- treating an IPython replay failure as an exception that should escape;
- mapping Textual events to the wrong public key identity;
- stripping Markdown and wiki-link syntax from visible Obsidian TOC text even
  when formatting stripping is disabled;
- treating `file007` and `file7` as numerically different during natural sort.

### Implementation-satisfaction error

The intended contract was correct or substantially correct, but the patch did
not realize it. This includes missing paths, incorrect wiring, regressions,
mechanical defects, and an empty submitted patch. Examples:

- implementing FastAPI settings for one route path but not every producer,
  copier, and consumer;
- implementing Testem bail state but not all reporters, runners, and browser
  adapters;
- creating sqlite savepoints that existing helpers subsequently commit away;
- protecting `collapse_groups` in OXVG but not `remove_empty_containers`;
- failing to submit any patch in several Helm, fd, and Obsidian trials.

### Mixed trial

`reward` is binary and cannot be split. A trial is labeled mixed when at least
one F2P/P2P failure has each causal type. Individual failed checks are still
assigned separately.

### Measurement artifact

A failed metric entry is classified as measurement only when the underlying
test execution proves that the check passed but the grader failed to associate
the result with the expected check.

The coding is conservative: uncertainty defaults to implementation rather than
normative unless the trace or patch demonstrates an alternative expected
behavior.

## Recorded benchmark totals

| Metric | Passed | Total | Failed |
| --- | ---: | ---: | ---: |
| Reward | 50 | 120 | 70 |
| F2P | 3,359 | 4,224 | 865 |
| P2P | 69,208 | 72,636 | 3,428 |

Recorded F2P is 79.5%, but that number contains a large grading artifact.

## Obsidian name-matching defect

Ten Obsidian patches changed the locale-derived Jest suite label from the
expected `Auto Table of Contents` to `Auto TOC`. The feature suite actually ran
in all ten trials and reported between 30 and 40 passing checks out of 41.
However, the grader matched complete test names literally, could not find the
expected suite names, and marked every expected check as “missing from report.”

The affected executions contain:

- 351 feature checks that visibly passed but were recorded as failed;
- 63 checks that visibly failed.

One additional Obsidian trial submitted no patch and correctly had all 41
feature checks fail to run. Sol job 1 used the expected suite label and was
scored correctly at 37/41.

Correcting only the ten name-matching cases changes aggregate F2P from
3,359/4,224 to **3,710/4,224 (87.8%)**, leaving **514 real failed checks**.
It does not change reward because every affected trial still had at least one
real feature failure.

As-recorded attribution of the 865 F2P deficit is therefore:

| Attribution | Failed checks | Share |
| --- | ---: | ---: |
| Normative contract | 108 | 12.5% |
| Implementation satisfaction | 406 | 46.9% |
| Measurement artifact | 351 | 40.6% |

Among the 514 execution-confirmed failures:

| Attribution | Failed checks | Share |
| --- | ---: | ---: |
| Normative contract | 108 | 21.0% |
| Implementation satisfaction | 406 | 79.0% |

## Exact reward attribution

| Corpus | Failed trials | Normative only | Implementation only | Mixed |
| --- | ---: | ---: | ---: | ---: |
| Original Python 60 | 33 | 9 | 23 | 1 |
| Non-Python 60 | 37 | 6 | 24 | 7 |
| **Total** | **70** | **15** | **47** | **8** |

Two complementary readings are useful:

- mutually exclusive: 15 normative, 47 implementation, and 8 mixed;
- causal incidence: normative is implicated in 23/70 failed trials and
  implementation in 55/70.

Reward alone hides severity. A one-check semantic miss and a patch that fails
thousands of regression checks both lose one reward.

## F2P and P2P attribution by task

`N`, `I`, and `A` mean normative, implementation, and measurement artifact.
Reward entries use `N/I/M` for normative-only, implementation-only, and mixed
failed trials.

| Task | Failed reward N/I/M | F2P N | F2P I | F2P A | P2P I |
| --- | ---: | ---: | ---: | ---: | ---: |
| IGEL feature schema | 4/5/0 | 48 | 44 | 0 | 0 |
| Textual kitty phases | 3/5/0 | 14 | 15 | 0 | 0 |
| IPython session bundle | 2/1/1 | 3 | 4 | 0 | 0 |
| FastAPI implicit methods | 0/8/0 | 0 | 23 | 0 | 3,421 |
| sqlite-utils checkpoints | 0/4/0 | 0 | 18 | 0 | 0 |
| Testem bail | 0/11/0 | 0 | 158 | 0 | 7 |
| OXVG structural selectors | 0/7/0 | 0 | 22 | 0 | 0 |
| Obsidian auto TOC | 3/2/7 | 40 | 64 | 351 | 0 |
| Helm manifest stream | 0/3/0 | 0 | 15 | 0 | 0 |
| fd sorting | 3/1/0 | 3 | 43 | 0 | 0 |
| **Total** | **15/47/8** | **108** | **406** | **351** | **3,428** |

There are no normative-attributed P2P failures. All compatibility regressions
came from patches that failed to preserve existing behavior while implementing
the requested contract.

## Corpus comparison

| Corpus | Real F2P failures | Normative | Implementation |
| --- | ---: | ---: | ---: |
| Python 60 | 169 | 65 (38.5%) | 104 (61.5%) |
| Non-Python 60, corrected | 345 | 43 (12.5%) | 302 (87.5%) |
| **All 120** | **514** | **108 (21.0%)** | **406 (79.0%)** |

The first five Python tasks made the normative hypothesis look stronger because
they contain compact semantic traps: option composition, protocol grammar,
host failure representation, and public event identity. The new non-Python
sample contains larger propagation tasks. Agents generally understood their
explicit contracts but failed to implement every required integration surface.

Examples:

- Testem alone contributes 158 implementation-attributed F2P failures across
  reporters, runners, browser adapters, reset behavior, and config validation.
- OXVG patches commonly protected one optimizer rewrite while omitting another.
- Three Helm trials, one fd trial, and one Obsidian trial submitted no usable
  model patch.
- FastAPI contributes 3,421 P2P failures from propagation and compatibility
  defects.

## Concentration and metric interpretation

Failed-check counts are not counts of independent root causes.

- One FastAPI trial accounts for 3,134/3,428 (91.4%) P2P failures because an
  absolute-path assertion broke broad test collection.
- The ten Obsidian name mismatches account for 351/865 recorded F2P failures.
- One empty fd patch accounts for 43 F2P failures.

Therefore:

- reward measures the frequency of incomplete trials;
- failed F2P/P2P checks measure the benchmark impact of those defects;
- neither metric directly counts distinct mistaken beliefs.

The report preserves both views rather than treating thousands of cascading
test failures as thousands of independent reasoning failures.

## What this says about the hypothesis

The narrow hypothesis—

> failures are mainly caused by the agent choosing the wrong normative
> behavioral contract

—is not supported by the full 120-trial sample. Normative errors are important,
but they explain only about one fifth of real F2P failures and no P2P failures.

The revised hypothesis is supported:

> agents fail mainly because they make an unsupported behavioral claim at one
> of two layers: either the contract itself is wrong, or the agent incorrectly
> concludes that its patch satisfies the correct contract.

The second layer is quantitatively dominant. This changes the likely Guardian
intervention. Guardian must sometimes challenge expected semantics, but more
often it should challenge coverage and satisfaction:

1. State the normative contract.
2. Enumerate its implementation obligations across producers, state copies,
   consumers, modes, and public boundaries.
3. Ask which obligation lacks direct evidence.
4. Run a discriminating probe through that exact path.

A system focused only on recovering task purpose would miss most observed
failures. A system focused only on generic testing would also miss them,
because many failed agents already wrote tests derived from incomplete
implementations.

## Validity limits

- Attribution is post-hoc and oracle-informed. It uses held-out verifier
  reports and reference solutions and must not be injected into benchmark
  agents.
- Causal coding is human judgment. The conservative rule reduces, but does not
  eliminate, ambiguity between a misunderstood contract and a faulty
  implementation.
- The 120 trials cover ten tasks, not a random sample of all software work. The
  non-Python task selection was language-stratified, while the first five tasks
  were selected for earlier Guardian experiments.
- F2P/P2P counts are test-weighted. Tasks with more checks contribute more to
  aggregate impact.
- Mixed reward cannot be additively divided without choosing an arbitrary
  weighting.
