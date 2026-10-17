# ContextBench retrieval experiments

The October 16 independent validation **rejects the earlier online candidate**.
Function completion plus directed graph navigation does not improve Recall over
native source tools and reduces F1. All 32 selected tasks remain in the scores.
This policy will not advance to the official 500-task run or a product default.
The revised first-source graph policy also fails its 24-task development gate:
Recall rises slightly, F1 falls, and the function-only ablation is interrupted.
The later eight-task relationship-check pilot completes all three arms and has
positive Recall/F1 point differences, but fails its two-language graph-source
gate. Its primary uncertainty intervals cross zero; it does not advance to 500.

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
A metadata-only inventory leaves 363 untouched tasks, with no C++ tasks remaining:
six of the release's ten C++ tasks are development data and four are in this
completed validation. Another independent selection requires an explicitly new
sampling protocol or new untouched tasks; the same four-per-language quota cannot
be repeated on this release. No replacement validation tasks have been selected.

## First-source graph policy on 24 development tasks

Runtime `63e035eb` freezes three fresh arms on all 24 disclosed development tasks,
three per language. It includes the eight earlier pilot tasks; these are not new
independent IDs. All arms use actual Opus 5.5, 20 turns, 600 seconds, $3/case and
$12/arm SDK-meter guards, with 2,048-token completion and graph-response budgets.
The candidate requires a graph hop after its first callable-bearing source reply
and prefers callable neighbors under the `unseen` policy.

| Cumulative trajectory, all 24 selected tasks | Recall | Precision | F1 | SDK dollar estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools, 24 completed | 53.35% | 31.62% | 31.18% | $5.493784 |
| Function completion + first-source graph, 24 completed | 54.02% | 28.61% | 30.15% | $6.375578 |
| Function completion, interrupted | 12.06% | 4.26% | 5.43% | $0.961724 known |

The complete primary pair changes Recall by **+0.66 points**, paired 95% interval
[-4.11, +5.59], and F1 by **-1.03 points**, interval [-3.71, +1.56]. The graph arm
makes 24 calls, covering all eleven available graphs. Six tasks receive 358
previously unseen physical source lines, including 139 annotated lines on one
Go and two Rust tasks. Subsequent assistant turns confirm delivery. This meets
the declared adoption and two-language novelty gates, but F1 decreases.
Physical overlap counts include blank/comment lines and are distinct from the
official line scores.

A post-hoc comparison with the complete native trajectories finds that 90 of
those 139 annotated graph lines also appear in the native control; the other 49
are confined to one Go task. Across all 24 tasks, the candidate adds 5,151
physical lines and omits 4,204 that the native control reads, including 472 added
and 464 omitted annotated lines. These are descriptive source-set differences,
not new official scores or causal graph effects. The interrupted function-only
arm cannot separate completion from graph navigation. Delivery-time novelty and
broader graph availability therefore remain insufficient evidence of a gain.

A further zero-model diagnostic attributes the existing replies before checking
annotations. Removing grep completion from the fixed trajectories would remove
635 annotated physical lines; disabling it is not supported by this evidence.
Completion also repeats 726 previously delivered lines. The optional
`--completion-source-policy unseen` omits verified prior source while retaining
the original read/grep reply and completing each selected callable across the
conversation. It does not use old observations to select additional callables.
The historical callable selection and budget reservations are retained before
removing duplicate lines; freed space cannot add a different function.

On all 24 fixed trajectories, the historical policy reproduces all 234 source
replies exactly. The new policy changes 20 replies, removes all 726 repeated
completion-line deliveries, and reduces reply characters from 1,008,050 to
971,508 (3.63%). Cumulative physical source is identical. An initial version
refilled the freed budget, adding 63 non-annotated lines and displacing 14 other
non-annotated lines in 16K delivery. That result is retained; the implementation
now preserves selection to isolate deduplication. These are offline diagnostics, not
new model results, token-cost measurements or a demonstrated quality gain.
Repacking every revised trajectory at 16K and 32K produces byte-identical
contexts, token counts and source predictions to the archived historical payloads.

Keeping the same 24 actual graph queries and source prefixes, the separately
prepared 19-graph composition returns 932 new physical lines versus 358 with
the original eleven graphs. After correcting annotation paths, novelty at delivery
rises 139 → 356, and annotated source absent from the complete native control
rises **49 → 194 lines**: 145 on C and 49 on Go. This supplies useful offline
mechanism evidence; fresh matched agent behavior is still required to establish
quality. Query timing and target selection remain open.
Both replays retain all tasks, reproduce their historical controls exactly and
verify frozen inputs and runtime hashes. Neither makes model calls or changes
the failed development/independent gates.

The function-only arm completes four tasks, then requests an unregistered graph
tool on a C task. The event normalizer aborts before the SDK's terminal usage;
the remaining 19 tasks are not run. Four completed traces and the partial failed
trace have valid official observations, with the 19 unstarted tasks retained as
zero. The interrupted arm's scores are failure accounting, not an estimate of
the completion policy or graph's marginal effect. Its missing usage remains
unknown. The predeclared three-arm development gate fails; neither retries nor
the later reliability smoke replace these records.

Fix `25e5e31e` preserves unavailable tool attempts and SDK error replies until
terminal usage, gives them no source credit, and includes graph instructions only
when that arm registers the tool. Extra advertised capabilities still fail the
tool-isolation check. A fresh eight-turn reliability check on the original C task
finishes with a terminal result and a $0.426619 SDK estimate; it has no quality
score and does not recover the historical missing usage.

An offline preparation diagnostic explains one missing graph. The first C++
development task, `fmtlib__fmt-2394`, has no compilation database under the
read-only preparation policy. With the same source and 300-second bound, enabling
project preparation in a private clone builds 8,247 nodes and 39,953 edges in
18.18 seconds; the control returns no graph in 4.16 seconds. The original source
remains clean. This availability check makes no model calls and does not alter
the frozen graph artifacts or scores above. Broader graph coverage and a revised
matched quality comparison remain open.

The existing [development summary](../assets/contextbench_online_development_summary.json)
retains all 72 task positions, intervals, failed gates, graph-source counts,
implementation hashes and the separate reliability check. The negative independent
32-task record remains sealed; the official 500 has not been launched.

## Query timing and anchor selection follow-up

A retrospective audit found that auxiliary overlap calculations compared raw
annotation paths with repository-relative source paths. Ten of the 24 tasks have
container-prefixed annotations. The official scorer already normalizes these
paths, so official scores and intervals are unchanged. Auxiliary counts in this
page and the development export now use the pinned official
`Gold.line_spans_init()` representation. The [correction record](../assets/contextbench_annotation_correction.json)
retains per-case amendments and original input hashes. Original frozen experiment
files remain intact. The evaluator bridge now exports canonical `gold_line_spans`
for future audits; regression checks cover relative, `/testbed/` and
`/workspace/<repo>/` annotations without exposing them to model inputs.

A zero-model probe retains all 24 development tasks and the 19 available composed
graphs. It chooses anchors from actual native observations before checking
annotations, queries both directions with the same 2,048-token bounds, and makes
38 queries per policy. Deferring the same first-read anchor until after the
complete native investigation leaves annotated source absent from native unchanged
at **80 physical lines** across Python and Go. Ranking observed callables by
symbol/file BM25 instead reduces that coverage to **50 lines**, all on one Go task.
This does not establish a benefit from delay or name matching. These are different
anchors and two directions per task, so the counts are not a matched comparison
with the earlier single-query 49-line result.

The next development control, `--graph-query-policy late_issue_reference_hop`,
asks the model to investigate first and then name a relationship question that
could change the fix or its validation. It selects an already observed callable
for that question and checks incoming or outgoing references before its final
handoff. The prompt permits at most two graph calls; this is an audited prompt
constraint, not an executor quota. It does not use the diagnostic BM25 ranking.
The historical `first_callable_source_reference_hop` remains the default. Both
runtimes record the policy; direct timing-policy comparisons require every other
graph setting, completion policy, tool, model and budget to match.

Runtime `5b91323c` freezes three fresh arms on the first task per language in the
existing 24-task development order. All eight tasks have source; seven have
authenticated composed graphs. They share Opus 5.5, 20 turns, 600 seconds,
2,048-token completion and graph budgets, conversation-aware completion, and
$2/case and $6/arm SDK-meter guards. All 24 task positions finish and score,
with terminal usage, the same actual model and no API-key route or fallback.

| Cumulative trajectory, all eight selected tasks | Recall | Precision | F1 | SDK dollar estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools | 39.85% | 34.14% | 32.85% | $2.038043 |
| Function completion | 40.40% | 28.87% | 31.38% | $2.433953 |
| Function completion + late graph check | 41.62% | 34.56% | 35.29% | $2.324711 |

The primary route changes Recall by **+1.77 points**, paired 95% interval
**[-4.36, +8.23]**, and F1 by **+2.45 points**, interval **[-1.06, +7.06]**.
Completion alone changes Recall/F1 by +0.55/-1.47 points. The exploratory graph
versus completion contrast changes Recall by +1.22 points, interval
[-5.49, +9.16], and F1 by +3.91 points, interval [+0.33, +8.92], without
multiple-comparison adjustment. These are reused development tasks, not an
independent validation or a leaderboard score.

Nine graph calls cover all seven available graphs. Every anchor was already
observed after at least two source replies; every result is followed by an
assistant response. No task exceeds the prompt's two-call limit. Only C and Rust
receive source: 200 and 20 new physical lines respectively. Canonical annotation
overlap is **200 lines on C, zero on Rust**; 197 of the C lines are absent from
the matched native trajectory. The two-language novelty gate therefore fails,
despite passing the point-score and execution gates. Python and Go have no unseen
neighbors; TypeScript and C++ return metadata for oversized source; Java has no
graph; both JavaScript probes have no definition at the requested line.

A zero-model fixed-trajectory check reproduces every original prediction, then
removes graph replies while keeping all later calls fixed. Macro Recall changes
41.62% → 39.80% and F1 35.29% → 34.00%. All Recall loss is on the C task. This
confirms direct source-score contribution, not what an agent would do without
graph feedback or a population improvement.
All runtime/input hashes match. The [24-row CSV](../assets/contextbench_late_graph_8.csv)
and [protocol, intervals, gate and source attribution](../assets/contextbench_late_graph_summary.json)
retain the complete result, the original raw-path audit's correction, and the
failed gate. The independent 32 remains sealed; the official 500 is not launched.

A subsequent zero-model diagnosis reproduces all nine original graph replies and
all three cached C++ development graphs before changing range selection. The
clangd graph materializer now chooses the smallest containing parsed range,
instead of the first overlapping chunk. This corrects 38 ranges across the three
graphs while preserving every compiler symbol, definition point and edge. At
`color.h:411`, navigation now selects the observed `ansi_color_escape` constructor
instead of an unrelated `rgb` constructor assigned a 520-line range. The corrected
query returns no neighbors; the other eight replies remain identical. No source
coverage or online score improvement is established. Existing frozen graphs and
scores remain unchanged; future graphs need regeneration to adopt the fix.

The oversized TypeScript neighbor has no unseen source within 16 lines of its
reference anchor, five lines within 32 and 37 within 64. The old C++ anchor has
none within 64 lines. These windows do not justify a new delivery policy. The
JavaScript file has 36 defined field nodes and 42 references owned by its file
node. Its compiler index lacks callable definitions for the assigned functions;
the chunker already supports their syntax. Adding inferred call edges would not
repair that compiler evidence gap. The linked summary includes this diagnostic's
hashes and aggregate evidence; it adds no model calls or API spend.

A compiler-only JavaScript follow-up separates that SCIP limitation from a
second problem in the existing LSP route. Identical syntax fixtures under three
project configurations reproduce missing assigned callable definitions in
`scip-typescript` 0.4.0. The TypeScript 5.9.3 language service can resolve the
NodeBB functions, but `typescript-language-server` 5.3.0 drops a function's name
span when it precedes the initializer, making reference lookup query the body
instead. The LSP client now recovers that span through the server's advertised
`navtree` command, requiring an exact name/body match and an unambiguous compiler
name span. Missing, stale or conflicting evidence retains the original symbol.
This serves existing LSP indexing and incremental updates; it neither changes
the active JavaScript SCIP route nor synthesizes relationships.

The complete existing LSP route processes all 764 original source files,
including tests, and 31,351 reference requests in 377.57 seconds with no request
errors or timeouts. A private clone uses a generated `allowJs` source list and
the observed TypeScript version is pinned; original source and runtime hashes
remain unchanged. The two targets have 26 and 18 compiler reference locations.
Replaying the original queries and message prefixes at the same 2,048-token
budget adds 132 unique physical lines and **zero annotated lines**, matching the
earlier partial probe. Even omitted neighbor ranges contain no unseen annotated
lines, so this result does not support increasing that delivery budget. The
summary retains the initial cleanup/PATH failures separately from the accepted
complete graph. The old-selection regression is reproduced and 206 related
tests pass. Useful relationship selection across languages and the active SCIP
definition gap remain open; this correction adds no model calls or API spend
and establishes no online quality gain.

A broader zero-model diagnosis reproduces all 72 frozen source predictions and
official line totals/intersections: both complete 24-task arms and all three
late eight-task arms. In the first-source candidate's 5,917 missed annotated
physical lines, 653 are in touched but incomplete callables, 3,476 elsewhere in
already observed files, and 1,457 in untouched files. The remaining 331 of 9,904
annotated lines have unavailable source locations: 303 refer to missing
canonical paths and 28 exceed the source reader's line count. Nine of the 24
tasks are affected. Eleven of the 28 out-of-range positions are just after a
file's final line. These pooled counts are not macro Recall averages.

For example, a Pylint annotation names `pylint/tests/...` while the source is
under `tests/...`; the pinned [official path normalizer](https://github.com/EuniAI/ContextBench/blob/1436c28a8eb95496da4ea69ad458b9f8a8eb7d61/contextbench/parsers/gold.py)
also removes leading dots from four Go configuration paths. The upstream main
revision remains the pinned revision as checked on October 17. The evaluator
now exports evaluation-only `gold_source_issues`, retaining every official path,
line and denominator. It does not remap plausible alternatives or grant them
read credit. Regression evaluation retains five gold lines and 40% Recall when
only two annotated lines exist and were read. The metadata matches the separate
24-task location audit; future runtime archives also include the evaluator bridge.

A predeclared follow-up spends only unused space in the same 2,048-token
completion budget on a whole remainder of an already touched short file. It
preserves every original reply, reproduces 310 source controls and all 32
original trajectory predictions, and adds no model calls. The unchanged official
evaluator scores the fixed-trajectory replay as follows:

| Fixed trajectory cohort | Recall, original → replay | F1, original → replay | Added physical / annotated lines |
| --- | ---: | ---: | ---: |
| First-source 24 | 54.02% → 56.08% | 30.15% → 29.36% | 1,918 / 153 |
| Late graph eight | 41.62% → 43.97% | 35.29% → 35.07% | 731 / 155 |

The eight tasks repeat from the 24; these are neither 32 independent tasks nor
new online outcomes. Source additions absent from matched native contain
annotations across Go, Java and Rust, but F1 falls in both cohorts. The declared
feasibility gate fails, so the file-remainder policy is not added to the agent
or advanced to model inference. The linked aggregate summary retains all rows,
protocol hashes, the initial diagnostic's missing-path failure and the amended
source classification. The next priority is useful relationship selection and
source attribution from actual solver/test feedback. All earlier failed gates,
the sealed independent 32 and the unlaunched official 500 remain unchanged.

## Historical post-run 24-task results

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
$0.828001. The later early-hop/unseen development trial adds $7.266208 for retrieval
and $1.367794 for its paired solver check. Two later one-task graph mechanism
versions add $0.327116. The first-source 24-task attempt adds $12.831086 in known
retrieval estimates and its separate receipt-fix smoke adds $0.426619.
The late relationship-check pilot adds **$6.796707** in SDK estimates.
Known program metering totals **$71.960832**, excluding one aborted development
solver attempt and one interrupted retrieval case with no terminal usage.
These are not account bills;
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
The opt-in `--completion-source-policy unseen` omits lines already verified in
the conversation; the default `definitions` retains the historical behavior.
Earlier source and the added missing lines together must complete the callable.
It retains the historical callable selection before omitting duplicates, so
unused budget does not expand a different function.
This option is recorded in run configurations and checked on resume and paired
comparison. Its offline diagnostic above does not establish an online gain.

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
rather than a smaller local-symbol definition. Within the bounded raw-edge scan,
function/method neighbors precede other defined symbols while preserving direction
and edge order within each group. The default development prompt requires a hop
after the first source reply showing a callable, including grep completion,
before further source tools. This guidance is included only for graph-enabled
arms. Actual use and new source must still be checked in the recorded trajectory.
The opt-in `--graph-query-policy late_issue_reference_hop` instead requires a
relationship check after initial investigation and before the final handoff, as
described in the timing follow-up above. Neither prompt is a product default or
evidence that an agent followed the requested timing.

The experimental `--graph-source-policy unseen` skips fully observed definitions
and returns only the missing lines of partly observed definitions when all of
those lines fit. It derives observations from actual, source-checked tool replies,
not requested ranges. Known neighbors do not consume the result limit; the raw
edge scan limit remains. The default `definitions` policy returns whole
definitions. Both options retain the original reference direction and budgets.
On an offline replay of the eight development calls, `unseen` removes all 220
repeated lines but adds no new source. This is a mechanism check, not evidence of
an online quality gain. The subsequent development comparison pairs fresh native,
function-completion and early-hop/unseen graph arms under runtime `768785a4`:

| Observed trajectory, eight development tasks | Recall | Precision | F1 | Graph calls | SDK dollar estimate |
| --- | ---: | ---: | ---: | ---: | ---: |
| Native source tools | 39.15% | 35.74% | 34.27% | unavailable | $2.010707 |
| Function completion | 43.97% | 31.57% | 34.02% | unavailable | $2.666574 |
| Function completion + early/unseen graph tool | 44.03% | 36.41% | 36.60% | 0 | $2.588926 |

All tasks finish and score, but the candidate makes **zero graph calls**. The
prespecified requirement for previously unseen annotated graph source therefore
fails. The primary whole-route Recall change is +4.88 points, paired 95% interval
[-0.11, +10.45]; F1 changes +2.33 points, interval [-6.44, +11.03]. These point
differences cannot be attributed to graph-returned source. The completed 32-task
negative result remains unchanged, and no new independent validation is started.
Future development must establish actual tool use and useful novel source before
another quality cohort. Detailed values and the failed gate are retained in the
[development summary](../assets/contextbench_online_development_summary.json).

An offline diagnostic then fixes the first callable-bearing read and its observed
source prefix in each of the same eight development traces. Before reordering,
the TypeScript and Go queries fill all five outgoing result slots with fields or
types. Prioritizing callable neighbors raises the Go query's new annotated source
from 1 to 16 physical lines; the other four available graphs add no annotated
lines at those anchors. Incoming output is unchanged. Across both directions,
the union of new physical source changes from 256 to 412 lines. These counts
include blank/comment lines and do not establish a Recall/F1 gain or predict the
agent's subsequent behavior. The old annotated Go line is displaced by different
source. All three unavailable graphs remain reported.

A first original-issue Go probe calls the graph after grep supplies a complete
function, before its first read. It delivers 132 previously unseen annotated
physical lines, but fails the literal predeclared after-first-read timing gate.
That failure and its $0.201015 SDK estimate remain recorded. A revised prompt
explicitly counts callable source from grep completion or read, and is frozen for
a second one-task mechanism version. Both versions have an eight-turn, $1 SDK
meter guard, receive no selected source anchor and stay outside quality scores.
The second version passes its declared source-observation timing and novelty
gate, delivers 132 new annotated physical lines to a subsequent assistant turn,
and reports $0.126101 in SDK metering. These are two versions on one development
task, not independent quality evidence. The later 24-task development controls
above establish broader tool use but fail the quality gate. Both mechanism
records accompany the public development summary; no new independent quality
cohort is started.

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

A subsequent development-only source audit finds that the three successful
graph responses contain 220 physical lines, all already observed by the agent.
Actual graph use therefore did not add new source in this trial. In the same
eight-task traces, grep-triggered completion contributes most added source but
also most newly observed annotated lines. These descriptive counts do not prove
that removing grep completion would improve an online agent. Future development
must measure useful new source and test selection policies before another
independent validation run; the completed 32 tasks remain excluded from tuning.

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

For a fresh graph build, `--allow-project-preparation` permits the existing
backend's dependency and build preparation inside its disposable clone. The
worker rejects changes to the selected commit or tracked source before publishing
a graph; generated build files are allowed. Timeout cleanup reaps the worker
before deleting that clone. The source checkout used by the agent stays clean.
The option defaults off, is recorded with the timeout in the graph manifest,
and cannot be combined with `--reuse-cohort`. Freeze these new artifacts before
any matched inference; they do not revise earlier results.

An offline 24-task preparation check obtains 14 graphs, compared with eleven
previously: eight become available and five are lost. Six Go/Java attempts lack
the managed indexers on their invocation PATH, two TypeScript builds time out,
and two builds are rejected after changing tracked source. Both the timeout and
preparation policy also differ, so this is not a policy-only comparison. All
source checkouts remain clean. On fixed native-trace prefixes, new physical
source rises from 687 to 890 lines, while newly covered annotated lines fall from
177 to 44 after canonical path normalization, largely because the Go graphs are
unavailable. This replay makes no
model calls and is not a Recall/F1 score or an autonomous agent result.

A separate prospective input combines fourteen fresh graphs with five
authenticated prior graphs for the identical source snapshots, leaving five
unavailable cases in the 24-task population. Artifact selection uses availability
alone. Its 19 available graphs have not undergone new online quality evaluation;
all prior build failures and negative quality results remain in the record.

`scripts/benchmark_context_solver.py` provides an initial downstream adapter for
**SWE-bench Verified and the Multi-SWE-bench fmt C++ pilot**. It runs the
pinned upstream mini SWE-agent loop with
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
Other Multi repositories, Poly/Pro adapters and full-agent source accounting
remain open. Arbitrary
shell reads from this solver are not added to the retrieval trajectory or claimed
as a complete leaderboard artifact.

The first Multi-SWE-bench native environment check is complete on the development
C++ task `fmtlib__fmt-2394`. The
[pinned native harness](https://github.com/multi-swe-bench/multi-swe-bench/tree/24f493f8a103e72312ded4f6b9c89f081d69cb09)
rejects a test-only
control and accepts the reference repair, passing all 18 required tests. Both
use the exact selected source tree and native patch hashes in the same official
image; both owned containers are removed. This check makes no model calls and
produces no CodeNib repair score. A separate solver image removes the reference
patches and scripts, retains only one source commit, and verifies an identical
clean tree. The fmt pilot now has a source-only solver route and a separate native
grader. The solver checks for retained reference files and history before any
model request. The grader authenticates native patches, replaces the production
reference with the exact solver patch, and then runs the original script. Other
Multi repositories still require native environment validation and adapters.
The public development summary retains image provenance, module hashes and
reports separately from agent quality results.

Run Multi grading from the repository root in an isolated environment containing
the pinned `multi-swe-bench==1.1.0` installation. The native grader entry point
does not import retrieval or model dependencies:

```bash
python -m scripts.contextbench_multiswe \
  --solver-run results/contextbench-dev/solver-fmt \
  --dataset results/contextbench-dev/native-fmt-case.jsonl \
  --native-image sha256:EXACT_LOCAL_NATIVE_IMAGE_ID \
  --output results/contextbench-dev/solver-fmt-grade
```

The native image must differ from the source-only solver image. Both must match
the selected source tree; their mutable tags are insufficient. Timeout, failed
patches, incomplete native reports and cleanup failures remain explicit. This
pilot supports `fmtlib/fmt`; it does not imply complete Multi benchmark coverage.

The new adapter rejects its unchanged-issue control. Two initial reference checks
fail at the 60-second container-start bound before tests begin; both containers
are removed. The bounded startup allowance is now 180 seconds, recorded
separately from test time. A fresh check at that bound rejects the unchanged-issue
control and accepts the reference repair with all 18 tests passing; both
containers are removed and the frozen runtime hashes match. The original two
startup failures remain recorded. These environment checks make no model calls
or agent scores.

A subsequent frozen C++ development pair at `fe4cbb72` reuses the first-source
trial's native and graph-configured 16K contexts for `fmtlib__fmt-2394`. Both
solvers submit a production-only patch and both resolve the task, **1/1 each**,
passing all 18 native tests. Each makes five model calls; all ten inputs have
confirmed receipts and terminal metering. SDK estimates are $0.734907 native and
$0.586895 candidate, $1.321802 combined. Both use Claude Max and the same source-only
image, model, 20-step limit and $2 SDK-meter guard; every solver and grader
container is removed. Runtime and retrieval-input hashes remain unchanged through
grading. This is one unique development task and does not establish a comparative
Pass@1 gain. Its graph was unavailable during the original retrieval, so this
result cannot establish a contribution from graph-returned source. The negative
24-task and independent 32-task retrieval gates remain unchanged.

New solver runs save each exact model request with a content hash and confirm
delivery only when the SDK returns an assistant message. The local
`model-observations.json` export therefore contains the shell output after the
native loop's formatting and truncation. Elided middle output and a final
submission diff that never reaches another model request are excluded. Missing
historical receipts and unconfirmed requests remain explicit; their billing may
still be unknown. This completes the raw observation boundary, while mapping
arbitrary shell output to source lines remains an open evaluation gate.

The next adapter adds a controlled source command to the same native loop:
`codenib-read PATH START END`, with 1-based inclusive lines, a 2 MiB text-file
limit and at most 2,000 requested lines. Both arms receive the same command and
instructions. Each read records the exact file version, pinned original blob,
rendered observation and its position in the conversation. Only complete lines
in a later confirmed model input receive coverage; native output truncation
still applies. Initial delivered context is also checked against its pinned
source before inference. Edits and tests continue through the existing shell.

`model-observations.json` labels this source attribution **partial**. Ordinary
shell output remains explicitly unattributed, and changed files retain their
current-version observations without guessed original line numbers. These
coverage records are not submitted to the official scorer as full trajectories.
The added instruction changes the solver protocol and therefore requires fresh
matched runs; earlier results remain frozen.

The [source-receipt verification](../assets/contextbench_solver_source_summary.json)
makes no model calls. An audit of the existing fmt pair preserves all ten actual
model-input receipts and verifies 411/375 initial source lines. Each arm still
has four delivered shell observations without execution receipts; none gains
retrospective source credit. A separate scripted loop in the pinned source-only
container delivers 36 complete lines from one read and 217 from a larger,
627-line file after native truncation. Four lines read after an edit receive no
original-source credit, and the final submission diff is excluded. Scripted
replies test the instrumentation only; they are not additional model runs or
repair outcomes. The container is removed and source/runtime hashes match.

An earlier development solver pair using the model-input recorder is **0/1 resolved in each
arm** on the same previously disclosed Python task. All seven native-context
inputs and eleven graph-enabled-context inputs have confirmed receipts. These
are repeated development attempts, not additional independent solver tasks.

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

The initial October 16 online comparison has a separate
[eight-task CSV](../assets/contextbench_online_development_8.csv) and
[protocol, metering and paired-interval summary](../assets/contextbench_online_development_summary.json).
They retain the negative result and exclude prompts, source excerpts, account
identities and provider request identifiers. The summary also contains the later
24-task first-source trial, including its interrupted function-only arm; the
eight-task CSV remains the original comparison.

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
