# Obligation-model attribution across 120 solo trials

## Verdict

Under the revised rubric, the dominant failure is a **wrong or incomplete
behavioral-obligation model**, not artifact construction after the relevant
obligations were already represented.

Of 120 original no-context-injection trials, 70 failed. Five of those submitted
empty patches and are operationally invalid. Among the 65 causally analyzable
failed trials:

- 41 were obligation-model-only;
- 15 were implementation-only;
- 9 contained both causes;
- 0 were uncertain.

Using causal incidence, which allows a mixed trial to count toward both causes:

- obligation-model failure was implicated in **50/65 (76.9%)**;
- implementation failure was implicated in **24/65 (36.9%)**;
- the two overlap in 9 trials.

These incidence values must not be added. The mutually exclusive breakdown is
41/15/9, plus five operationally invalid trials.

This result is substantially different from the earlier
normative-contract-versus-implementation analysis. The difference is expected:
the new definition treats an omitted producer, consumer, mode, lifecycle
phase, or preserved invariant as an **incomplete obligation model** whenever
stating that missing obligation would have changed the agent's plan, patch
scope, or tests.

## Scope and evidence

The audit covers every original solo trial currently in the workspace:

- 60 Python trials across five tasks;
- 60 language-stratified non-Python trials across five tasks;
- four trials for each task/model setting;
- `gpt-5.6-luna`, `gpt-5.6-terra`, and `gpt-5.6-sol`, all at medium reasoning;
- no context injection.

All 120 metadata/result records are present. There were 50 successful trials
and 70 failed trials. For every failed trial, the audit used:

1. the public task instruction;
2. the raw Codex trace and completion claims;
3. the submitted patch and any authored tests;
4. the held-out verifier failures;
5. the reference solution and test patch, post hoc.

The non-Python traces initially appeared absent from the result directories.
Their relative bind mounts had written them below each task's
`environment/data/...` directory. All 60 were recovered and used.

The complete evidence and counterfactual for each failed trial are recorded in
[trial_attributions.csv](trial_attributions.csv). Each row identifies the
agent-message evidence, artifact/verifier evidence, and why restating the
obligation would or would not add actionable information.

## Coding procedure

### Obligation-model failure

A trial was assigned obligation-model failure only when the trace, authored
tests, patch organization, or completion claim positively showed an incorrect
or incomplete behavioral model.

Examples include:

- an incorrect expected value, such as treating leading-zero numeric runs as
  unequal;
- a missing boundary, such as prediction inputs being allowed to omit targets;
- a missing execution path, such as `remove_empty_containers` in OXVG;
- a missing propagation surface, such as non-GET FastAPI helpers;
- an unrecognized invariant, such as allowing explicit artifact paths to
  retain ownership.

The counterfactual question was:

> Would stating the corrected or missing obligation add information that
> should change the plan, patch scope, or test scope?

### Implementation failure

Implementation failure required positive evidence that the exact relevant
condition or path had already been represented. Generic claims such as
"end-to-end support" or "all tests pass" were not sufficient.

Examples include:

- FastAPI Terra job 3 explicitly identified every HTTP-method decorator as a
  propagation surface, edited that surface, but left one helper wired
  incorrectly;
- sqlite Terra job 2 explicitly identified helper commits as destructive to
  active savepoints and attempted commit suppression, but the suppression was
  mechanically ineffective;
- IPython Terra job 3 explicitly distinguished stream output from expression
  results, but installed capture at the wrong lifecycle point;
- Textual Sol job 4 explicitly named key-code zero and tested it, but the
  matcher still rejected that concrete form.

The counterfactual question was:

> Would stating the correct obligation again add no meaningful information
> because the agent had already identified it specifically?

### Mixed, uncertain, and operational

A trial is mixed when separate observed defects satisfy each category. Mixed
does not mean ambiguous.

No trial was classified as uncertain. This was not a default: every non-empty
failed patch had a sufficiently detailed trace, artifact, and verifier result
to meet one or both positive-evidence thresholds.

Five trials were operationally invalid because their implementation remained
only in the container worktree and the submitted `model.patch` was empty:

- Obsidian Terra job 4;
- Helm Luna job 4;
- Helm Sol jobs 3 and 4;
- fd Terra job 3.

All five traces say implementation was complete but Git commit was blocked by
missing author/DCO identity. Empty patches were not used as evidence for either
behavioral category.

## Primary quantitative result

### Mutually exclusive trial categories

| Category | Failed trials | Share of all 70 | Share of 65 analyzable |
| --- | ---: | ---: | ---: |
| Obligation-model only | 41 | 58.6% | 63.1% |
| Implementation only | 15 | 21.4% | 23.1% |
| Mixed | 9 | 12.9% | 13.8% |
| Uncertain | 0 | 0.0% | 0.0% |
| Operationally invalid | 5 | 7.1% | excluded |
| **Total** | **70** | **100%** | **65** |

### Causal incidence

| Cause implicated | Trials | Share of 65 analyzable |
| --- | ---: | ---: |
| Wrong/incomplete obligation model | 50 | 76.9% |
| Implementation failure | 24 | 36.9% |
| Both | 9 | 13.8% |

The incidence table is the most faithful answer to "how many failures are due
to each cause." The first two rows overlap through the nine mixed trials.

## Attribution by task

| Task | Obligation only | Implementation only | Mixed | Uncertain | Operational |
| --- | ---: | ---: | ---: | ---: | ---: |
| IGEL feature schema | 8 | 1 | 0 | 0 | 0 |
| Textual kitty phases | 5 | 2 | 1 | 0 | 0 |
| IPython session bundle | 3 | 1 | 0 | 0 | 0 |
| FastAPI implicit methods | 2 | 6 | 0 | 0 | 0 |
| sqlite-utils checkpoints | 1 | 2 | 1 | 0 | 0 |
| Testem bail | 4 | 3 | 4 | 0 | 0 |
| OXVG structural selectors | 5 | 0 | 2 | 0 | 0 |
| Obsidian auto TOC | 10 | 0 | 1 | 0 | 1 |
| Helm manifest stream | 0 | 0 | 0 | 0 | 3 |
| fd sorting | 3 | 0 | 0 | 0 | 1 |
| **Total** | **41** | **15** | **9** | **0** | **5** |

The task variation is informative:

- FastAPI had the strongest implementation-only signal. Several agents
  explicitly enumerated repeated inclusion, OpenAPI, dependency dispatch, and
  every helper, then implemented those known obligations incorrectly.
- OXVG had the cleanest obligation-model signal. Every failed non-empty trial
  organized its plan around `collapse_groups`; all omitted
  `remove_empty_containers`. Two also misimplemented their known
  `collapse_groups` model and are mixed.
- Obsidian was overwhelmingly obligation-model failure. Ten patches conflated
  anchor normalization with visible TOC text, and several omitted marker or
  empty-region obligations.
- Testem contained both causes because the task exposed many exact,
  independently observable surfaces. Some agents omitted adapter/runner
  obligations; others represented formatter or lifecycle behavior and wired
  it incorrectly.

## Corpus comparison

| Corpus | Obligation only | Implementation only | Mixed | Operational |
| --- | ---: | ---: | ---: | ---: |
| Original Python 60 | 19 | 12 | 2 | 0 |
| Non-Python 60 | 22 | 3 | 7 | 5 |
| **Total failed** | **41** | **15** | **9** | **5** |

Among the 33 failed Python trials, obligation failure was implicated in 21
(63.6%) and implementation failure in 14 (42.4%).

Among the 32 causally analyzable non-Python failures, obligation failure was
implicated in 29 (90.6%) and implementation failure in 10 (31.2%).

The non-Python tasks contain especially visible repository-wide surface
omissions: all relevant optimizer passes, all browser adapters, all output
formats, and all marker/Markdown boundary cases.

## Representative discriminators

The following pairs show how the counterfactual was applied rather than simply
calling every missing edit an obligation failure.

### FastAPI

- **Obligation:** Terra job 1 never represented empty and relative callback
  paths. An explicit path-compatibility obligation would have changed its
  constructor assertion and compatibility tests.
- **Implementation:** Terra job 3 explicitly said every HTTP-method decorator
  needed propagation, edited them, and still left one helper wrong. Restating
  the obligation adds no information.

### sqlite-utils

- **Obligation:** Terra job 1 represented CLI and upsert separately but not the
  reopened persisted-safe-state plus CLI-upsert path. That composed obligation
  would add an end-to-end test.
- **Implementation:** Terra job 2 explicitly identified existing helper commits
  as the mechanism that destroys savepoints and attempted suppression. The
  guard was ineffective.

### OXVG

- **Obligation:** Five trials made `collapse_groups` pass while never
  recognizing `remove_empty_containers` as governed by the same invariant.
- **Mixed:** Luna job 1 and Terra job 3 also explicitly modeled selector
  anchors for `collapse_groups`, but their attempted tracking failed those
  known cases mechanically.

### Testem

- **Obligation:** Several trials spoke generally about "browser adapters" but
  omitted the distinct one-shot completion, deferred callback, and QUnit queue
  obligations.
- **Implementation:** Sol jobs 2–4 used the required `npmlog.warn` call and
  prefix on the exact invalid-config path; only the emitted warning text was
  constructed incorrectly.

## Sensitivity analysis

There are no uncertain trials, so assigning uncertain cases to either category
does not change any count.

Two additional worst-case views test whether coding choices could reverse the
conclusion.

### Reassign all mixed trials exclusively

| Assumption | Obligation trials | Implementation trials |
| --- | ---: | ---: |
| All mixed assigned to obligation | 50/65 (76.9%) | 15/65 (23.1%) |
| All mixed assigned to implementation | 41/65 (63.1%) | 24/65 (36.9%) |

Even the implementation-maximizing assignment leaves obligation-model failure
ahead by 17 trials.

### Improperly force operational failures into a causal category

This is not the preferred analysis, but it provides another bound:

| Assumption | Obligation incidence | Implementation incidence |
| --- | ---: | ---: |
| All five operational failures assigned to obligation | 55/70 (78.6%) | 24/70 (34.3%) |
| All five operational failures assigned to implementation | 50/70 (71.4%) | 29/70 (41.4%) |

The qualitative conclusion is unchanged under either extreme.

## Metric and verifier caveats

The primary unit is trial incidence, not failed test count. Cascading failures
are not independent reasoning failures.

Recorded aggregate metrics remain:

- reward: 50/120;
- F2P: 3,359/4,224;
- P2P: 69,208/72,636.

Ten Obsidian patches renamed the Jest suite from `Auto Table of Contents` to
`Auto TOC`. The feature suite actually executed, but exact-name matching marked
351 visibly passing checks as missing. Correcting that measurement artifact
changes F2P to 3,710/4,224. It does not change any trial category because every
affected trial still had at least one real failure.

The very large P2P deficits are also not counted as thousands of root causes.
For example, one FastAPI path assertion caused 3,134 regression checks not to
complete.

## Interpretation

The revised hypothesis is supported by this sample:

> Most failed agents lacked at least one actionable behavioral obligation
> needed to plan the correct patch and tests.

This is broader and more useful than saying agents usually misunderstand the
top-level task semantics. The missing obligation was often repository-wide:
another producer, helper family, optimizer pass, lifecycle composition, public
boundary, or invariant.

The result does not show that artifact construction is unimportant. It was
implicated in 24/65 analyzable failures and dominated FastAPI. It shows that
simply restating the top-level purpose is unlikely to be sufficient. A useful
Guardian intervention should help construct and challenge an obligation model:

1. identify conditions and expected behavior;
2. enumerate producers, consumers, copies, modes, lifecycle phases, and public
   paths governed by each behavior;
3. identify existing invariants that must survive;
4. require direct evidence for each high-impact obligation;
5. distinguish a missing obligation from a known obligation whose attempted
   implementation is broken.

## Validity limits

- This is a single-coder, post-hoc attribution. The evidence ledger makes each
  decision auditable, but no inter-rater agreement has been measured.
- Oracle tests and reference solutions were used diagnostically and must not
  be injected into benchmark agents.
- The sample contains ten selected tasks, not a random sample of software
  development.
- Completion claims can be inaccurate. They were treated as evidence of what
  the agent represented only when paired with a concrete condition/path, not
  as proof that behavior worked.
- The zero uncertain count reflects unusually rich traces and artifacts, not a
  policy of forcing ambiguity into a causal category.

Run [validate.py](validate.py) to reconcile the ledger with all 120 metadata
files, confirm the five empty-patch exclusions, check trace references, and
reproduce the aggregate counts and Obsidian measurement correction.
