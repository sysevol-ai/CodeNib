# Universal context injection experiment

## Executive conclusion

The universal generic review prompt did **not** improve solo-agent performance
across this five-task sample.

Exact task success fell from **27/60 (45.0%)** in the uninjected baseline to
**25/60 (41.7%)** with injection. Feature-test coverage also fell slightly,
from **1,835/2,004 (91.6%)** to **1,820/2,004 (90.8%)**. The prompt helped
IGEL and, to a lesser extent, FastAPI, but those gains were outweighed by
regressions on Textual, IPython, and sqlite-utils.

The agents did respond behaviorally to the prompt: they authored an estimated
401 test functions, versus 234 in the baseline, and used 32% more input tokens.
More review activity therefore did not reliably produce better task
understanding or implementation.

## Experiment

- Tasks: 5
- Models: `gpt-5.6-luna`, `gpt-5.6-terra`, and `gpt-5.6-sol`
- Trials per task/model setting: 4
- Reasoning effort: medium
- Total trials: 60
- Concurrency: 3
- Prompt: [universal_generic_prompt.md](universal_generic_prompt.md)
- Run state:
  `data/deepswe_outputs_context_injection/universal_generic_review/_matrix_runs/universal_generic_5x3x4_medium.json`
- Started: 2026-07-29 07:59:43
- Finished: 2026-07-29 11:46:05
- Wall time: 3:46:22
- Summed agent time: approximately 10:50:09

The launcher state says `recorded=59` and `launcher_error=1`. This is a
post-processing defect, not a failed agent trial. Luna/IGEL job 4 completed,
passed the verifier at 24/24 F2P and 2/2 P2P, and has a valid
`pier_result.json`. Its `codex.txt` contains a standalone JSON string line;
the token parser assumed every JSON line was an object and raised
`AttributeError: 'str' object has no attribute 'get'`. This report recovers
that trial from its raw artifacts and analyzes all 60 outcomes.

## Evidence and contamination boundary

Outcome and cost totals come from the saved metadata, `pier_result.json`, and
the recovered Luna/IGEL transcript. Trial diagnoses compare the agent
transcript, submitted patch, authored tests, verifier report, task tests, and
reference solution after execution. That oracle evidence is used only for this
post-hoc analysis. The injected prompt itself was identical across all five
tasks and contained no task names, reference solution, or held-out cases.

## Aggregate results

| Task | Baseline passes | Injected passes | Delta | Baseline F2P | Injected F2P | F2P delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| IGEL | 3/12 | 7/12 | +4 | 196/288 | 198/288 | +2 |
| Textual | 4/12 | 3/12 | -1 | 247/276 | 241/276 | -6 |
| IPython | 8/12 | 5/12 | -3 | 197/204 | 189/204 | -8 |
| FastAPI | 4/12 | 6/12 | +2 | 493/516 | 492/516 | -1 |
| sqlite-utils | 8/12 | 4/12 | -4 | 702/720 | 700/720 | -2 |
| **Total** | **27/60** | **25/60** | **-2** | **1,835/2,004** | **1,820/2,004** | **-15** |

P2P moved from 47,699/51,120 to 47,984/51,120, but the entire net increase
comes from FastAPI's very large 3,134-test regression suite. This should not be
read as broad compatibility improvement: four tasks had identical, perfect
P2P totals in both settings, while individual FastAPI arms still caused severe
regressions.

## Results by model

| Model | Baseline passes | Injected passes | Delta |
| --- | ---: | ---: | ---: |
| Luna | 5/20 | 3/20 | -2 |
| Terra | 5/20 | 6/20 | +1 |
| Sol | 17/20 | 16/20 | -1 |
| **Total** | **27/60** | **25/60** | **-2** |

The effect is not a simple capacity scaling story. Sol remained strongest, but
lost one exact success. Terra gained one, largely through two FastAPI successes,
while becoming much less reliable on IGEL and sqlite-utils. Luna's three
injected successes all came from IGEL.

## Cost and review behavior

| Measure | Baseline | Injected | Change |
| --- | ---: | ---: | ---: |
| Input tokens | 170,941,595 | 225,618,931 | +32.0% |
| Cached input tokens | 164,975,360 | 219,132,416 | +32.8% |
| Uncached input tokens | 5,966,235 | 6,486,515 | +8.7% |
| Output tokens | 1,028,043 | 1,171,733 | +14.0% |
| Reasoning output tokens | 375,459 | 410,409 | +9.3% |
| Authored test functions | 234 | 401 | +71.4% |

The authored-test count is a mechanical count of test functions added by model
patches. It measures agent behavior, not test quality.

The extra work was frequently misdirected:

- Textual agents wrote more tests but continued to miss parts of the key-event
  grammar, especially colon, shifted aliases, functional phases, and legacy
  alternate-key behavior.
- IPython failures still concentrated in replay stopping and magic-command
  parsing.
- Luna and Terra sqlite-utils patches repeatedly missed CLI upsert or
  operation-specific checkpoint integration.
- FastAPI remained vulnerable to repository-wide routing and inheritance
  regressions even when local feature tests looked convincing.

## Interpretation

The prompt's useful ideas—review boundaries, test negative paths, inspect
callers, and re-check the full change—are too abstract to reliably change the
agent's semantic model of a task. It encourages *more review*, but does not tell
the agent which belief about the feature is unsupported.

IGEL is the positive exception. That task has a compact, inspectable lifecycle
and the prompt often pushed agents to connect fit, persistence, prediction, and
HTTP use. Even there, the distribution was bimodal: seven complete solutions
and five 6/24 implementations, so the prompt improved pass probability without
eliminating the dominant failure mode.

The experiment supports a narrower prototype direction:

1. Do not inject a longer universal checklist by default.
2. Let the solver send Guardian its current explanation of the objective and
   implementation.
3. Have Guardian challenge concrete claims in that explanation and return the
   highest-value missing evidence.
4. Inject that task-grounded challenge, not benchmark text or a static taxonomy,
   into the next solver cycle.
5. Evaluate whether the challenge changes the known failure cluster, not merely
   token use or number of authored tests.

The task reports contain the per-trial evidence:

- [IGEL](igel-persist-feature-schema/universal_context_injection_report.md)
- [Textual](textual-kitty-key-phases/universal_context_injection_report.md)
- [IPython](ipython-session-bundle-replay/universal_context_injection_report.md)
- [FastAPI](fastapi-implicit-head-options/universal_context_injection_report.md)
- [sqlite-utils](sqlite-utils-safe-import-checkpoints/universal_context_injection_report.md)
