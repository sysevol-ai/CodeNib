# Proposal-ready report: local specifications as an agent intervention

## Executive summary

This experiment asks whether coding-agent failures are often caused by an
incorrect or incomplete model of the behavior the patch must preserve, rather
than by code localization or mechanical implementation alone.

The evidence is promising but should be described as a diagnostic ceiling
result:

- In a prior post-hoc audit of 120 no-context solo trials, a wrong or incomplete
  obligation model was implicated in 50/65 (76.9%) causally analyzable failed
  trials. In the five Python tasks used here, it was implicated in 21/33
  failures (63.6%).
- A universal generic review prompt did not help. It increased input tokens by
  32% and authored tests by 71%, but exact success fell from 27/60 (45.0%) to
  25/60 (41.7%).
- A first set of task-specific obligation prompts also failed overall because
  several obligations were ambiguous or wrong. It achieved 25/60 (41.7%),
  and an invented artifact-relocation obligation drove IGEL from 3/12 to 0/12.
- After correcting those prompts to state concrete, repository-grounded
  obligations, the completed v2 experiment achieved 39/60 exact successes
  (65.0%), versus 27/60 (45.0%) in the original no-context baseline. F2P rose
  from 91.57% to 93.66%, and P2P rose from 93.31% to 100.00% after rounding.

The result supports a specific proposal direction:

> Guardian should not merely ask the solver to review more. It should construct
> and challenge a task-specific behavioral-obligation model, then communicate
> the highest-impact missing or weakly supported obligations back to the
> solver.

The experiment does **not** show that Guardian can already discover those
obligations autonomously. The v2 prompts use held-out tests and reference
solutions after the fact, so they estimate the value of correct obligation
information if Guardian could produce it.

## Background and motivation

### The observed Guardian failure mode

Earlier Guardian runs suggested that localization was not the main bottleneck.
Agents usually found the relevant files and implemented the central happy path.
They failed when they generalized a partially supported belief:

- a selector helper worked, so all fit/evaluate/predict paths were assumed to
  obey the same schema;
- several keyboard sequences parsed, so the full protocol grammar and public
  event semantics were assumed correct;
- one FastAPI route worked, so propagation through every helper, router
  inclusion, and generated OpenAPI artifact was assumed complete;
- a SQLite savepoint existed, so helper-level commits were assumed unable to
  escape it;
- an IPython serializer worked, so real shell lifecycle and output-channel
  capture were assumed correct.

This is better described as premature convergence on an incomplete behavioral
model than as failure to retrieve the right code.

### Behavioral obligation

A behavioral obligation is a falsifiable requirement of the form:

> Under condition or execution path X, the system must produce behavior Y or
> preserve invariant I.

It covers both value semantics and repository-wide applicability: producers,
consumers, copied state, modes, lifecycle phases, public APIs, legacy paths,
and compatibility invariants.

An obligation-model failure occurs when explicitly stating the missing or
corrected obligation before implementation would have changed the agent's plan,
patch scope, or test scope. An implementation failure occurs when the agent
already represented the exact obligation but realized it incorrectly.

## Research question and hypotheses

The main research question is:

> Does supplying a correct, concrete behavioral-obligation model improve coding
> agents relative to the same agents receiving only the task instruction?

The experiment tests three related hypotheses:

1. **Generic review is insufficient.** Asking for broader review or more tests
   does not reliably repair a wrong task model.
2. **Correct task-specific obligations are useful.** Explicit conditions,
   expected behavior, affected paths, and discriminating probes improve scope
   and verification.
3. **Obligation precision matters.** Incorrect or invented obligations can
   coherently redirect the agent toward a worse patch.

## Experimental lineage

| Condition | Prompt | Trials | Exact success | F2P | Interpretation |
| --- | --- | ---: | ---: | ---: | --- |
| Original | No injected context | 60 | 27/60 (45.0%) | 1,835/2,004 (91.57%) | Baseline |
| Universal review | Same generic review checklist for every task | 60 | 25/60 (41.7%) | 1,820/2,004 (90.82%) | More review did not improve understanding |
| Obligation v1 | Task-specific, oracle-informed obligations | 60 | 25/60 (41.7%) | 1,707/2,004 (85.18%) | Ambiguous/wrong obligations can harm |
| Obligation v2 | Corrected concrete, oracle-informed obligations | 60 | 39/60 (65.0%) | 1,877/2,004 (93.66%) | Diagnostic value of correct obligations |

This lineage is important. Comparing only the original baseline with v2 could
suggest that any task-specific hint helps. The universal and v1 failures show
something narrower: the intervention must contain the *right behavioral
model*, not simply more instructions or more task-related text.

## v2 experiment design

### Matrix

- Tasks: five Python DeepSWE tasks:
  - IGEL persisted feature schemas;
  - Textual Kitty keyboard phases;
  - IPython session bundle replay;
  - FastAPI implicit HEAD/OPTIONS;
  - sqlite-utils safe import checkpoints.
- Models: `gpt-5.6-luna`, `gpt-5.6-terra`, and `gpt-5.6-sol`.
- Reasoning effort: medium.
- Trials: four per task/model setting, 60 total.
- Concurrency: three.
- Baseline being compared: the original four-trial-per-setting, 60-trial
  no-context matrix.
- Intervention: one v2 prompt selected by task and appended to the original
  task instruction.

### Prompt construction

The v2 prompts were built post hoc from:

- the public task instruction;
- reference solution;
- held-out feature and regression tests;
- traces and patches from the original and v1 runs.

Each prompt attempts to state four things:

1. the triggering condition or public entry point;
2. the exact observable behavior or invariant;
3. every relevant mode, lifecycle, or repository surface;
4. a probe that distinguishes a likely wrong interpretation.

Examples include:

- IGEL must honor configured artifact paths and store a directly loadable
  schema path; v2 removes the unsupported bundle-relocation requirement.
- Textual phase parsing applies to CSI-`u` and functional final-byte sequence
  families, not only one syntax.
- IPython must use the real `InteractiveShell` lifecycle and keep explicit
  streams, display output, expression results, and failures separate.
- FastAPI must preserve omission through nested router inclusion and derive the
  path-wide OPTIONS payload from the actual OpenAPI path item.
- sqlite-utils must preserve checkpoint ownership across helper commits and
  obey exact invariant, failure-result, strict-mode, and real CLI-upsert
  semantics.

Because these facts include oracle information, v2 is a contaminated
diagnostic intervention, not a valid benchmark submission.

## Quantitative results

### Aggregate

| Metric | Original | Obligation v2 | Change |
| --- | ---: | ---: | ---: |
| Exact success | 27/60 (45.0%) | 39/60 (65.0%) | +20.0 percentage points |
| F2P | 1,835/2,004 (91.57%) | 1,877/2,004 (93.66%) | +2.10 points |
| P2P | 47,699/51,120 (93.31%) | 51,118/51,120 (100.00%) | +6.69 points |

The exact-success risk ratio is 1.44: a v2 trial passed 44% more often than an
original trial in this sample. An unstratified two-sided Fisher exact test gives
`p = 0.043`. This is useful supporting evidence, but not a confirmatory test:
the intervention is oracle-informed, the tasks were selected rather than
randomly sampled, and the interim 30-trial result was inspected before the
planned jobs 3 and 4 were completed.

### By task

| Task | Original exact | v2 exact | Original F2P | v2 F2P |
| --- | ---: | ---: | ---: | ---: |
| IGEL | 3/12 (25.0%) | 7/12 (58.3%) | 196/288 (68.06%) | 215/288 (74.65%) |
| Textual | 4/12 (33.3%) | 5/12 (41.7%) | 247/276 (89.49%) | 257/276 (93.12%) |
| IPython | 8/12 (66.7%) | 11/12 (91.7%) | 197/204 (96.57%) | 191/204 (93.63%) |
| FastAPI | 4/12 (33.3%) | 8/12 (66.7%) | 493/516 (95.54%) | 500/516 (96.90%) |
| sqlite-utils | 8/12 (66.7%) | 8/12 (66.7%) | 702/720 (97.50%) | 714/720 (99.17%) |
| **Total** | **27/60 (45.0%)** | **39/60 (65.0%)** | **1,835/2,004 (91.57%)** | **1,877/2,004 (93.66%)** |

IPython has the highest exact success at 11/12. Its prompt names concrete host
lifecycle and channel-separation obligations. The one failed Luna trial scored
only 4/17, however, so aggregate IPython F2P is lower than the original despite
three additional exact passes. Correct obligations improved pass probability
but did not prevent one severe implementation failure.

IGEL is the strongest evidence that obligation *quality* matters. The v1 prompt
introduced an unsupported relocation model and produced 0/12 passes. After v2
removed that requirement and stated the repository's configured-path seam,
IGEL recovered to 7/12. Its failures remain bimodal: four trials scored 6/24
and one scored 23/24, so artifact-path realization is still brittle even under
the corrected obligation model.

FastAPI doubles exact success from 4/12 to 8/12 while missing only two of
37,608 P2P checks. This is the strongest repository-wide compatibility result.
Textual improves more modestly from 4/12 to 5/12; most remaining failures are
near-complete, but Luna still passes none of its four trials.

sqlite-utils exact success is unchanged at 8/12, although only six of 720
feature checks fail. Its remaining failures are narrow implementation or
integration errors rather than broad feature omissions.

### By model

| Model | Original exact | v2 exact | Original F2P | v2 F2P |
| --- | ---: | ---: | ---: | ---: |
| Luna | 5/20 (25.0%) | 11/20 (55.0%) | 592/668 (88.62%) | 618/668 (92.51%) |
| Terra | 5/20 (25.0%) | 11/20 (55.0%) | 595/668 (89.07%) | 611/668 (91.47%) |
| Sol | 17/20 (85.0%) | 17/20 (85.0%) | 648/668 (97.01%) | 648/668 (97.01%) |

The gain is entirely concentrated in Luna and Terra: both improve from 5/20 to
11/20. Sol is exactly unchanged at 17/20 and 648/668 F2P. This is consistent
with explicit obligations providing more new planning information to weaker
agents, while Sol often reconstructs the same model independently. The
interaction is striking but still comes from five selected tasks and should be
tested on a broader corpus.

## What the experiment supports

The combined evidence supports the following claims:

1. Missing behavioral obligations are a common root cause in these tasks.
2. More generic review activity is not sufficient; it can increase cost and
   test count without improving correctness.
3. Correct task-specific obligation information can materially improve exact
   completion, especially for Luna and Terra.
4. Incorrect obligation information can be actively harmful.
5. Even a correct obligation model leaves a meaningful implementation gap.

The strongest proposal claim is therefore:

> The valuable reviewer output is not a generic risk list or more source
> context. It is a compact, repository-grounded set of behavioral obligations
> whose important claims lack convincing evidence.

## What the experiment does not support

- It does not demonstrate benchmark-valid improvement; the v2 prompts contain
  oracle information.
- It does not show that current Guardian can autonomously infer the v2 prompts.
- It does not isolate prompting from additional test/probe suggestions, because
  the prompts include both obligations and discriminating experiments.
- Its nominal `p = 0.043` does not make it confirmatory because the prompt is
  oracle-informed, task selection is non-random, and the interim result was
  observed before completion.
- It does not establish generalization beyond five selected Python tasks.
- It does not show that all failures are obligation failures. The attribution
  study implicated implementation failure in 24/65 analyzable failures, and
  v2 still failed 21 trials.

## Implication for the Guardian prototype

The experiment argues for a lightweight solver–reviewer interaction rather
than a benchmark-specific task-description interface.

### Proposed interaction

1. The solver sends Guardian a custom message containing:
   - its current explanation of the requested change;
   - behavioral claims it believes the patch satisfies;
   - affected paths and modes it considered;
   - tests or runtime evidence supporting those claims;
   - remaining uncertainties.
2. Guardian re-grounds that explanation against the current repository.
3. Guardian converts important claims into falsifiable obligations.
4. Guardian searches for omitted producers, consumers, copies, modes,
   lifecycle phases, legacy paths, and preserved invariants.
5. Guardian returns a short message containing only:
   - the highest-impact missing or weakly supported obligations;
   - the contradictory or missing evidence;
   - the best discriminating investigation for each.
6. After the solver changes the repository, Guardian rebuilds its understanding
   rather than only checking whether old findings were resolved.

This preserves generic applicability. In real development there may be no
clean public task description; the solver's evolving explanation becomes one
input, while repository evidence remains authoritative.

### Target internal representation

The v2 prompts suggest a minimal obligation record:

- **condition/path**: when the obligation applies;
- **expected behavior/invariant**: what must happen;
- **applicability surface**: which modes, producers, consumers, and boundaries
  participate;
- **evidence**: implementation, callers, tests, runtime behavior, and
  documentation;
- **uncertainty**: what is still assumed;
- **discriminating probe**: the cheapest experiment that separates the current
  explanation from a plausible broken one.

This need not become a heavy rule engine. It can remain an LLM-maintained
understanding that is compactly serialized between review cycles.

## Recommended next experiment

Use four arms on the same task/model matrix:

1. original solo;
2. solo plus the universal generic review prompt;
3. solo plus the oracle-informed v2 prompt as a ceiling;
4. solo plus a Guardian-generated obligation message produced without access
   to held-out tests or the reference solution.

For a more credible result:

- retain at least four trials per task/model setting and pre-register the
  stopping rule;
- reuse identical task/model settings and, where possible, paired seeds;
- include additional Python and non-Python tasks;
- predefine handling for agent, launcher, and verifier timeouts;
- report exact success as primary, with F2P, P2P, token cost, latency, and
  authored-test quality as secondary outcomes;
- after grading, compare Guardian's generated obligations with the oracle
  obligation set using precision/recall and causal trial analysis;
- test whether the generated message changes the specific failure cluster, not
  merely the amount of review activity.

The key prototype target is not to match every oracle detail. It is to recover
enough high-impact obligations to close part of the gap between the original
45.0% baseline and the 65.0% oracle-informed ceiling.

## Reproducibility and artifacts

- Original solo matrix:
  `data/deepswe_outputs`
- Universal generic prompt report:
  `docs/guardian/experiments/solo_trial_analysis/universal_context_injection_report.md`
- Obligation attribution audit:
  `docs/guardian/experiments/obligation_model_attribution/report.md`
- v1 prompt investigation:
  `docs/guardian/experiments/local_specification_context_injection/prompt_investigation.md`
- v2 prompts:
  `docs/guardian/experiments/local_specification_context_injection/prompts_v2`
- v2 60-trial output (the directory retains its original interim name):
  `data/deepswe_outputs_context_injection/behavioral_obligations_python5_v2_2trials`
- Luna/sqlite timeout replacement:
  `data/deepswe_outputs_context_injection/behavioral_obligations_python5_v2_2trials_retry_luna_sqlite_job2`

One original v2 Luna/sqlite trial ended in `VerifierTimeoutError`. Its isolated
replacement passed 60/60 F2P and 1,038/1,038 P2P. The replacement is used in
the v2 totals, while the timeout artifact remains preserved.
