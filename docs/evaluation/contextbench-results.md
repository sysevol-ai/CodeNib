# ContextBench retrieval experiments

The October 16 independent validation **rejects the current online candidate**.
Function completion plus directed graph navigation does not improve Recall over
native source tools and reduces F1. All 32 selected tasks remain in the scores.
This policy will not advance to the official 500-task run or a product default.

Earlier development experiments found useful missing source by completing
functions after search. That offline gain has not transferred to the tested
online agent. These are retrieval experiments, not a leaderboard submission.

## Independent 32-task validation

The frozen selection contains four tasks per language and 22 repositories,
excluding all 105 earlier development IDs. It is independent by task, not by
repository, and is not a population-weighted estimate of the verified 500. Its
source mix is 23 Multi, five Poly, three Pro and one SWE-bench Verified task.

All three arms use actual Opus 5.5, SDK 0.2.165 / CLI 2.1.301, the same source
snapshots, 20-turn limit, 600-second timeout, $3/case and $20/arm SDK-meter guards.
The policy is frozen at `3f04993f`; runtime `4152c23d` fixes preparation-failure
accounting before any held-out inference without changing the model-facing policy.
Each arm has 31 completed and officially scored tasks, with one Git submodule
preparation failure retained as zero. No selected task is substituted or dropped.

| Cumulative observed trajectory, all 32 tasks | Recall | Precision | F1 | SDK dollar estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools | 69.64% | 29.01% | 29.01% | $6.740282 |
| Online function completion | 68.56% | 24.50% | 28.22% | $8.055923 |
| Function completion + targeted graph tool | 68.76% | 23.58% | 26.16% | $8.876974 |

Scores are macro means of per-task line metrics, including per-task F1 and the
failed preparation's zero. The prespecified primary contrast is the complete
graph-enabled route against native tools; it changes both completion and graph
navigation. Its Recall difference is **-0.88 percentage points**, paired 95%
bootstrap interval **[-5.73, +3.59]**. F1 changes **-2.84 points**, interval
**[-4.78, -1.02]**. The frozen gate requires a positive lower Recall interval and
non-decreasing F1, so it fails. Intervals use 10,000 paired task resamples with
seed `20261016`.

The exploratory ablations also fail to establish an improvement. Completion
alone changes Recall by -1.08 points, interval [-5.78, +2.79], and F1 by -0.79,
interval [-2.17, +0.69]. Adding graph navigation to completion changes Recall by
+0.19 points, interval [-1.41, +1.62], and F1 by -2.05, interval [-3.81, -0.48].
These secondary intervals have no multiple-comparison adjustment.

Eleven tasks have available graphs. The candidate calls the graph tool on all
31 prepared tasks, including all eleven with graphs: 35 calls yield source on
six tasks, with 14 returned source records. Four calls receive invalid-argument
errors; other responses include unavailable graphs and definitions that cannot
fit the response budget. All observations and failures remain in the result.

The only Verified task in the selection, `scikit-learn__scikit-learn-13142`, also
receives a matched downstream solver check. With one attempt per arm, native and
graph-enabled contexts each resolve **1/1 task**, passing two required regression
tests and 54 preservation tests. This establishes no comparative or population
Pass@1 gain. Both arms and graders use the same pinned image with the selected
clean source tree; a no-network derived image restores file modes changed in the
official parent. The reference-patch check passes separately and is excluded
from agent scores. Reference patches and test feedback never enter either solver.

The [per-task CSV](../assets/contextbench_independent_32.csv) and
[frozen protocol, intervals and solver receipts](../assets/contextbench_independent_summary.json)
retain the negative result. Return to the development cohort for diagnosis;
any revised candidate needs a new validation protocol and independent task IDs.
These 32 tasks must not become a tuning set or a source of best-of retries.

## Matched 24-task results

The sample contains three tasks per language, 24 repositories and eight languages,
selected deterministically from the earlier 100-task development cohort. Every
task remains in the denominator, and all results below have 24 valid official
evaluations. Recall, precision and F1 are macro means of per-task line scores.

| Route / final context | Recall | Precision | F1 |
| --- | ---: | ---: | ---: |
| Opus native agent, complete observed trajectory | 57.50% | 25.92% | 29.76% |
| Opus native agent, 16K delivery | 56.68% | 27.12% | 30.81% |
| Same observations + decision rerank, 16K | 56.76% | 27.09% | 30.81% |
| Same search + function completion, 16K | **64.97%** | 23.12% | **29.06%** |
| Opus native agent, 32K delivery | 57.26% | 26.06% | 29.94% |
| Same observations + decision rerank, 32K | 57.24% | 26.06% | 29.94% |
| Same search + function completion, 32K | **66.95%** | 21.09% | **27.42%** |
| Published JiuwenSwarm, same task IDs | 64.46% | 22.13% | 27.45% |

The public reference comes from the
[JiuwenSwarm submission](https://github.com/ContextBench/contextbench.github.io/tree/main/submissions/2026-09-12_openjiuwen-jiuwenswarm).
It includes a complete solving agent; our routes retrieve context only. The
leaderboard's full-population 75.3% Recall is a different denominator and must
not be directly compared with this 24-task estimate.

Function completion uses the existing language chunkers to read the entire
function or method touched by an observed source line, capped at 1500 lines per
callable. It does not complete whole classes/files or recursively expand from
new lines. The added source is part of a new serving stage; it is never credited
retroactively to the original Opus trajectory. This stage makes no model calls.

At 16K, completion improves Recall by 8.29 points over unchanged source delivery.
Its point estimates exceed the matched reference in Recall and F1, but the paired
95% Recall interval versus that reference is [-7.71, +7.61] points. At 32K the
Recall difference is +2.48 points, with interval [-5.75, +9.37]. Both intervals
cross zero, and this subset was used for development. Independent validation and
the official verified 500 have not been run for these policies.

## What the decision model contributed

The earlier 100-task, fixed-4K experiment is a different population:

| Opus grep route, 100 selected tasks | Recall | F1 |
| --- | ---: | ---: |
| Raw grep candidate order | 39.88% | 22.60% |
| Decision reranking | 45.70% | 27.84% |
| Graph expansion + decisions | 47.64% | 28.95% |

Two checkout failures remain zero in that denominator; graphs were available on
48 tasks. These results support reranking when a tight delivery budget forces
selection. They do not establish that decisions should control search actions or
stopping. In the later 24-task native agent, most observations already fit in the
larger context, leaving little benefit for another ordering pass.

Earlier chunk-based feedback loops also exposed the output bottleneck: using the
same candidates and scores, removing the ten-chunk cap and increasing delivery
from 4K to 32K raised Recall from 37.90% to 55.11%, while F1 fell from 30.04% to
19.72%. Increasing volume without better source selection is therefore not a
complete solution. No new decision-based product default is introduced.

## Cost and Claude subscription support

The completed 24-task native Opus run used 278 successful model calls and cost
**$23.7864** in recorded OpenRouter API charges, or $0.9911/task. Successful calls
were reused during recovery rather than billed twice. The extra decision-only
selection pass cost **$0.05941** in total; function completion cost no additional
API dollars. These exclude local compute and a downstream solver. This strong
search reference is not an ultra-low-cost serving result.

The source-checkout experiment also supports the official Claude Agent SDK with
an existing Claude Code subscription login. A Claude Max smoke completed with
actual **Opus 5.5**, eight turns and seven source-tool calls, using the same
confined `read`, `grep` and `glob` tools and the official scorer. It did not call
OpenRouter. SDK-reported dollar metering is kept separate from actual billing:
the smoke reported $0.116873 as an estimate; actual billed dollars are unknown.
Subscription limits still apply. See the
[official subscription guidance](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan).

The subsequent SDK batch was stopped at the user's request after three completed
tasks and one interrupted task; twenty were not run. It is not reported as a
24-task quality result. A new online comparison was started on October 16;
the historical results in this page do not describe that new policy.

The October 16 development and independent-validation program uses the existing
Claude Max route with **zero standalone API-key inference calls**. Independent
retrieval reports $23.673179 in SDK estimates, and its paired solver check reports
$0.828001. Known program metering totals **$41.623502**, excluding one aborted
development solver request with no usage returned. These are not account bills;
actual subscription billing and any account-level extra usage are unknown.
The authorized ceiling for new API spending remains **$100**. No automatic API
fallback is enabled, and the failed quality gate does not authorize a 500-task run.

## Online function-completion controls

The source-checkout runners now accept `--source-policy native|functions`.
`functions` adds the missing physical lines of a function or method touched by
a real `read` or content-mode `grep` result **before the next model turn**.
The added lines are visible to the agent and belong to that actual tool step.
This differs from the historical post-run serving refinement above.

Original observations stay first. Completion does not follow newly added lines
recursively or expand entire classes/files. An added callable must fit in full
within the additional-source budget (`--completion-tokens`, default 2,048) and
the native runner's 16,000-character response limit; skipped completion is
reported when space permits. Names-only results cannot trigger reads. These
policies remain experiments and do not change the product retrieval defaults.

The first fresh online comparison is complete on eight development tasks, one
per language selected by CSV order from the disclosed 24-task cohort. Both arms
use actual Opus 5.5 through SDK 0.2.165 / CLI 2.1.301, the same implementation and
source snapshots, a 20-turn SDK limit, 600-second timeout, $3/case and $15/arm
SDK-meter guards. All eight cases completed and scored in both arms.
The table uses task-level macro means, including per-task F1, as in the cited
JiuwenSwarm submission. The official evaluator's CLI summary also provides
pooled-count micro aggregates; those use a different weighting.

| Observed trajectory | Recall | Precision | F1 | SDK dollar estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools | 41.13% | 33.94% | 33.67% | $2.153231 |
| Online function completion | 41.67% | 29.33% | 31.91% | $2.406782 |

The paired 95% bootstrap interval for the Recall change is [-2.03, +3.36]
percentage points; for F1 it is [-4.94, +1.04]. Neither establishes an improvement.
The independent 32-task results above evaluate the frozen policy selected by
the later directed-graph development trial below.
These SDK estimates are not paid receipts, and this eight-task population is not
directly comparable with the historical 24-task table or the full leaderboard.

Use a fresh directory for each arm, with the same prepared tasks, model,
runtime, turn limit and cost settings. Existing clean task checkouts can be
reused during preparation with `--repos-root`; they are audited, never reset.
For the optional subscription SDK environment described below:

```bash
python scripts/benchmark_context_claude.py \
  --prepared results/contextbench-dev/prepared \
  --output results/contextbench-dev/sdk-native \
  --model claude-opus-5-5 --max-turns 20 --source-policy native --live
python scripts/benchmark_context_claude.py \
  --prepared results/contextbench-dev/prepared \
  --output results/contextbench-dev/sdk-functions \
  --model claude-opus-5-5 --max-turns 20 --source-policy functions --live
```

`--max-meter-usd` and `--case-meter-usd` bound SDK-reported metering, not measured
account billing. `--case-timeout` bounds each case. Unknown or invalid metering
stops later tasks; those tasks remain in the selected population. An in-flight
request can exceed an observed meter limit. Subscription quotas still apply.

Evaluate both directories using `benchmark_context_agent.py evaluate` and the
isolated official evaluator, then compare the cumulative observed trajectories:

```bash
python scripts/benchmark_context_agent.py compare \
  --baseline results/contextbench-dev/sdk-native \
  --candidate results/contextbench-dev/sdk-functions \
  --output results/contextbench-dev/sdk-comparison.json
```

Comparison rejects different harnesses, actual model identities, code snapshots,
task selections or budgets, and currently requires fresh runs rather than resumed
ones. It averages per-task Recall, precision and F1 over every selected task,
counts scoring failures as zero, preserves runtime failures and usage records,
and reports paired task-bootstrap intervals. It does not produce Pass@1 or turn
a development subset into a leaderboard result.

## Directed graph and downstream solver controls

The optional `graph_neighbors` tool follows one directed compiler-reference hop
from a source definition. Incoming references include tests; file hubs and
containment edges are excluded. It returns complete numbered definitions when
they fit the response budget, otherwise explicit metadata without source-read
credit. References are not a runtime call trace. Graphs are verified against a
frozen source manifest and graph-file hashes before use.
Inside a function or method, a selected line navigates from the enclosing callable
rather than a smaller local-symbol definition. The current shared prompt requests
at least one targeted reference hop before handoff when the tool is available;
actual tool adoption must still be checked in the recorded trajectory.

Prepare graphs locally, then run two fresh `functions` arms with the same
`--graphs` directory; add `--graph-navigation` only to the candidate. The paired
comparison permits that declared tool difference while retaining the other
runtime and budget checks. The eight-task development selection has five
available graphs; all eight tasks remain selected for the graph comparison.

The first optional-graph comparison completed all eight tasks in both arms:

| Observed trajectory | Recall | Precision | F1 | Graph calls | SDK dollar estimate |
| --- | ---: | ---: | ---: | ---: | ---: |
| Function completion control | 40.23% | 28.19% | 31.11% | unavailable | $2.682236 |
| Function completion + optional graph tool | 44.56% | 32.14% | 34.28% | 0 | $2.482844 |

The Recall difference is +4.34 percentage points, paired 95% interval
[-0.06, +11.88]; F1 is +3.17 points, interval [-0.81, +7.46]. The candidate
never called the graph tool, so these scores do not demonstrate a benefit from
graph-returned source. An explicit-call integration probe succeeds, but is excluded
from quality scores. A separate fresh paired trial requests a targeted hop:

| Observed trajectory, targeted-hop implementation | Recall | Precision | F1 | Graph calls | SDK dollar estimate |
| --- | ---: | ---: | ---: | ---: | ---: |
| Function completion control | 37.43% | 28.40% | 30.18% | unavailable | $2.497661 |
| Function completion + targeted graph tool | 41.29% | 32.16% | 32.85% | 8 | $2.702899 |

All eight tasks complete and score in both arms. The Recall difference is +3.86
points, paired 95% interval [-0.51, +11.82]; F1 is +2.67 points, interval
[-1.11, +6.84]. Three graph calls return source, one returns an oversized-definition
notice, one finds no definition at the selected line, and three report unavailable
graphs. All five available graphs are called. This passes the development
candidate-selection gate fixed before scoring: actual graph use, higher Recall
and non-decreasing F1. The intervals still do not establish a reliable gain.

The policy at `3f04993f` is frozen for independent 32-task validation with native,
functions and functions-plus-graph arms. Eleven cases have available graphs;
all three arms have completed and scored. The primary contrast is the complete
graph-enabled route against native source tools, with the other two contrasts as
explanatory ablations. The independent quality gate fails, as reported above.
The 32 tasks remain excluded from tuning. Neither graph development trial is
directly comparable with the earlier native/functions implementation freeze.
Preflight retains 31 ready snapshots and one failed Git submodule preparation.
Before held-out inference, `4152c23d` fixes failure accounting so that known
preparation failures remain zero without cancelling other selected tasks. The
model-facing policy is unchanged and all 32 tasks remain in every arm.

```bash
python scripts/contextbench_graph_navigation.py \
  --prepared results/contextbench-dev/prepared \
  --output results/contextbench-dev/graphs --timeout 120
```

`scripts/benchmark_context_solver.py` provides an initial downstream adapter for
**SWE-bench Verified only**. It runs the pinned upstream mini SWE-agent loop with
the already-observed 16K/32K context, an existing Claude subscription and an
already-local Docker image identified by digest. The image must have the same
clean source tree as the selected base commit. The solver container has no
network access or host credentials. SDK metering, failed attempts and missing
usage remain explicit.

The `grade` command requires an isolated `swebench==4.1.0` environment and the
corresponding native Verified dataset row in JSON format. It uses the native test
script, reference test patch and resolution grader on the exact solver image:

```bash
python scripts/benchmark_context_solver.py grade \
  --solver-run results/contextbench-dev/solver-case \
  --dataset results/contextbench-dev/native-verified-case.json \
  --output results/contextbench-dev/solver-case-grade
```

The first completed development attempt produced a patch that applied but failed
the required native tests: **0/1 resolved**. Its generation reported $0.555974 in
SDK metering; preliminary aborted attempts include missing usage, so this is not
a total paid-cost figure. This single attempt is not a comparative Pass@1 result.
Multi/Poly/Pro adapters and full-agent source accounting remain open. Arbitrary
shell reads from this solver are not added to the retrieval trajectory or claimed
as a complete leaderboard artifact.

A subsequent matched check feeds the targeted-hop trial's two 16K contexts into
fresh solver runs on that same Python development task. Both use the same pinned
image, implementation, actual model, 30-step limit and $3 SDK-meter guard. Both
patches apply, but each arm is **0/1 resolved**: the native test patch imports a
definition omitted by both generated production patches. The official reference
production patch passes in the same image and native harness; that grading-only
check is excluded from agent scores and makes no model calls. Paired solver SDK
estimates are $0.826470 for the control and $0.442726 for the graph candidate.
These repeated development attempts do not establish comparative Pass@1, and
reference patches or test feedback are never passed back to either solver.

## Evidence and reproduction

The [per-case CSV](../assets/contextbench_development_24.csv) and
[summary JSON](../assets/contextbench_development_summary.json) contain the exact
24-task metrics, costs, revisions and uncertainty estimates. They exclude source
text, prompts, provider identifiers and credentials. The raw local receipts remain
under ignored `verification/` directories; these summary exports are not a complete
official submission artifact bundle.

The October 16 online comparison has a separate
[eight-task CSV](../assets/contextbench_online_development_8.csv) and
[protocol, metering and paired-interval summary](../assets/contextbench_online_development_summary.json).
They retain the negative result and exclude prompts, source excerpts, account
identities and provider request identifiers.

The dataset revision is `c2855792b006af41c67202d33883fb9d46362853` and evaluator
revision is `1436c28a8eb95496da4ea69ad458b9f8a8eb7d61`. The recorded native and
refinement experiments preceded integration with current `main`; archived
implementation hashes are retained with their receipts. See
[ContextBench setup and scoring](contextbench.md) and
[dataset/evaluation ownership](architecture.md).

To rerun the same development IDs from a source checkout, first follow the
[installation steps](contextbench.md#installation), then export the pinned tasks
in the CSV's recorded order:

```bash
python - <<'PY'
import csv
import json
from pathlib import Path
from codenib.dataset.contextbench import load_contextbench_cases

with Path("docs/assets/contextbench_development_24.csv").open() as stream:
    ids = [row["instance_id"] for row in csv.DictReader(stream)]
cases = load_contextbench_cases(config="contextbench_verified", instance_ids=ids)
by_id = {case.instance_id: case for case in cases}
output = Path("results/contextbench-dev")
output.mkdir(parents=True, exist_ok=True)
(output / "selected.jsonl").write_text(
    "".join(json.dumps(by_id[key].to_record()) + "\n" for key in ids)
)
PY
python scripts/benchmark_context_agent.py prepare \
  --cases results/contextbench-dev/selected.jsonl \
  --output results/contextbench-dev/prepared
```

The following `run` explicitly enables new OpenRouter charges. Its cost guards
use observed spending; in-flight requests can exceed the thresholds. Function
completion and evaluation make no model calls:

```bash
python scripts/benchmark_context_agent.py run \
  --prepared results/contextbench-dev/prepared \
  --output results/contextbench-dev/native \
  --max-turns 20 --workers 4 --max-cost-usd 30 --case-cost-usd 3 --live
python scripts/benchmark_context_agent.py refine \
  --input results/contextbench-dev/native \
  --output results/contextbench-dev/functions --method functions
python scripts/benchmark_context_agent.py evaluate \
  --output results/contextbench-dev/functions \
  --upstream /tmp/ContextBench \
  --evaluator-python /tmp/contextbench-evaluator/bin/python
```

The SDK uses an optional `claude-agent-sdk==0.2.165` environment and an existing
logged-in Claude Code CLI; the smoke used CLI 2.1.296. Model calls require an
explicit `--live` flag. `benchmark_context_claude.py --help` describes the alternate
subscription runner; it uses a different agent loop and is not a transport-only
ablation. Reproduction runs use the current implementation and live model endpoint,
so they need not reproduce the historical scores exactly. The completed independent
32-task selection excludes all 105 earlier development IDs and rejects the tested
candidate. Future leaderboard claims require a new independently validated policy,
matched failure accounting and actual solver outcomes for Pass@1.
