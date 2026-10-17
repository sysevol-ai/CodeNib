# ContextBench context-serving experiments

Owner: retrieval/evaluation maintainers. Lifecycle: **labs**, source-checkout
benchmark only. No product route, storage boundary, graph schema, or default
retriever changes. The graph pilot is
[`scripts/benchmark_graph_decisions.py`](../../scripts/benchmark_graph_decisions.py);
the graph-free controls are
[`scripts/benchmark_decision_loop.py`](../../scripts/benchmark_decision_loop.py)
and [`scripts/benchmark_context_agent.py`](../../scripts/benchmark_context_agent.py).
[`scripts/benchmark_context_claude.py`](../../scripts/benchmark_context_claude.py)
provides the subscription-backed SDK experiment using the same source tools.

The current quality gate separates **cumulative source observed by an agent**
from **final context delivered to a downstream model**. The old top-10/4096-token
payload remains a historical fixed-budget control, not the quality target.
The native search/read control retains the full tool trajectory and separately
packs 16K/32K delivery from actually observed source. None of these retrieval-only
experiments supplies patches, test outcomes or Pass@1.

## Active follow-up and quality gates

The October 11 development iteration is complete and retained through merged
[#827](https://github.com/sysevol-ai/CodeNib/pull/827). On October 16 the user
resumed work on online context completion, matched controls and a complete
issue-resolution evaluation in draft
[#834](https://github.com/sysevol-ai/CodeNib/pull/834). The earlier objective
of **ContextBench retrieval SOTA** was not achieved; the completed development
experiments and their limitations are retained here and in the public
[results report](../evaluation/contextbench-results.md).
As checked on 2026-10-11, the [official leaderboard](https://contextbench.github.io/)
lists JiuwenSwarm / DeepSeek V4 Pro at 75.3% line Recall and 25.7% line F1 on the
verified population. Recall is the primary target, with F1 at least as high as
that same reference under a matched protocol; reading unrelated source alone
cannot establish a useful quality improvement.

The current milestones and remaining gates are:

| Milestone | Current outcome | Open gate |
| --- | --- | --- |
| Online source completion | Independent 32-task comparison: Recall 69.64% → 68.56%, F1 29.01% → 28.22%. Conversation-aware completion removes 726 repeated deliveries with identical fixed-trajectory source. A further bounded file-remainder replay preserves original source and raises Recall on the 24/eight-task cohorts, but F1 falls 30.15% → 29.36% and 35.29% → 35.07%; its predeclared feasibility gate fails. | Retain useful grep completion. Do not ship the rejected file-remainder policy or advance it to model calls. Prioritize relevant source selection and fresh matched validation over more automatic source volume. |
| Evaluation source diagnostics | All 72 development predictions and official line totals/intersections reproduce. The 24-task source audit finds 303 missing-path and 28 out-of-range annotated lines across nine tasks. Evaluation-only `gold_source_issues` reports these without remapping paths or changing scores; future archives include the evaluator bridge. | Preserve official paths and denominators. Do not treat nonexistent annotation locations as recoverable retrieval omissions or use diagnostic alternatives for source credit. |
| Matched experiment | The independent 32-task validation remains complete and negative. The revised 24-task development primary pair completes; its function-only ablation stops after four complete cases and one partial error, retaining 19 not-run cases. Receipt handling is fixed and a separate one-task reliability check passes. | Preserve the interrupted arm; its ablations cannot establish policy effects. Keep all 32 validation tasks sealed and require fresh matched controls for any revised policy. |
| Directed graph navigation | The 24-task first-source policy fails its quality gate. Canonical-path correction shows composed-graph replay adds 194 annotated lines absent from native across C/Go, versus 49 before. The late-check eight-task pilot changes Recall 39.85% → 41.62%, F1 32.85% → 35.29%, both primary intervals crossing zero; its two-language novelty gate fails. Zero-model fixes correct 38 overlapping C++ ranges and recover compiler-backed JavaScript LSP name selections. The complete 764-file NodeBB graph makes 31,351 reference queries without errors; its two fixed queries add 132 physical lines and zero annotated lines. | Establish useful relationship selection and graph delivery across languages before another matched candidate. Nearby anchor windows and the NodeBB omitted-neighbor audit do not justify more source budget. Active SCIP assigned-function definitions remain incomplete. Preserve canonical paths, failed gates and the sealed independent 32. No promotion or full-500 launch. |
| Complete solver | The independent Verified task resolves in both arms, 1/1 each; the repeated development pair remains unresolved, 0/1 each. The Multi fmt pair resolves in both arms with 18 passing tests. A separately frozen corrected-reader check obtains two confirmed reads and another resolved fmt repair, but adds zero source lines beyond the initial context. The earlier reader-adoption failure is retained. Gson passes native readiness checks; its fresh real pair resolves 1/1 in both arms with 96 required classes passing and eleven confirmed inputs. The C ponyc pilot passes native/source gates, but its fresh native/functions repairs fail (12/11 tests); its graph arm ends in a retained budget error with no prediction. Terminal usage is recovered offline and the accounting bug is fixed. | Freeze fresh matched solver runs with the common reader and use actual repair/test feedback to guide retrieval. Source coverage remains partial: arbitrary shell output and modified-file base coordinates are unattributed. Use the C preservation failures and duplicated function/graph source to improve relevant selection before more paid comparisons; establish broader matched outcomes and extend other Multi repositories and Poly/Pro coverage. Jackson's preparation changes tracked build configuration and needs an explicit source-overlay treatment. Environment readiness and repeated one-task repairs establish no comparative Pass@1 gain. |
| Leaderboard submission | Official artifact and population requirements are documented. The current candidate is rejected before full-scale execution. | A new independently validated policy, verified 500, measured resolution outcomes, complete artifacts and a separate measured budget decision. |

The evaluation sequence remains:

1. Diagnose and iterate on the disclosed 100 development IDs plus the earlier
   five pilot IDs. The 24-task multilingual subset remains development data.
2. Freeze a policy, then compare on independent task IDs with all selected tasks
   retained in the denominator. `verification/context-agent-holdout-32/` freezes
   four tasks per language, 32 tasks / 22 repositories, excluding all 105 earlier
   development IDs. Its SHA-256 is
   `49e0370ff0f384aed1691cb8dd3f071cf900c9020fadd3608d3f1955912be64a`.
   Selection uses only metadata and a fixed seed; no reference scores or labels
   were inspected for selection. Its frozen three-arm validation is complete
   and fails the independent quality gate. It is
   task-held-out rather than repository-held-out, and not a population-weighted
   500-task estimate.
   Source preparation has 31 ready cases and one unconfigured Git submodule
   failure. Before held-out model calls or scoring, `4152c23d` fixes batch
   accounting so that this known preparation failure remains zero without
   cancelling unrelated cases. The retrieval prompt and graph policy remain
   byte-identical to `3f04993f`; snapshot checks are not weakened. All 32 tasks
   remain in every arm. Eleven cases have an available compiler graph.
   The 32 tasks now remain a completed validation record. Development diagnosis
   returns to the disclosed 105 IDs; a revised candidate requires new independent
   task IDs and a new protocol before inference.
   A metadata-only inventory leaves 363 untouched tasks, including zero C++ and
   four Java tasks. All ten C++ tasks in this pinned 500-task release are already
   in development (six) or the completed validation (four). A second independent
   four-per-language selection is therefore impossible on this release. Define
   a new sampling protocol or obtain new untouched tasks before another quality
   validation; do not reuse the completed 32 to preserve the old quota.
3. For a SOTA claim, freeze implementation, model and evaluator versions and run
   the official verified 500. Recheck the then-current leaderboard and denominator
   conventions (the current leader has 499 valid retrieval records). Report
   failures, paired uncertainty, both Recall and F1, complete API cost and actual
   delivery budgets. Beating a matched 24-task subset is not this gate.
4. Keep retrieval SOTA separate from task-solving SOTA. Pass@1 and promotion to
   the product context route require actual downstream solver evaluation.

The paired offline decision-selection and touched-function controls below are complete.
Their development evidence prioritizes missing source over another final rerank.
The requested Claude subscription alternative is implemented through the official
Agent SDK and passes an Opus 5.5 smoke with the logged-in Max account. Its follow-up
batch was stopped at the user's request. There is no automatic paid API fallback.
The SDK's metered dollar estimate is not a paid receipt, and subscription limits
still apply. The current online controls start fresh SDK sessions in both arms;
they do not compare the SDK's quality directly with the earlier native runner.

### Current execution and cost envelope

Prefer the existing Claude Max login. The user authorized at most **$100 in new
API spending** for development and small-sample validation; a verified-500 run
is a later budget decision. The SDK runner has no automatic API fallback.
SDK dollar metering and account billing remain separate. Meter guards and a
per-case timeout stop new work when a run reaches its limit; an in-flight call
can exceed an observed guard. Unknown or invalid metering stops later cases.

The completed independent retrieval arms report **$23.673179** in SDK estimates;
their one-task paired solver check adds **$0.828001**. Across this October 16
program, known metering totals **$71.960832**, plus one aborted development solver
attempt and one interrupted retrieval case with unknown metering. Two separately
frozen October 17 reader-adoption checks add $0.695992 and $0.932850, bringing
the subtotal to $73.589674. The fresh paired Gson solver check adds $1.088503,
bringing the subtotal to $74.678177. The C three-arm comparison adds $8.115611,
including $0.321885 recovered from its preserved terminal event, bringing known
cumulative metering to **$82.793787** with the same two historical
unknowns.
Standalone API-key inference calls and their
charges remain zero. Actual subscription billing is unknown. The independent
quality gate fails, so this policy does not advance to the full-500 run.
The later early-hop/unseen development trial contributes $7.266208 in retrieval
estimates and $1.367794 for its new paired solver attempts to that program total.
The two later one-task graph mechanism versions add $0.327116, with no standalone
API-key inference or quality score.
The C++ range and JavaScript compiler/LSP diagnoses add no model calls or API
spend. The latter's accepted complete graph takes 377.57 seconds locally; its
initial cleanup and executable-resolution failures remain separate records.
The first-source 24-task development attempt adds $12.831086 in known retrieval
estimates; its separate receipt-fix reliability smoke adds $0.426619. The failed
function-only arm retains its unknown usage rather than substituting the smoke.

The historical native search average is $0.9911/task (24 cases; median $0.690554,
maximum $4.07134). At that unchanged mean, 32 tasks in two arms cost **$63.43**
for search alone; 500 tasks cost **$495.55 per arm**, or **$991.10 for two arms**.
These are arithmetic projections, not current quotes or solver measurements.
The provisional complete-agent planning range is **$1,500–3,000 for two 500-task
arms**, assuming $1.50–3.00 per task including search and solving. Recalibrate
that assumption with measured solver cases before authorizing the large run.
These figures exclude extra cloud compute and any separate subscription charge.

Development starts with the first CSV-ordered task per language from the
disclosed 24-task cohort: eight tasks, selected without consulting their scores.
A one-task smoke uses the first of those tasks. Artifacts are retained under
`verification/contextbench-online-20261016/`. The independent 32-task selection
is not used to tune the policy. The first native/functions comparison does not
pass the development selection gate. The subsequent targeted-hop trial passes
that gate and advances unchanged to independent validation. The 32-task protocol
retains native, functions and functions-plus-graph arms, with $20/arm SDK-meter
guards; all use the same $3/case guard and 600-second timeout. The primary contrast
is the complete graph-enabled route against native source tools; the other two
contrasts are explanatory ablations. A positive lower paired Recall interval and
non-decreasing F1 point estimate are required on that primary contrast before
further quality claims. Secondary intervals are exploratory, with no adjusted
multiple-comparison significance claim.

### Independent 32-task outcome

All three arms finish with 31 `ok` cases and one `preparation_failed` case, with
31 valid official scores and the failed preparation retained as zero. There are
no SDK batch errors or missing usage records. Runtime `4152c23d` preserves the
selected `3f04993f` policy; all 38 archived implementation hashes match in every
arm. The selected population contains 23 Multi, five Poly, three Pro and one
Verified task, so its native solver coverage is limited to one task.

| Cumulative observed trajectory | Recall | Precision | F1 | SDK dollar estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools | 69.64% | 29.01% | 29.01% | $6.740282 |
| Online function completion | 68.56% | 24.50% | 28.22% | $8.055923 |
| Function completion + targeted graph tool | 68.76% | 23.58% | 26.16% | $8.876974 |

The primary whole-route contrast changes Recall by -0.88 points, paired 95%
interval [-5.73, +3.59], and F1 by -2.84, interval [-4.78, -1.02]. The declared
gate fails. The secondary graph-over-functions contrast changes Recall by only
+0.19 points and F1 by -2.05; its exploratory intervals are [-1.41, +1.62] and
[-3.81, -0.48]. Do not infer a quality improvement from the earlier development
gate or compare these retrieval-only scores directly with the full leaderboard.

The candidate makes 35 graph calls across all 31 prepared tasks, including all
eleven graph-available tasks. Six tasks receive source, totaling 14 source
records. Four model calls to the tool have invalid arguments and return errors;
these remain in the trajectories. Other explicit outcomes include unavailable
graphs, missing definitions and source that does not fit the response budget.

The one independent Verified case, `scikit-learn__scikit-learn-13142`, resolves
in each of the two solver arms: **1/1 each**, with one attempt, five model calls,
16K context limit, 30-step limit and $3 SDK-meter guard per arm. Native grading
passes two required regression tests and 54 preservation tests. Both solvers
and graders use the exact same clean source tree and pinned derived image. Its
official parent had identical tracked content but changed file modes; a local
no-network image layer resets only to the selected source commit. This keeps the
strict tree check intact. The separate reference-patch check passes without
model calls and is excluded from agent scores. Solver SDK estimates are
$0.359556 for native and $0.468446 for the candidate. One successful independent
task does not establish comparative Pass@1 or complete the full-agent boundary.

The sanitized [32-task CSV](../assets/contextbench_independent_32.csv) and
[protocol and summary](../assets/contextbench_independent_summary.json) preserve
the failure, uncertainty, implementation hashes and image provenance. Local
receipts remain in `verification/context-agent-holdout-32/`. The next development
diagnosis uses only development IDs. No new model batch or policy is selected
from the held-out results.

### Development source-attribution diagnosis

An offline audit of the same eight development tasks separates original tool
output from automatic completion and counts each physical source line at its
first observation. It makes no model calls and does not inspect held-out labels.
The targeted-hop candidate's three source-returning graph calls contain 220
physical lines, **all already observed**. Successful calls alone did not establish
additional coverage. The next development gate must measure previously unobserved,
relevant source before spending another independent validation selection.
The annotation counts below include the retrospective official-path correction;
the original raw-path receipts remain archived with the correction record.

| Existing eight-task trace | New grep-completion lines / annotated | New read-completion lines / annotated |
| --- | ---: | ---: |
| Initial function-completion arm | 1,901 / 381 | 584 / 108 |
| Targeted-hop function control | 1,931 / 335 | 640 / 102 |
| Targeted-hop graph candidate | 2,123 / 278 | 382 / 115 |

Grep completion supplies 75–85% of completion's new physical lines, but also most
of its newly observed annotated lines. Disabling it may lose useful coverage;
these counts do not establish that read-only completion would improve quality.
Annotation overlap includes physical blank/comment lines, is not the official
score, and does not make unannotated source useless to a solver. This audit
attributes observations in recorded traces; it cannot predict how an online
agent would react to changed outputs. The next bounded development hypotheses
are selecting which matched callable to complete and avoiding graph responses
that only repeat known source. Receipts are in
`verification/contextbench-online-20261016/development-source-attribution.json`;
sanitized counts accompany the existing development summary.

### Early graph navigation and unseen-source development gate

The source-checkout graph tool accepts `--graph-source-policy definitions|unseen`.
`unseen` derives known lines from actual source-checked observations, skips fully
known neighbors without spending the result limit, and returns all missing lines
of a partly known definition only when they fit. It keeps the existing raw-edge,
definition-size, output and source-token limits. Each call snapshots prior
messages; there is no shared mutable ledger or retained lock. Parallel tool calls
may conservatively return overlapping source. Metadata alone never marks a line
observed. Both the native and SDK runners bind this observer to their own case.

The eight-call development replay checks that `definitions` output still equals
the original recorded output. `unseen` removes all 220 repeated physical lines
but adds none. Consequently the next model-facing policy requests a graph hop
as soon as a central callable is found and a caller/dependency/test remains
unclear, before broadening manual reads. All arms use the same new shared prompt;
old runs are not reused as controls.

Before inference, freeze the next development trial on the same disclosed eight
IDs with fresh native, function-completion and functions-plus-unseen-graph arms.
Use actual Opus 5.5, 20 turns, 600 seconds, $3/case and $10/arm SDK-meter guards,
2,048 completion/graph source tokens, the same prepared snapshots and graph
manifest, and no API fallback. The primary contrast is the complete graph route
against native. Report both adjacent ablations as exploratory.

The gate requires all selected tasks to finish and score, at least one graph
response to supply previously unseen annotated source, a positive macro Recall
point difference and non-decreasing F1 for the primary contrast. Annotation
overlap is computed only after inference, never supplied to graph navigation or
the model. Report paired intervals even if they cross zero; passing this small
development gate is candidate selection, not an independent improvement claim.
The prior 32 tasks stay sealed against tuning. Local receipts live under
`verification/contextbench-early-unseen-20261016/`.

The frozen trial at `768785a4` completes and officially scores all eight tasks in
every arm, with no SDK errors or missing metering:

| Cumulative observed trajectory | Recall | Precision | F1 | SDK dollar estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools | 39.15% | 35.74% | 34.27% | $2.010707 |
| Function completion | 43.97% | 31.57% | 34.02% | $2.666574 |
| Function completion + early/unseen graph tool | 44.03% | 36.41% | 36.60% | $2.588926 |

Primary Recall changes +4.88 points, paired 95% interval [-0.11, +10.45]; F1
changes +2.33 points, interval [-6.44, +11.03]. The graph-enabled arm makes
**zero graph calls on all eight tasks**, including the five with graphs. Its
new annotated source count is therefore zero and the predeclared gate fails.
The point differences do not establish a graph-source benefit. No new independent
selection is consumed. Before another quality cohort, verify tool adoption and
new relevant source in a small mechanism check; advisory wording alone has now
failed twice. Keep such probes out of quality scores and avoid repeating another
full cohort merely to discover that the tool was unused.

The parallel solver-accounting change records the exact serialized model request
before inference and confirms its input when an assistant response arrives.
`model-observations.json` exports confirmed, rendered histories rather than raw
shell stdout or the solver's terminal message list. A pinned native-loop test
proves that middle output elided by the 10,000-character renderer and an unrequested
submission diff are absent. Unknown delivery and missing historical input receipts
remain explicit. This is the raw full-agent observation boundary; source-line
attribution and the other native harnesses are still required. It changes no
retrieval runtime file used by the concurrent frozen development experiment.
Its live paired check at `54a8f15f` uses the same first development Verified task,
pinned image, model, 16K context limit, 30-step limit and $3 SDK-meter guard. Both
patches are **0/1 resolved** under native grading. All seven native-context model
inputs and eleven candidate-context inputs are confirmed and exported, with no
missing usage; SDK estimates are $0.485857 and $0.881937. Both owned containers
are removed. This revisits one development task, not two new independent tasks.
The public development summary retains this complete trial and its failed gate.

The October 17 source-receipt follow-up adds a single controlled reader at that
existing environment boundary. `codenib-read PATH START END` records a bounded
UTF-8 byte snapshot and its original blob digest, then binds the native rendered
output to its input position. Initial delivered source is validated separately.
Only complete lines in confirmed inputs and unchanged base-file versions receive
base coordinates. Ordinary shell observations, missing receipts, partial lines,
changed-file coordinates and undelivered submission output remain explicit.
The same reader instructions apply to both arms; their addition changes the
solver protocol and needs a fresh matched run, not retrospective rescoring.

The no-inference verification is retained at
`verification/contextbench-solver-source-receipts-20261017/` with protocol hash
`4c519cd6a84dd00b995379e5ef4555d4f53ce021840bf5b064a52f4c2e194853`.
It preserves ten historical fmt model inputs and validates 411/375 initial
source lines, without assigning any of the eight unreceipted shell observations.
A scripted loop in the exact source-only image verifies native truncation,
current-file edits and terminal exclusion; all hashes match and its one owned
container is removed. These simulated replies add no model calls, usage,
resolution result or official score. The public aggregate identifies the
scripted scope and partial-coverage limitations.

The predeclared real adoption check at
`verification/contextbench-solver-reader-adoption-20261017/` reuses the native
fmt 16K context, actual Opus 5.5, 12 turns and a $2 SDK-meter guard. Protocol hash
`18a526e32ca402b62bc00c169b262945df64ce717bdcc02404e38a779741ab87`
pins implementation `0a153397`. Five actual calls cost $0.695992 in SDK estimates.
The first action chains two readers after `cd`, receives command-not-found, and
falls back to `sed`: controlled-reader adoption fails, with zero confirmed
controlled reads. All four delivered shell observations have execution receipts
but remain unattributed; only 411 initial source lines receive partial coverage.
The production patch resolves the same development task with 18 native tests;
this supplies no comparative gain. Both owned containers are removed and frozen
runtime/input hashes match after grading. No retry or follow-on model batch is
part of this protocol.

The old command-not-found behavior is reproduced locally before fixing the
existing environment adapter. Compound command-position attempts now explain
that the reader is available as a standalone action; quoted shell mentions are
unchanged. The fix passes 29 related unit and two native-loop integration tests.
A separately frozen corrected-feedback check at
`verification/contextbench-solver-reader-adoption-v2-20261017/` uses implementation
`0b2b5964` and protocol
`3a5fa6cab44908dd4a92ffeac7f57080eb7faffb0f99947b453964756d8fa3c1`.
The same source, native 16K context, model and limits yield eight confirmed model
inputs at $0.932850 in SDK estimates, with `apiKeySource=none` throughout. After
one explicit usage error, two standalone reads reach the model. Adoption passes
and the patch passes all 18 native tests. Both reads were already in the initial
context: zero new original lines, 411 total partial-coverage lines and four
unattributed shell observations. Both containers are removed and frozen hashes
match after grading. This closes the corrected-interface mechanism gate without
establishing a quality gain. Fresh matched runs, arbitrary shell attribution and
broader native harnesses remain open.

### First-callable graph mechanism check

Before another model cohort, freeze a source-only diagnostic on the same eight
development traces. Select the first actual `read` response intersecting a
compiler function/method, then the callable with the most returned physical lines
(smallest span and lexical name break ties). Query its earliest observed line.
Both reference directions use that identical prefix independently, with five
results and 2,048 tokens per call. Annotation overlap is measured only after
selection and source retrieval. Three unavailable graphs remain explicit.

The TypeScript and Go outgoing responses fill all five slots with fields/types.
The revised tool prioritizes functions and methods within the same 1,000-raw-edge
scan; it preserves edge order within each group, reference direction and source
budgets. The replay holds every anchor and observed prefix fixed:

| Development language | Outgoing new physical lines, before → after | Annotated overlap, before → after |
| --- | ---: | ---: |
| Python | 59 → 64 | 0 → 0 |
| TypeScript | 5 → 128 | 0 → 0 |
| Go | 5 → 101 | 1 → 16 |
| JavaScript | 25 → 25 | 0 → 0 |
| Rust | 114 → 46 | 0 → 0 |

Incoming output is unchanged. Taking the union of both directions per task,
new physical source changes from 256 to 412 lines, with annotated overlap from
1 to 16 lines. The original annotated Go field line is displaced; the 16 returned
annotated lines are different source. Counts include physical blank/comment lines
and are not official Recall/F1 or a counterfactual online quality score. The
other available queries with zero annotated overlap remain
reported. No inference is used for this diagnostic.

The first original-issue Go probe calls the graph after function completion in
`grep`, before its first `read`. The response supplies 132 previously unseen
annotated physical lines and reaches a subsequent assistant turn, but the literal
predeclared **after-first-read** timing gate fails. Its $0.201015 SDK estimate and
failed timing gate remain recorded. This is useful early navigation, not a reason
to silently redefine that probe's gate.

The revised shared prompt explicitly counts callable source delivered through
either read or grep completion, and requires the next tool call to be the graph
query. It avoids requiring another hop just because a later `read` occurs. A
second version of the same original-issue probe freezes this source-observation
rule before inference. Each version has one attempt, eight turns, 180 seconds and
a $1 SDK-meter guard; neither supplies a selected source anchor or annotations to
the model. Both remain outside quality evaluation. Raw receipts and the frozen
implementations are under
`verification/contextbench-graph-mechanism-20261016/`; sanitized counts accompany
the existing public development summary.

The second version passes its predeclared timing and unseen-source gate: its
first callable-bearing reply is grep completion, its next tool call is the graph,
and a subsequent assistant turn confirms delivery of 132 newly observed annotated
physical lines. It reports $0.126101 in SDK metering. Both versions finish without
SDK errors or missing metering and use actual Opus 5.5. The first version's timing
failure remains reported; the two versions are not independent tasks. This
establishes working tool adoption and source delivery on one development issue,
not a quality improvement attributable to ordering or prompting. The next gate
was the frozen 24-task development comparison below. Its failed quality gate
does not advance to a new independent selection or full-500 run; the prior
negative validation and native-solver gates remain unchanged.

### First-source 24-task development outcome

Runtime `63e035eb` tests all disclosed 24 tasks, three per language, including
the eight earlier pilot IDs. Selection SHA-256 is
`122a70d4c61abcea5c27f87781fc204db768d933dd0d3bb7516e20117866f513`.
All three fresh arms use actual Opus 5.5, the same 20-turn / 600-second limits,
$3/case and $12/arm SDK-meter guards and 2,048-token completion/graph budgets.
Source preparation succeeds on all tasks; eleven graphs are available.

The primary native and graph-enabled arms both finish and score 24/24 tasks.
Recall changes 53.35% → 54.02%, paired difference +0.66 points, 95% interval
[-4.11, +5.59]. F1 changes 31.18% → 30.15%, difference -1.03, interval
[-3.71, +1.56]. The graph arm makes exactly 24 calls, including every available
graph. Six tasks receive 358 newly observed physical lines; 139 overlap
annotations on one Go and two Rust tasks. Each response reaches a later assistant
turn. Adoption and two-language novelty pass, but F1 fails the declared gate.

The function-only arm finishes four cases and aborts on C case `b8c88ac8` after
requesting the unregistered graph tool. The normalizer throws before terminal
SDK metering, so the next 19 cases remain `not_run`. The four complete traces
and the partial error trace receive five valid official scores; all 24 positions
remain in the macro denominator, with the other 19 zero. These incomplete
ablations cannot estimate completion or marginal graph benefit. The full declared
three-arm completion gate fails independently of the F1 failure.

Fix `25e5e31e`, developed separately while the frozen experiment ran, retains
unavailable attempts and SDK error feedback until terminal usage. Error outputs
earn no source credit; an unexpected advertised tool inventory remains fatal.
Only graph-enabled arms receive graph instructions, and their prompt is unchanged.
A separate C-task smoke at eight turns / 180 seconds / $1 SDK-meter guard passes
all receipt and isolation checks, with a $0.426619 estimate. No quality score is
assigned and no original record is replaced. Offline event replay preserves the
old unsupported attempt but cannot recover a terminal result that was never read.

Known retrieval estimates are $5.493784 native, $0.961724 function-only and
$6.375578 graph-enabled; one retrieval case remains unmetered. All 38 runtime
hashes match the frozen commit through official scoring. Raw receipts are under
`verification/contextbench-first-source-24-20261016/`; the existing public
development summary contains every selected task, gate and separate smoke.

An offline metadata-selected C++ probe diagnoses a preparation constraint:
`fmtlib__fmt-2394` needs a compilation database, but the benchmark worker disables
project preparation even inside its private clone. Under the same 300-second
bound, the control returns no graph in 4.16 seconds; enabling preparation builds
8,247 nodes / 39,953 edges in 18.18 seconds. The original checkout stays clean.
This uses no model calls or label-based selection and does not replace any frozen
artifact. The broader preparation outcome is recorded below; a new matched
quality candidate remains open. The completed independent 32 is not used for tuning.
The source-checkout graph preparer now accepts explicit
`--allow-project-preparation` for fresh private-clone builds and records the
policy/timeout. It rejects reuse with this option and rejects a prepared graph
if HEAD or tracked source changed. Generated build files may remain only within
the owned temporary clone; the existing bounded worker owns their cleanup.

A post-hoc final-source comparison retains every one of the 24 tasks. Of the
139 annotated graph lines new at delivery, 90 are also read by the native control
and only 49 are absent from it, all on one Go task. Candidate final source adds
5,151 physical lines and omits 4,204 native-read lines, including 472 added and
464 omitted annotated lines after canonical path normalization. These physical
counts are not official macro
scores; the interrupted function-only arm prevents causal decomposition. The
next hypothesis should emphasize selective navigation and coverage beyond the
matched native trajectory, rather than advancing on graph availability alone.
No new model calls or quality scores are produced by this diagnostic.

### Conversation-aware completion and fixed-query follow-up

Source attribution on all 24 existing candidate trajectories finds 894 annotated
lines first exposed by grep completion. Removing grep completion while holding
all later tool calls fixed would lose 635 annotated physical lines, so a blanket
grep-completion removal is not a supported next policy. Function completion also
repeats 726 previously delivered physical lines: 310 in grep additions and 416
in read additions. These counts include physical blank/comment lines and are
not official line scores or causal effects on subsequent agent choices.

Both source runners now accept `--completion-source-policy definitions|unseen`.
The historical `definitions` policy stays the default. The opt-in `unseen`
policy derives prior source from verified conversation text, preserves original
tool replies and appends only the remaining lines of currently touched
callables. Retain the historical selection and budget reservations before
omitting duplicates; freed space cannot select another function. Prior reads
cannot select unrelated functions. No shared mutable seen-set is introduced.
Concurrent calls may conservatively repeat source from the same model turn.
Configuration and resume checks retain the policy; a paired completion-source
contrast requires matching graph configuration and tools.

A frozen replay holds every actual tool call and original source reply fixed,
using the previous assistant-confirmed prefix for each parallel call batch.
The control reproduces all 234 source replies exactly. The new policy changes
20 replies, reduces repeated completion-line deliveries 726 → 0 and reply
characters 1,008,050 → 971,508 (3.63%), with identical cumulative physical source.
An initial implementation refilled the saved budget and added 63 non-annotated
lines, displacing 14 other non-annotated lines in 16K delivery. Both initial
replays remain recorded. The current implementation preserves callable
selection before removing duplicates to avoid changing downstream content.
Recomputing every candidate payload at 16K and 32K confirms exact equality of
context bytes, token counts and source predictions with the archived historical
payloads. The separate input/runtime hashes also match through this check.
This is delivery behavior, not a new quality score or measured model-token cost.
All input/runtime hashes match; no model calls occur. Native-loop and SDK-stream
regressions verify delivery, original-reply retention and rejection of unobserved
or truncated source. A deterministic small-budget regression reproduces the
unselected-function refill and verifies its rejection; comparison tests reject
a simultaneous graph-policy change.

The actual-query graph replay uses the same 24 old candidate queries and
prefixes for both prior and composed artifacts. All old responses reproduce
exactly. With 19 available graphs instead of eleven, new physical source rises
358 → 932 and canonical annotated source at delivery rises 139 → 356. Annotated
source absent from the complete native trajectory rises 49 → 194 lines, with
145 on C and 49 on Go. The earlier 49 → 49 claim missed container-prefixed
annotations and is withdrawn. This is useful offline source evidence, while
query timing/selection and fresh agent quality still require measurement. Private diagnostics,
hashes and source-bearing replay outputs remain under
`verification/contextbench-completion-source-diagnostic/`; sanitized counts are
in the existing public development asset. The independent 32 remains sealed and
both failed quality gates remain in force.

### Late issue-directed graph development control

The offline 24-task anchor probe preserves all source snapshots and the composed
19-graph inputs. For each available graph it queries incoming and outgoing
references, at five results, 2,048 tokens and 1,000 raw edges per direction.
Anchors and replies are fixed before annotations are checked. The first anchor
comes from the first native read intersecting a callable, with ties by observed
line count, shorter span and lexical node ID. A late-same control uses that anchor
with the full native prefix. A late-issue diagnostic ranks all actually observed
callables with the existing code-tokenized BM25 over symbol/file names.

All three policies make 38 queries. Annotated source absent from the full native
trajectory is 80 lines for both early-first and late-same, versus 50 for BM25;
the latter loses 30 Python lines and keeps only Go coverage. This is a negative
result for delay alone and this static ranking, not an online quality estimate.
All frozen input/runtime hashes match. Private source-bearing responses stay in
`verification/contextbench-late-graph-probe-20261016/`.

Runtime `5b91323c` adds an explicit `late_issue_reference_hop` prompt policy to
both runners, preserving the previous default and common non-graph prompt.
The model must investigate first, state a concrete unresolved relationship,
choose a previously observed callable by issue behavior, and query before its
final handoff. It may make at most two graph calls by prompt; the analysis audits
that limit rather than implying a hard quota. The model does not use the probe's
BM25 ranking. Configuration, resume validation and direct timing comparisons
retain the option and reject confounding source/budget/tool changes. Native and
SDK regressions verify the actual received prompt, tool inventory and unchanged
source receipts. The related unit tier has 165 passing tests; four pinned
integration tests also pass.

The new protocol selects the first task per language in the unchanged 24-task
development order, with no substitution and no overlap with the sealed 32.
All eight source records and seven available graphs are authenticated projections
of existing inputs. Three fresh arms share Opus 5.5, 20 turns, 600 seconds,
2,048-token completion/graph limits, conversation-aware completion, and $2/case
and $6/arm SDK-meter guards. The total observed-meter stop threshold is $18;
this is not an account bill or an exact cap on an in-flight turn. Existing Max
remains the route, without API fallback. The primary comparison is graph plus
completion against native; completion/native and graph/completion are separate
exploratory contrasts. Runtime, source, graph, selection and analysis hashes are
frozen before inference under `verification/contextbench-late-graph-8-20261016/`.

The predeclared development gate requires all selected tasks to finish and score,
known terminal metering, use of every available graph, at most two graph calls
per task, observed anchors after at least two source replies, and confirmed
delivery. At least two languages must receive annotated graph source absent from
the complete matched native trajectory. Primary Recall must increase and F1 must
not decrease. Passing would only select a development candidate; a new independent
protocol and broader native solver evidence would still be required.

All three arms complete and officially score 8/8, with known terminal usage,
actual `claude-opus-5-5`, eight Max-authenticated sessions per arm and no API-key
source or unavailable tool attempts. Native/function/graph Recall is
39.85%/40.40%/41.62%; F1 is 32.85%/31.38%/35.29%. The primary Recall difference
is +1.77 points, interval [-4.36, +8.23], and F1 +2.45, interval [-1.06, +7.06].
The secondary graph/functions F1 difference is +3.91 points, interval
[+0.33, +8.92], without multiplicity adjustment. This is development evidence.

Nine graph calls respect the count/timing/observed-anchor gates and cover all
seven available graphs. They return 220 new physical lines: 200 C and 20 Rust.
Canonical annotation overlap is 200 C lines, 197 absent from the matched native
trace, and zero on Rust. The two-language gate fails; the point-score gates pass.
Python/Go report no unseen neighbors, TypeScript/C++ have oversized source,
Java has no graph, and JavaScript has two missing-definition responses. The
completed mechanism diagnosis is recorded below; it does not change this gate.

The fixed-trajectory source-removal audit reproduces all eight original
predictions. Removing graph replies while preserving every later call changes
Recall 41.62% → 39.80% and F1 35.29% → 34.00%; all Recall loss belongs to C.
This establishes direct source-score contribution only, not a counterfactual
agent outcome. Runtime and input hashes match through scoring and the audit.
SDK estimates total $6.796707 ($2.038043 native, $2.433953 functions, $2.324711
graph), below the $18 stop threshold. Actual subscription billing remains unknown.

The source-removal check exposed an auxiliary diagnostic bug: ten of the 24
development tasks have container-prefixed annotations. Raw string intersection
missed them, although official scoring already normalized them. The evaluator
bridge now emits `Gold.line_spans_init()` as `gold_line_spans`; six native
integration variants cover relative, `/testbed/` and `/workspace/<repo>/` paths,
with and without a model patch. A zero-model audit corrects the saved 8/24-task
source diagnostics using the same pinned normalizer, without rebuilding source,
rerunning models or changing official metrics. It retains the original frozen
results and publishes explicit amendments in
`docs/assets/contextbench_annotation_correction.json`. The late pilot remains
rejected under its original two-language criterion. Independent validation and
official-500 gates remain closed.

The zero-model diagnosis in
`verification/contextbench-graph-range-diagnostic-20261016/` reproduces all nine
old graph replies and all three C++ graphs from unchanged cached clangd shards.
Its protocol hash is
`0e34596e4799ee11710da7713eb1173bebb850d23255b768d88cd096cb9ce34f`.
The range materializer previously chose the first containing tree-sitter chunk,
assigning several distinct methods the same macro-confused 520-line span. It now
chooses the smallest containing range; missing chunks retain the compiler point.
The three graphs change 33, five and zero ranges, respectively. Every compiler
identity, selection point and edge remains exact, and every changed span still
contains its definition point. At `include/fmt/color.h:411`, the seed changes
from an unrelated `rgb` constructor to the observed `ansi_color_escape` overload.
The corrected incoming query has no returned neighbors, and the other eight
replies remain identical. There is no added physical or annotated source and no
new online result. Historical graphs and scores stay frozen.

Reference-anchor windows provide zero unseen C++ lines through radius 64; the
TypeScript anchor provides zero through radius 16, five at 32 and 37 at 64.
Do not introduce a window-delivery policy on this evidence alone. The JavaScript
file has 36 field definitions and 42 file-owned references. Raw SCIP emits its
assigned property names as local references without callable definitions or
enclosing ranges; assignment-function syntax is already supported by the
chunker. Do not invent compiler-defined callables or call edges to hide the
provider gap. This patch changes no graph schema or native query contract; full
graph construction is shared by the Python and native-provider routes. The
regression first fails on the old range choice, then passes for both chunk
orders; 41 related tests and 48 compiled native-query tests pass. Useful graph
delivery across languages, provider definition coverage and revised matched
quality evidence remain the open gates. This diagnosis uses no model calls or
additional API spend.

### Broader offline graph preparation outcome

The complete 24-task check at `64f07462` freezes runtime hashes and enables
private-clone preparation with a 300-second bound. Fourteen graphs are available:
eight new successes and five losses versus the previous eleven. Six Go/Java
failures come from missing managed tools on this invocation PATH, two TypeScript
builds time out, and two builds change tracked source and are rejected. The
managed tools are installed; the doctor still reports unrelated missing tools.
All original source snapshots remain clean. Policy, timeout and environment
changes prevent a causal preparation-policy claim.

A fixed-prefix replay chooses anchors without annotations and tests both edge
directions before checking annotation overlap. New physical lines change
687 → 890; canonical annotated lines change 177 → 44, including the lost Go coverage.
It makes zero model calls and does not measure online agent adoption or quality.
A separate future input uses fresh available graphs and otherwise authenticated
same-source prior artifacts, producing 19/24 available graphs. It retains all
cases and both build histories; no scores or annotations select artifacts and
no previous result is overwritten. The next candidate still requires a frozen
matched protocol and meaningful new-source evidence.

### Multi-SWE-bench native environment readiness

On the development C++ task `fmtlib__fmt-2394`, the original Multi harness at
`24f493f8a103e72312ded4f6b9c89f081d69cb09` rejects the test-only control and accepts
the reference repair. Its native grader verifies all 18 required tests, with no
failures. Both stages use the same pinned official image, exact selected source
tree and native patch hashes. They run the original test scripts with no network
or host mounts, bounded resources, and remove their owned containers afterward.
No model calls occur and neither result is an agent score.

The original grading image contains the reference and test patches. A separate
solver image removes those artifacts and their test/prepare scripts, replaces
Git history with one selected-source commit, and passes the same tree and clean
checkout checks. Its tree remains
`c793e859f77f2ef96bcc8f1b1804f06ebce087aa`. Native grading continues to use the
original image. The source-checkout solver now accepts the `fmtlib/fmt` pilot,
checks source-only history/reference isolation before model work, and records the
source family and container working directory. The separate native adapter
checks installed harness hashes and source/patch identity, replaces the reference
production patch with the exact solver patch, and invokes the original native
script and resolution grader. Every owned container has bounded lifetime and
synchronous cleanup; errors remain explicit. This does not establish that
CodeNib resolves the issue or supports other Multi repositories.

The new adapter's unchanged-issue control is correctly unresolved. Two initial
reference checks time out during Docker creation at the 60-second bound, before
tests begin, and both owned containers are removed. A separate create/start
probe succeeds, with creation taking 31.83 seconds amid high host I/O pressure.
The adapter now allows a bounded 180 seconds for container startup, records that
limit, and retains its test and cleanup bounds. Fresh native validation at
`fe4cbb72` completes with unchanged runtime hashes: the unchanged-issue control
is unresolved and the reference repair passes all 18 tests. Both containers are
removed. All these checks make zero model calls; prior failures remain recorded.
The bounded C++ solver pair at the same frozen commit reuses the previous
native and graph-configured 16K contexts. Both arms submit production-only fixes
and resolve this one unique development task, 1/1 each, passing all 18 tests.
Both use Opus 5.5 through Max, the same source-only image, 20 turns and a $2 SDK
meter guard. Each makes five calls, all ten exact model inputs are confirmed,
and all terminal usage is present: $0.734907 native and $0.586895 candidate,
$1.321802 total. Every solver/grader container is removed, and frozen runtime
and input hashes match through grading. No new retrieval or independent
validation occurs. The graph was unavailable in that task's original retrieval;
these matched repairs do not establish a graph-source or comparative Pass@1 gain.

Pinned native dataset revision, image IDs, module hashes and reports are retained
under `verification/contextbench-multiswe-native-20261016/` and summarized in the
existing public development artifact.

The disclosed Java development task `google__gson-1391` now passes the corresponding
gates. Native protocol
`0ddc4234dc8f136cdb76d9ed1c9a816086ca29d849c18c75a04c68f5330e9cc9`
rejects the test-only control and accepts the reference patch: one required
regression class changes from failing to passing, and all 95 preservation classes
pass in both stages. Both Maven exits are 1 because `DefaultDateTypeAdapterTest`
also fails in the pinned dataset. Full parser sets match its original outcomes;
do not call the entire test suite green or count classes as individual assertions.

The source-only Gson image removes reference files and history while retaining
the exact clean tree. Python 3.10.12 runs the controlled reader successfully.
Adapter protocol
`c4bb87f172c152ed8896ee14944c2aed1dc87c597d58c5c7dfafec157c9bc3be`
rejects a behavior-preserving comment and accepts the reference production patch
through the implemented native grader. These are synthetic grading fixtures,
not solver attempts. The adapter binds repository/task/source identity and all
three native entrypoint hashes before patch execution; the new checks also match
the existing fmt image. All owned containers are removed and frozen hashes match.
Receipts are under `verification/contextbench-multiswe-java-readiness-20261017/`;
the public development artifact retains provenance, the source-only Dockerfile,
both controls and parser parity. All Java readiness checks make zero model calls.

Jackson-databind is deferred because its pinned preparation script rewrites a
tracked POM, conflicting with exact clean-source validation. This is a source
inspection, not a failed runtime probe.

The fresh Gson solver pair at frozen `0039b2a2` is complete under protocol
`3e42a546571c16b1ebc33b27eff275156807b3b28348b66d21be6a9c59aa35f2`,
with artifacts at `verification/contextbench-multiswe-gson-solver-pair-20261017/`.
Both arms submit production-only fixes and resolve 1/1 task, passing all 96
required test classes. The known always-failing class remains present. Source,
image, Opus 5.5/medium, reader instructions, 16K context limit, 20 steps and $2
SDK guards match. All eleven input receipts are confirmed, every SDK init uses
`apiKeySource=none`, and usage is complete: $0.500642 native, $0.587861 candidate,
$1.088503 total. All four owned containers are removed and frozen hashes match
after grading. The separate native results never enter either solver.

Each arm adopts one controlled read after an explicit usage error. Source coverage
changes 318 → 318 and 293 → 294 lines; two/three delivered shell observations
remain unattributed. Original retrieval had no available graph. This expands
native pipeline coverage but shows no comparative repair or graph-source gain.
Other Multi repositories, Poly/Pro harnesses, complete solver-source attribution
and broader matched outcomes remain open.

The next native pilot uses disclosed C task `ponylang__ponyc-2205`. Its selection
is informed by the earlier development graph contribution, not an independent
sample: all 200 graph-returned lines remain in the candidate's fixed 16K payload,
197 absent from native. No solver result or reference-patch content selects a
new task. Reference/control protocol
`a7a21664742a4d07ca15b24076d2195be7b1affdf445ed2d2d8f05bb0da75ecd`
preserves 1,010 tests and fails seven regressions in the control; the reference
passes all 1,057 required tests. All native parser sets match the pinned dataset.
Both native exits remain 2. The reference reaches a standard-library link failure
for missing `crypto`, `pcre2-8` and `ssl`; a separate unmodified-source check
reproduces it after 1,045 passing baseline tests. Required-label success must not
be described as a green full standard-library suite.

The original grader image lacks the Python 3 interpreter required by the reader.
A private source-only image adds the
hash-verified CPython 3.11.14 standalone archive and removes reference artifacts
and Git history. Its warm variant builds only unmodified source, keeping model
test commands within the existing time bounds. Warm source/reader protocol
`ea72f3722cf7f170abcf75664a93487512171c8f19765ce21c44c3d8c06599af`
passes, source remains clean after baseline testing and all owned containers are
removed. Native grading still uses the original image. The existing adapter adds
only this validated repository and its pinned native module; no registry or new
product route is introduced. Related unit tests pass 197 cases and pinned
integration tests pass ten. All readiness checks make zero model calls.
Artifacts are under `verification/contextbench-multiswe-pony-readiness-20261017/`.
The frozen C solver protocol
`ea199d9940f8bc77a2cdaaba917117af1e8349fd2e9bf1cf85e6ec7bf33d26c1`
at `6c1585b8` is now complete: native/functions both submit production patches,
then fail original grading with 1,005/1,006 passing and 12/11 failing tests. They
fix zero/one of seven required regressions and both break five preservation tests.
The graph arm reaches its SDK budget error, supplies no prediction and remains
zero; no retry or native report substitutes for it. The three arms make 9/9/11
calls, all 29 inputs are confirmed and every subscription initializer reports
`apiKeySource=none`. Controlled reads add 0/191/0 original lines, with partial
coverage of 1,025/1,677/1,393. All five owned containers are removed and confirmed
absent, and frozen hashes match after grading.

The function-only payload already covers the earlier graph response. Relative to
it, the graph payload adds 190 and removes 283 other initial lines; this is not a
controlled graph-only source intervention. No comparative repair gain follows.
SDK estimates reconcile to $2.134897/$2.684968/$3.295746, totaling $8.115611.
Amendment `955db06b47399b76fec281ab182e7c17d77cb48cb2c67e519487b7941ad9694e`
recovers the graph call's $0.321885 from its preserved terminal event without
changing any original run, patch or outcome. A ResultMessage followed by an SDK
iterator exception bypassed post-loop accounting. The reproduced regression now
passes: terminal usage is persisted before the next iteration, while errors and
the exhausted-budget stop remain. This correction makes no model calls.
The per-call SDK guard is not a hard billing cap; the terminating call can exceed
its remainder. Two historical attempts still have no terminal usage.
Artifacts are under
`verification/contextbench-multiswe-pony-solver-three-arm-20261017/`.
Use the retained required/preservation failures and source redundancy to improve
selection before another paid comparison. Independent quality, broader native
coverage and full-500 gates remain open; the sealed 32 is unchanged.

### October 16 online development result

The eight tasks are the first CSV-ordered task per language in the disclosed
24-task development selection. Both arms use actual `claude-opus-5-5`, SDK
0.2.165, CLI 2.1.301, a 20-turn SDK limit, 600-second case timeout, $3/case and
$15/arm SDK-meter guards. Function completion adds at most 2,048 source tokens
per response. Both arms have eight `ok` results and eight valid official scores.
The same implementation and input-manifest hashes were checked by `compare`.

| Cumulative observed trajectory | Recall | Precision | F1 | SDK dollar estimate |
| --- | ---: | ---: | ---: | ---: |
| Native source tools | 41.13% | 33.94% | 33.67% | $2.153231 |
| Online function completion | 41.67% | 29.33% | 31.91% | $2.406782 |

The paired Recall difference is +0.53 percentage points, with a 95% bootstrap
interval of [-2.03, +3.36]. F1 changes by -1.76 points, interval [-4.94, +1.04].
These task-level macro scores do not demonstrate an improvement. The offline
24-task gain cannot be transferred to this online policy or this SDK harness.
Artifacts and per-task values are in
`verification/contextbench-online-20261016/development-comparison.json`.

The first completed downstream solver attempt uses the one-task development
smoke's function-completed 16K context, the pinned upstream mini SWE-agent loop,
and the same actual Opus model. Its patch applies, but native required tests do
not pass: **0/1 resolved**, without a comparative or population claim. Generation
reports $0.555974 in SDK metering. Preliminary aborted attempts are also retained;
one returned no usage, so total metering and actual billing must not be presented
as fully known. The adapter checks the image's clean Git tree against the selected
base tree, allowing the official image's synthetic preparation commit. The native
dataset is pinned to revision `c104f840cc67f8b6eec6f759ebc8b2693d585d4a`.
Only the issue and observed retrieval payload enter the solver; reference patches
and required-test annotations are isolated to the native grading stage.

A fresh paired solver check uses the targeted-hop trial's Python case, the same
16K delivery limit, implementation `3f04993f`, pinned image, actual model,
30-step limit and $3 SDK-meter guard. Both patches apply, but both arms are
**0/1 resolved**. Native test collection fails because the generated production
patches omit `IS_PYPY`, which the reference test patch imports. The official
reference production patch passes in the exact same image and native harness;
that separate grading-only check makes no model calls and is excluded from agent
scores. Solver SDK estimates are $0.826470 for the control and $0.442726 for the
graph candidate. This revisits one disclosed development task, not two new
independent tasks or a population Pass@1 estimate. No reference-test feedback or
reference production patch is sent to either solver.

Directed graph controls use a new shared implementation freeze. Their source
tools retain function completion in both arms; only the candidate receives
`graph_neighbors`. Both arms receive the same graph manifest, per-response graph
budget, runtime, model and case limits. Unavailable graphs remain selected.
Do not compare a new graph run directly with the earlier frozen eight-task arm
and attribute all differences to the graph tool.

Before scoring the targeted-hop trial, freeze the development-to-validation gate:
both fresh arms must finish and score every selected task; the candidate must
actually call the graph tool on every graph-available task and receive source on
at least one; its macro Recall must increase without a lower macro F1. This is a
candidate-selection gate, not proof of an improvement. Report both paired
intervals even when they cross zero. Only a passing candidate advances unchanged
to the independent 32 tasks; promotion still requires independent quality and
downstream outcomes. Do not select individual best task runs or tune on the 32.

The first optional-graph trial, frozen at `359357a2`, completes and officially
scores all eight tasks in both arms. Its function-only control has 40.23% Recall,
28.19% precision and 31.11% F1; the graph-enabled candidate has 44.56%, 32.14%
and 34.28%. The paired Recall difference is +4.34 points, 95% interval
[-0.06, +11.88]; F1 is +3.17 points, interval [-0.81, +7.46]. However, the
candidate makes **zero graph calls on all eight tasks**. These differences
cannot establish a benefit from graph-returned source. Combined SDK metering is
$5.165080, not a paid receipt. A separate explicit-call integration probe makes
one successful graph call ($0.028054 SDK estimate); its synthetic instruction is
excluded from quality scores. The next development trial requires a targeted hop
before handoff and fixes local-symbol selection hiding enclosing callables.
It uses two fresh arms under the same new implementation, not an old control.

That targeted-hop trial is complete at `3f04993f`, with all eight tasks valid in
both arms. The function control scores 37.43% Recall, 28.40% precision and 30.18%
F1 ($2.497661 SDK estimate); the graph candidate scores 41.29%, 32.16% and 32.85%
($2.702899). Recall changes +3.86 points, paired 95% interval [-0.51, +11.82];
F1 changes +2.67 points, interval [-1.11, +6.84]. Each task makes one graph call:
Python, Go and Rust receive source; TypeScript's definition exceeds the response
budget; JavaScript has no definition at the queried line; C, C++ and Java report
unavailable graphs. All remain in the scores. The declared development gate
passes, but the intervals do not establish a reliable gain. The policy advances
unchanged to the independent 32 tasks; no product or leaderboard claim follows.

## Graph-free feedback and context-budget diagnosis

Select three cases per language from the frozen 100-task development cohort,
using repository round-robin and SHA-256 ordering with seed
`decision-loop-v1-2026-10-11`. All **24 tasks, 24 repositories and eight languages**
stay in the denominator. Selection does not consult scores, annotations or
preparation success; all selected snapshots happen to prepare successfully.
This is an exploratory development subset, not a representative or held-out
estimate of the verified 500. The same dataset and evaluator revisions below
apply to every arm.

The chunk-based loop shares each task's recorded Opus 5.5 initial grep plan,
candidate scores and exact initial payload with the one-shot control. It uses
actual source observations to choose identifier search, adjacent-chunk read,
feedback replanning or stopping. Only novel candidates receive new Perplexity
scores; seed candidates are never evicted from the pool. No graph, embedding
index or BM25 fallback runs. The inference interface receives the issue and
source, not benchmark annotations. Stop, budget exhaustion or scoring failure
retains the last complete payload; partial/error status remains explicit.

The legacy flat policy permits three steps, one Opus replan, 80 total candidates
and 12 new candidates per action. Both expanded controls permit six steps, six
replans, 160 candidates and 24 additions per action, with at least two expansion
attempts. Their binary continuation decision is separate from action selection.
The declared 0.5 threshold is not calibrated confidence; overridden stop
decisions are retained in the trace. The feedback control invokes Opus on each
continuing step; the decision control can instead choose local grep or read.
All three retained top-10/4096-token output for the initial paired comparison.

| Same 24 tasks, final 4K context | Line Recall | Line precision | Line F1 | Candidate Recall ceiling | New API bill, USD |
| --- | ---: | ---: | ---: | ---: | ---: |
| Shared one-shot Opus grep + decisions | 36.73% | 36.85% | 29.29% | 40.89% | reused |
| Flat decision loop | 41.65% | 37.45% | 30.47% | 47.77% | 0.21200538 |
| Expanded decision loop | 37.07% | 38.34% | 29.30% | 44.52% | 1.46935764 |
| Expanded Opus feedback loop | 37.90% | 39.07% | 30.04% | 55.57% | 3.74157388 |

Metrics are per-task macro means of official line scores, with all 24 valid
in each arm. F1 is computed per task before averaging. The new calls cost
**$5.42293690** in total; the shared initial inference cost **$0.42127482** was
already paid in the 100-task experiment and is not billed again. Serving cost
for one route must still include that shared initial inference. The expanded
controls had generous observed-spend guards ($10 per variant, $1 per case);
none hit a cost guard or had an API failure. Their quality loss is not a budget
exhaustion failure. These are API dollars, excluding local compute and a solver.

The flat controller stops immediately on 16/24 cases. In 11 of its 24 stop
decisions, stop has less than 0.5 probability but is the largest single action:
continuation probability is divided across many alternatives. This motivates
the binary control, but the subsequent experiment changes multiple policies
and does not isolate the effect of the stop rule. More expansions alone do not
improve the final 4K scores.

There is a material discovery/delivery mismatch. The expanded Opus feedback
pool averages 46.21 candidates and covers 55.57% of gold lines, yet final packing
retains only 37.90%: **17.67 Recall points are lost between the pool and delivery**.
The expanded decision loop delivers about 274 unique lines per case, while the
published JiuwenSwarm trajectories average 1,248.5 lines on these same IDs.
The original frozen chunks are themselves limited to approximately 100 lines
and 3000 characters each. Candidate ceilings are diagnostics; source that only
the reranker saw is not retroactively credited as a downstream agent trajectory.

The [published JiuwenSwarm results](https://github.com/ContextBench/contextbench.github.io/tree/main/submissions/2026-09-12_openjiuwen-jiuwenswarm)
recomputed on these **same 24 original IDs** give **64.46% Recall / 27.45% F1**,
29.375 recorded steps and $0.64534 mean cost. Its full agent includes solving
and testing, whereas these controls serve context only. The initial flat loop
is 22.81 Recall points below this matched reference. Higher F1 under a smaller
context budget does not establish a better agent. The full leaderboard's 75.3%
Recall is a different population and is not the matched 24-task reference.

Removing the chunk-count cap and replaying the same scored candidates gives
the following output-budget curve. These are offline line scores with no new
inference; the audit independently reproduces 96 primary official score records
and checks the faster replay packer against their exact payloads and 120 generated
boundary cases. Both the baseline and every loop receive each budget.
The one-shot and Opus feedback 32K arms are also rescored with the pinned official
evaluator; their 48 task-level records agree with the offline results.

| Same candidates and scores | 4K Recall, no chunk cap | 8K Recall | 16K Recall | 32K Recall | 32K F1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| One-shot Opus grep + decisions | 37.67% | 39.36% | 40.54% | 40.89% | 24.67% |
| Flat decision loop | 42.59% | 45.67% | 47.43% | 47.77% | 24.41% |
| Expanded decision loop | 38.00% | 41.83% | 43.21% | 44.15% | 20.27% |
| Expanded Opus feedback loop | 39.12% | 48.29% | 49.49% | **55.11%** | 19.72% |

Thus the Opus feedback output moves from the original top-10/4K **37.90%** to
no-chunk-cap/32K **55.11%**, a **17.21-point** improvement. It averages 18,157 actual
tokens under the larger cap. F1 falls from 30.04% to 19.72%, so retaining more
source alone does not solve precision. The 32K one-shot control reaches only
40.89%; its remaining limit is discovery. All three 4K loop improvements have
paired 95% bootstrap intervals crossing zero on these 24 development cases.

### Native source-search control

`benchmark_context_agent.py` reuses the existing `AgentRunner` with a focused
retrieval prompt and only its native `grep`, `glob` and `read` tools. Opus 5.5
with medium reasoning generates searches and read ranges after inspecting actual
tool results. It can inspect complete implementations, callers, helpers,
configuration and tests directly in the frozen repository, without the earlier
eligible-chunk filter. It builds no graph or embedding index and receives no
gold annotations or reference patch. This changes source access and controller
as well as context policy, so it is not a single-factor output-budget ablation.

The policy allows 20 exploration turns, multiple tool calls per turn, one grounded
review of the proposed final handoff, and a tool-free final call if the turn limit
is reached. The full conversation is retained without
history eviction; 120K estimated input tokens is a safety guard. Individual
native tool responses keep their existing size bounds and continuation notices.
The 4096 `max_tokens` request setting bounds model-generated output, **not**
source context. Actual delivered tool text forms the official cumulative
trajectory. Separate 16,384- and 32,768-token payloads contain only complete,
verified observed source lines, deduplicated without a chunk-count cap. Their
declared order is first-observed file, then source line, with no label-based
selection or automatic completion of unread ranges.

Four worker lanes have disjoint equal observed-cost budgets, processed serially
within each lane. Unknown usage cancels new calls in all lanes; in-flight calls
may complete. There are no automatic request retries, and every request/response
and provider-reported charge is checkpointed. The initial route preflight sent
four requests with forced `tool_choice=required`, which OpenRouter rejected with
HTTP 404 before any successful inference. The endpoint catalog advertises only
`auto`/`none` for this model. Those requests have no usage receipts and remain
explicit in `verification/context-agent-24-live/`; they are not quality results
or silently treated as priced model calls. The corrected run uses `auto` and
lives in `verification/context-agent-24-v2-live/`.

The completed, audited result is `verification/context-agent-24-complete/`:
**24/24 tasks valid**, 278 successful Opus calls, **$23.786400** total known API
charges across the initial run and explicit recoveries ($0.99110/task).
Recovery reuses exact successful model requests/responses and the exact returned
tool text, bound to clean source snapshots and receipt hashes. This matters
because repeating a head-limited grep can change traversal order and invalidate
the cached model prefix. Previously completed calls are never billed again.
Two phases received HTTP 402 `in_flight_budget_exhausted`; these are provider
reservation failures, not quality results or proof that account balance is zero.
Their rejected requests have no usage receipt. The final successful phase adds
$0.738944; it reuses $23.047456 in prior successful calls. This accounting excludes
the earlier chunk-based experiment and local compute.

| Same 24 tasks, native source agent | Line Recall | Line precision | Line F1 | Mean unique lines | Mean delivery tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| Complete observed trajectory | **57.50%** | 25.92% | 29.76% | 864.46 | full trajectory |
| Final 16K serving payload | 56.68% | 27.12% | **30.81%** | 711.17 | 9,221 |
| Final 32K serving payload | 57.26% | 26.06% | 29.94% | 819.13 | 10,722 |
| Published JiuwenSwarm, matched IDs | 64.46% | 22.13% | 27.45% | 1,248.50 | different solver trajectory |

The native trajectory is **6.96 Recall points below** the matched leader. Its
paired 95% bootstrap interval for this difference is [-15.59, -0.01] points; the
F1 difference is +2.32 points with interval [-1.47, +5.80]. These exploratory
development estimates do not establish superiority. The native route is also
more expensive than the matched public full agent's reported $0.64534/task,
despite excluding a solver. It is a quality reference, not an ultra-low-cost claim.

The independent audit verifies all 72 official task-level score records, exact
physical lines, unique packing, source snapshots and charges across all recovery
phases. The serving parser now handles the native grep formatter's separator
space without discarding grep-only observations. The final 32K delivery loses
only **0.25 Recall points** against the actual trajectory. The main remaining
gap is discovery: gold restricted to already encountered file names has an
**87.20% diagnostic Recall ceiling**, well above the actual 57.50% viewed lines.
That ceiling is not a submitted retrieval score and does not justify reading
entire files; it motivates testing complete callable boundaries and dependency
coverage before another larger-model or budget change.

This strong-model reference does **not** invoke the independent decision model.
The 100-task recorded ablation below supports decision reranking (Opus grep
Recall 39.88% without it versus 45.70% with it), whereas the 24-task expanded
controller does not establish a reliable action-selection or stopping benefit.
The paired final-context control below measures its usefulness under the wider
budget. Replacing the whole search controller with decisions remains unproven.

### Serving refinement: selection versus reading missing source

Both controls reuse exactly the 24 complete native trajectories. `decisions`
splits actually observed source at physical line boundaries (100 lines / 3000
characters per scoring unit), then applies the existing Perplexity relevance
scorer with no final chunk-count cap. Splitting preserves complete delivery
lines; a single longer line is retained even though the scorer sees only its
first 3000 characters. This control costs **$0.05940868** for all 24 tasks.

`functions` makes new deterministic local reads: for an observed source line,
complete the intersected function or method using the registered language
chunker. Do not complete whole classes/files or recursively expand from added
lines; skip callables over 1500 lines. Retain the original observations first,
then append complete callables, deduplicating within the final token budget.
This reads additional source with **zero new model calls**. The original Opus
trajectory is unchanged; the additions belong only to this new serving stage.
Neither policy uses benchmark annotations or scores as inference inputs.

| Same 24 tasks | 16K Recall | 16K F1 | 32K Recall | 32K F1 |
| --- | ---: | ---: | ---: | ---: |
| Original native delivery | 56.68% | 30.81% | 57.26% | 29.94% |
| Decision rerank of observed source | 56.76% | 30.81% | 57.24% | 29.94% |
| Complete touched callables | **64.97%** | **29.06%** | **66.95%** | **27.42%** |
| Matched public full-agent trajectory | 64.46% | 27.45% | 64.46% | 27.45% |

The function control improves same-budget Recall by **8.29 points at 16K** and
**9.69 points at 32K**, with lower F1 than the smaller original delivery. At 16K
both point estimates exceed the matched leader; at 32K Recall is 2.48 points
higher and F1 is essentially tied (-0.02 points). The paired 95% Recall intervals
versus that leader are [-7.71, +7.61] and [-5.75, +9.37] points respectively.
These development intervals cross zero: **this is not a SOTA claim**.

The decision control changes 16K Recall by only +0.08 points and 32K by -0.02.
Most already-observed source fits, so ordering alone has little remaining room
to help. This does not contradict the earlier 100-task 4K reranking gain, nor
does it establish that a decision model can choose searches or stopping well.
Keep decisions optional and require a measured benefit for each role.

`verification/audit_context_refinement.py` independently verifies the 96 official
task/budget scores, physical source, new-line provenance, no changes to original
messages/trajectory, complete decision scoring and receipts. Results live under
`verification/context-agent-24-functions/`, `verification/context-agent-24-decisions/`
and `verification/context-refinement-audit.json`. The combined `functions_decisions`
method is available but **has not been run**.

### Claude subscription transport

The installed Claude Code 2.1.296 is logged in through `claude.ai` with a Max plan.
The [official subscription guidance](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
states that Agent SDK and `claude -p` can use subscription limits. The experiment
uses `claude-agent-sdk==0.2.165` in an isolated virtual environment and the existing
CLI login; it rejects API-key/provider environment overrides and has no
OpenRouter fallback. No OAuth secret is extracted or passed to another provider.

SDK sessions expose only three in-process MCP tools backed by the same confined
CodeNib `read`, `grep`, and `glob` implementations. Built-in tools, inherited
settings, skills, plugins, other MCP servers and subagents are disabled. A fresh
session is used per case and the advertised tool set is checked. Raw SDK events
are checkpointed and actual tool-result text is normalized for the existing
official scorer; requested-but-unreturned ranges earn no credit.

The fixed first development task (`pylint-dev__pylint-4604`) passes an end-to-end
smoke: actual model **`claude-opus-5-5`**, eight turns, seven source-tool calls,
clean source before/after and one valid official score. Its observed Recall/F1
is 48.96%/52.68%, solely a transport smoke, not a quality comparison. Evidence is
under `verification/context-claude-smoke-1-live/`. The SDK reports $0.116873 in
metered cost, recorded as an estimate; **actual billed dollars are unknown**.
The run used the subscription authentication route and made no OpenRouter call.

This backend uses the SDK's agent loop rather than `AgentRunner`; even with the
same model, prompt and source primitives it is a different harness and needs a
fresh paired quality reference. The smoke does not establish equivalent scores
or unlimited/free usage. The development-24 SDK batch used up to **40 model turns**
per task and sequential execution, with no independent decision model. It was
stopped at the user's request after **3 completed, 1 interrupted and 20 unrun**
tasks. Completed tasks report $0.6709191 in SDK metering; the interrupted task has
no final meter and actual billing remains unknown. The partial batch is not a
24-task quality result. Its larger turn allowance and different harness would
also prevent attributing a quality difference solely to transport.

For future reproduction, new source-only cohorts can be prepared without a graph,
chunk index or initial grep-model run:

```bash
python scripts/benchmark_context_agent.py prepare \
  --cases verification/context-agent-holdout-32/selected.jsonl \
  --output verification/context-agent-holdout-32-prepared
/tmp/codenib-contextbench-claude-venv/bin/python scripts/benchmark_context_claude.py \
  --prepared verification/decision-loop-24-v2 \
  --output verification/context-claude-24-live --model opus --max-turns 40 --live
python scripts/benchmark_context_agent.py evaluate \
  --output verification/context-claude-24-live \
  --upstream /tmp/codenib-contextbench-upstream \
  --evaluator-python /tmp/codenib-contextbench-evaluator-venv/bin/python
```

### Loop reproduction and receipts

```bash
python scripts/benchmark_decision_loop.py prepare \
  --cohort verification/context-serving-100-v3 \
  --baseline verification/context-serving-100-replayed \
  --output verification/decision-loop-24-v2 --model opus \
  --max-steps 6 --max-replans 6 --candidate-limit 160 \
  --per-action-limit 24 --min-steps 2 --stop-rule binary
python scripts/benchmark_decision_loop.py run \
  --prepared verification/decision-loop-24-v2 \
  --output verification/decision-loop-24-v2-live \
  --max-cost-usd 10 --case-cost-usd 1 --live
python scripts/benchmark_decision_loop.py evaluate \
  --prepared verification/decision-loop-24-v2 \
  --run verification/decision-loop-24-v2-live \
  --upstream /tmp/codenib-contextbench-upstream \
  --evaluator-python /tmp/codenib-contextbench-evaluator-venv/bin/python
python scripts/benchmark_context_agent.py run \
  --prepared verification/decision-loop-24-v2 \
  --output verification/context-agent-24-v2-live \
  --max-turns 20 --workers 4 --max-cost-usd 30 --case-cost-usd 3 --live
python scripts/benchmark_context_agent.py evaluate \
  --output verification/context-agent-24-complete \
  --upstream /tmp/codenib-contextbench-upstream \
  --evaluator-python /tmp/codenib-contextbench-evaluator-venv/bin/python
```

Output directories must be new. To reproduce the feedback control, prepare a
separate directory with `--action-mode feedback` and otherwise identical bounds.
The earlier flat policy is frozen under `decision-loop-24-v1`; its paid outputs
are under `decision-loop-24-live`. Preparation archives the implementation and
hashes selected tasks, source artifacts, cached seeds and policy before billing.
New live runs reject implementation/input drift. Read-only evaluation checks
the archived implementation while allowing later checkout changes.

`run --resume-from <previous-run>` requires a new output directory and reuses
complete results or exact successful prefixes. `repack --resume-from <run>`
recomputes final delivery offline and requires all input cases to be complete.
Serving-stage `refine --input <run> --method decisions|functions|functions_decisions`
keeps the original trajectory unchanged. Decision methods require `--live`;
the function-only control performs local source reads without a model call.

The loop audit under `verification/decision-loop-24-v2/audit.py` checks frozen
inputs, exact source lines, unique packing, official scores, novel-only reranking
and provider charges. Its 4K/8K/16K/32K offline replays use identical recorded
scores and pools for all arms; they add no API calls. Native trajectory and
serving audits live under `verification/context-agent-24-complete/audit.py`.
After integration with `main` at `4232ddfa`, the evaluation/dataset/grep tier passes
**510 tests**, with seven optional tests deselected. All **three pinned official
evaluator integration tests** pass. Strict MkDocs, the public-document boundary,
the dependency lock check and the pinned pre-commit hooks pass. The committed CSV
and JSON agree on all 24 task IDs, aggregate metrics, eight paired confidence
intervals and recorded API totals. Local tool tests verify
multi-hop reading, retained context beyond 4K, exclusion of unseen/truncated
lines, path confinement, billing failures, stop behavior, function completion,
source-only preparation and subscription-only authentication/trajectory conversion.

Promotion remains gated on a frozen held-out comparison and actual downstream
task outcomes, with all explored development IDs disclosed. Establish the strong
search/read reference before distilling actions or stopping into a cheaper
decision controller. This work introduces no product retrieval route or
persistence boundary. The historical implementation and results were merged in
[#827](https://github.com/sysevol-ai/CodeNib/pull/827). The online follow-up above
is active in [#834](https://github.com/sysevol-ai/CodeNib/pull/834); the promotion
gates remain open.

## Earlier graph and fixed-payload evidence

The [100-task multilingual follow-up](#frozen-multilingual-serving-cohort)
confirms low serving API cost: about **$0.0119/task with Sonnet** and
**$0.0205/task with Opus** for one grep/graph/decision route. With Opus, graph
expansion raises the all-selected-task Recall/F1 from **45.70%/27.84%** to
**47.64%/28.95%**. Sonnet gains Recall but loses F1. Only 48 tasks produce graphs
under the bounded cold-build policy, so this remains development evidence rather
than a product promotion or a leaderboard-ready agent result.

The earlier five-task matched grep extension reaches **53.47%** delivered line coverage with grep
plus graph and decisions, versus **48.91%** for grep plus decisions and **43.61%**
for the initial BM25 plus decisions control. These are five-task micro scores,
not a representative benchmark or evidence for removing dense retrieval.
The frontier planner control improves the grep/graph candidate ceiling to
**77.74%** with Opus 5.5 and **70.80%** with GPT-6 Astra, but delivers only
**52.92%** and **50.18%** after the same decision reranker and top-five packing.
Stronger planning helps discovery in this sample; its gains do not carry through
the original top-five selection stage. Offline replay below raises delivered
coverage to **70.80%** and **66.61%** with the same scores and 4096-token budget
after removing the five-chunk cap, at the cost of substantially lower precision.

## Question and mechanism

Can lexical seed discovery, semantic graph expansion, and a small decision model
serve useful code context without a dense embedding index? This replaces the
dense candidate-generation stage; it remains retrieval-augmented generation.

```text
issue -> planned grep (or BM25 control) -> seed chunks -> definition identities
      -> bounded 1/2-hop reference + symbol-containment traversal
      -> up to 40 candidates -> Perplexity relevance decisions
      -> deduplicate source lines, top-5 / 4096-token context
      -> the same OpenRouter answer model
```

The decision model scores the candidates it receives. It does not discover
unseen graph nodes, build a corpus index, or guarantee that a missing seed can
be recovered. The [model card](https://huggingface.co/perplexity-ai/pplx-decider-v1.1-27b)
uses "Decision Index" for an evaluation benchmark, not an ANN index exposed by
this model.

Perplexity `pplx-decider-v1.1-27b` accepts the existing typed Decisions contract
(`state`, `questions`, probability distributions and expected scale scores).
The existing `OpenRouterDecisions` client and `RerankAgent` work without changes.
The [official model page](https://openrouter.ai/perplexity/pplx-decider-v1.1-27b)
and [Decisions guide](https://openrouter.ai/docs/guides/community/multimodal-decisions)
describe this interface. The real preflight returned
`perplexity/pplx-decider-v1.1-27b-20261006`, 124 input tokens, one output token,
$0.00000248, and 0.459 s. This tiny request is not the retrieval latency.

## Frozen pilot

Use ContextBench **verified**, pinned dataset revision
`c2855792b006af41c67202d33883fb9d46362853`, with the official evaluator at
`1436c28a8eb95496da4ea69ad458b9f8a8eb7d61`.

Before scoring, select up to two tasks in ascending curated-ID order from each
of `psf/requests`, `pallets/flask`, and `pytest-dev/pytest`. These small Python
repositories make this an inexpensive feasibility check. This is a convenience
sample, not a random or representative ContextBench result. All five eligible
selected tasks stay in the denominator, including failures.

| Original task | Base commit |
| --- | --- |
| `psf__requests-1142` | `22623bd8c265b78b161542663ee980738441c307` |
| `psf__requests-6028` | `0192aac24123735b3eaf9b08df46429bb770c283` |
| `pallets__flask-5014` | `7ee9ceb71e868944a46e1ff00b506772a53a4f1d` |
| `pytest-dev__pytest-7432` | `e6e300e729dd33956e5448d8be9a0b1540b4e53a` |
| `pytest-dev__pytest-7324` | `19ad5889353c7f5f2b65cc2acd346b7a9e95dfcd` |

Private clean checkouts are audited before and after preparation and inference.
SCIP graphs use the existing active Python backend, with references. The five
original clone/index/decode builds took 14.09–41.37 s each; later preparation
loads those cached graphs. Index construction is not counted as online query
latency, but it remains a real cold-start cost.

All arms share native tree-sitter chunks (depth 2, at most 100 lines) over tracked
Python sources selected by the production path filters. The default chunker
filters recognized test files; remaining fixtures/examples can still be indexed.
The graph walk does not impose an additional test-file filter. **The chunker's
synthetic headers are not source:** the experiment reads each original file at the declared source span,
then clips to complete lines within 3000 characters. This identical visible
code is used for BM25, dense documents, decisions and final context. No unseen
suffix receives coverage credit. Every prepared chunk was checked against the
actual source lines after newline normalization. BM25 also indexes the normal
`file:symbol` identity; dense documents use only the visible source text.

Chunk identities bind to graph definitions by display identity and overlapping
span, then by innermost enclosing scope. Split chunks can share a definition;
methods take precedence over their containing classes. Unmapped chunks remain
in lexical/dense controls and can still be seeds, but cannot start graph walks.

Expansion walks reference edges in both directions and containment edges only
between definition-backed symbols. It cannot fan out through file/directory
hubs or undefined external symbols. Bounds are 10,000 inspected raw edges,
512 visited symbols and depth 1 or 2. Within a depth, BM25 order breaks ties.
Candidate caps, reachable counts and retained depths are saved per task. This
pilot does **not** use a learned frontier gate, PPR, or dynamic stopping. The
initial seven arms use BM25 seeds; the extension below adds planned grep seeds.

| Arm | Candidate generation | Final order |
| --- | --- | --- |
| BM25 | top 40 | lexical |
| BM25 + decision | same top 40 | decision relevance |
| 1-hop + decision | five BM25 seeds + bounded 1-hop neighbors, cap 40 | decision relevance |
| 2-hop + decision | same seeds + bounded 2-hop neighbors, cap 40 | decision relevance |
| BM25 + graph + decision | lexical top 20, then distinct 2-hop candidates; lexical backfill to 40 | decision relevance |
| Dense | top 40 cosine similarities | dense similarity |
| Dense + decision | same dense top 40 | decision relevance |

Forty is a **cap**, not a guarantee that a disconnected graph supplies forty
candidates. Dense uses local `nomic-ai/CodeRankEmbed` at immutable revision
`3c4b60807d71f79b43f3c4363786d9493691f8b1`, normalized float32 vectors, exact inner
product search, query prompt from the model, max sequence length 2048, batch 4,
on the shared H100. This is a controlled same-visible-text baseline, not an
exact reproduction of the 100-candidate/8192-token Jev dense experiment.

Decisions reuse the existing four-level relevance rubric in batches of ten.
Application code sorts expected rubric score / 3; this is not model confidence.
Calls run serially without retries. Any API failure, incomplete batch, or missing
cost invalidates the arm; the product reranker's fallback is never counted as
successful model scoring. The cost flag bounds cumulative **decision** spend
between calls, not the final in-flight call or answer-model spend.

Final context uses at most five nonempty chunks and 4096 `cl100k_base` tokens,
including JSON framing, paths and line numbers. Duplicate source lines are
removed; clipping happens at line boundaries. Each arm sends exactly that text
to `openai/gpt-4.1-mini` on OpenRouter with temperature 0 and max output 600.
No tools, further retrieval, reference patches, gold spans or separate benchmark
hints are supplied; the original issue text is preserved.
These diagnoses demonstrate context consumption; they are not applied patches,
test execution, SWE-bench resolution, or a judged answer-quality comparison.

## Initial BM25/dense controls

All 35 arms (5 tasks × 7 routes) completed, with 85 successful decision requests
covering 815 candidate questions and 35 successful answer-model requests.
The official evaluator scored every task in every arm. The union of annotations
contains 548 gold lines and 41 extracted symbols across the five tasks.

Final coverage and precision below use the official **micro** aggregation
(sum intersections / sum denominators). Macro coverage is also shown so the
larger tasks cannot hide differences between task-weighted and line-weighted
results. Values are percentages; no confidence or significance claim is made.

| Route | Line coverage, micro | Line precision, micro | Line coverage, macro | Context-serving stage sum, median s |
| --- | ---: | ---: | ---: | ---: |
| BM25 | 22.26 | 16.29 | 23.95 | 0.11 |
| BM25 + decision | 43.61 | 32.30 | 43.60 | 2.84 |
| 1-hop + decision | 26.64 | 18.89 | 28.17 | 0.63 |
| 2-hop + decision | 41.97 | 34.53 | 38.08 | 2.63 |
| BM25 + graph + decision | **45.99** | 33.87 | **45.10** | 2.64 |
| Dense | 7.66 | 8.66 | 8.30 | 0.05 |
| Dense + decision | 32.85 | 26.95 | 37.69 | 2.44 |

The latency column adds retrieval measured during preparation to decision and
context-packing time measured during inference; it excludes answer generation
and is **not** an end-to-end wall measurement. Median decision time alone was
2.78 s for BM25, 2.56 s for 2-hop, 2.53 s for the union, and 2.26 s for dense.
One-hop was faster (0.51 s) with only 15 candidates on average, and substantially
lower coverage. Two-hop averaged 28 candidates; lexical, union and dense had 40.
Mean delivered context size ranged from 1150 to 1864 tokens across routes,
below the common 4096-token cap. Final answer medians were 2.89–4.07 s by route.

| Candidate pool, before selection | Gold-line coverage ceiling |
| --- | ---: |
| BM25 top 40 | 46.17% |
| 1-hop, cap 40 | 29.20% |
| 2-hop, cap 40 | 44.71% |
| BM25 + graph, cap 40 | 51.64% |
| Dense top 40 | 42.88% |

The union adds 30 gold lines to the candidate pool compared with BM25
(253 → 283), of which 13 become additional delivered lines (239 → 252).
Compared with BM25 + decision it wins on two tasks and ties on three, yielding
**+2.37 percentage points micro coverage**, +1.50 points macro coverage, and
+1.57 points micro precision. This is a useful pilot signal, not a general win.

The failure cases are equally informative:

- On `psf__requests-6028`, pure 2-hop covers none of the 94 gold lines, while
  BM25 + decision and the union cover 47. The graph has only seven candidate
  chunks after expansion. Reranking cannot repair this candidate-recall gap.
- On `pallets__flask-5014`, dense + decision covers 35 of 39 gold lines; all
  lexical/graph delivered contexts cover zero. Removing dense unconditionally
  would lose a success in this very small sample.
- On `pytest-dev__pytest-7324`, pure 2-hop covers 43 of 168 lines versus 11 for
  BM25 + decision and 18 for the union. Fixed pool allocation can discard useful
  graph evidence even when the graph contains it.

Decision usage was 2,558,988 input tokens and **$0.05117976**; final answers used
66,603 input and 16,895 output tokens, costing **$0.053174**. Total formal API
cost was **$0.10435376**, plus the $0.00000248 preflight. Provider-reported usage
is retained per call; batching is not assumed to amortize shared input billing.
Seven answers reached the 600-token output cap, and 28 finished normally. No
answer-quality or task-resolution score is reported. GPU/indexing costs are
not included in API dollars. Dense document encoding took 1.94–15.74 s per task,
separate from model loading; graph cold builds are described above.

This first comparison omitted the existing model-planned grep retriever, so its
best no-embedding route was only the best among the seven listed arms. The grep
extension isolates this missing seed-selection control. Neither comparison
justifies a product default or a general replacement for dense retrieval.

## Grep seed and search-scope controls

The extension reuses `benchmark_model_grep.py`, the previous Jev experiment's
planner, ripgrep executor, smallest-visible-chunk mapping and round-robin action
merge. Sonnet 4.6 plans at most six regex/glob actions from the issue and a
directory-count overview. No source contents, labels, retrieved hits or graph
identities enter this one-shot plan. The five plans are frozen once and reused
across every extension arm; no score-driven query repair or BM25 refill occurs.

The original source chunks, task hashes, graph files, BM25 neighbor priorities,
40-candidate cap, decision rubric and final 5-chunk/4096-token context budget
remain fixed. Preparation verifies that the old BM25 and BM25-seeded 2-hop pools
are reproduced exactly before adding the grep controls. This is the previous
grep mechanism on ContextBench's frozen corpus, not a reproduction or pooled
score of the larger Jev experiment.

| Extension arm | Candidate generation |
| --- | --- |
| Grep | planned globs, up to 40 chunks, original round-robin order |
| Grep + decision | the same pool, decision selection |
| Wide grep + decision | the same regexes, every glob replaced with `**/*` over the same eligible corpus |
| Grep seeds 1/5/10/20 + 2-hop + decision | prefix of the original grep order as seeds, unchanged traversal and BM25 priority within each depth, cap 40 |
| Grep + graph + decision | first 20 grep chunks, then the 5-seed 2-hop pool, then remaining grep chunks; deduplicate and cap at 40 |

Seed counts are upper bounds when grep returns fewer chunks. They count chunks,
not unique graph symbols. The scope control only changes globs; it neither adds
unindexed files nor replans the query. Ripgrep retains the existing 20-match
limit per file and 500-match-line limit per action. The original planner's
100-candidate contract is preserved during search: first 40 enter inference,
while all available candidates up to 100 are retained only for ceiling audits.
Seed-only prefixes are also scored separately. These audit pools do not feed
the answer model and are not delivered-context results.

Planner time and cost are counted even when saved plans are reused. Graph arms
also include BM25 neighbor-priority search in the retrieval stage sum. Shared
graph/index construction and immutable snapshot materialization remain offline
preparation costs, as in the original controls.

### Matched extension results

All **40 routes** (8 arms × 5 tasks) completed without API or scoring failures.
The scorer uses the same compatible isolated interpreter and unchanged official
formulas as the corrected initial controls. All 21 delivered/pool reports include
all five tasks, with 548 gold lines and 41 gold symbols. The audit verified every
one of the 4924 frozen chunks and 40 delivered contexts against original source,
unique line numbers, the token cap, and exported scoring spans.

| Route | Line coverage, micro | Line precision, micro | Line coverage, macro | Context-serving stage sum, median s |
| --- | ---: | ---: | ---: | ---: |
| Grep | 38.50 | 32.12 | 51.82 | 3.38 |
| Grep + decision | 48.91 | 42.27 | 60.95 | 5.18 |
| Wide grep + decision | 47.99 | 38.12 | 60.47 | 5.03 |
| Grep seed 1 + 2-hop + decision | 38.50 | 26.91 | 42.75 | 5.08 |
| Grep seeds 5 + 2-hop + decision | 48.72 | 40.52 | 58.28 | 5.71 |
| Grep seeds 10 + 2-hop + decision | 48.72 | 37.93 | 58.28 | 5.61 |
| Grep seeds 20 + 2-hop + decision | **53.47** | **43.67** | **63.81** | 5.69 |
| Grep + graph + decision | **53.47** | **43.67** | **63.81** | 6.31 |

Grep plus decisions delivers 268 gold lines versus BM25 plus decisions' 239:
**+5.29 percentage points** micro coverage. The grep/graph union delivers 293,
adding 25 lines over grep plus decisions: **+4.56 points**, with two task wins
and three ties. Against the initial BM25/graph union's 252 lines, it gains 41
lines (+7.48 points), with two wins, one loss and two ties. Initial controls
reuse their saved outputs; they were not rerun in the extension. Latency is
still a stage sum from separate preparation/inference measurements, not a paired
end-to-end timing study.

| Task | Gold lines | BM25 + decision | Grep + decision | Grep seeds 5 + 2-hop + decision | Grep + graph + decision |
| --- | ---: | ---: | ---: | ---: | ---: |
| requests-1142 | 67 | 65 | 65 | 65 | 65 |
| requests-6028 | 94 | 47 | 47 | 21 | 47 |
| flask-5014 | 39 | 0 | 35 | 35 | 35 |
| pytest-7432 | 180 | 116 | 101 | 116 | 116 |
| pytest-7324 | 168 | 11 | 20 | 30 | 30 |

The Flask case that only dense retrieved in the initial comparison is recovered
by planned grep: 35/39 lines. The proxy case exposes a different failure: five
grep roots expand to a pool covering 70/94 lines, yet decision selection delivers
only 21. The union keeps the direct grep candidates and delivers 47. Better
candidate recall alone does not guarantee better final selection. On
pytest-7324, a single grep root delivers 59 lines, more than the union's 30;
there is no uniformly winning seed count across tasks.

| Requested grep seeds | Seed-only line coverage | After 2-hop, cap 40 | Delivered after decision |
| --- | ---: | ---: | ---: |
| 1 | 14.78% | 43.25% | 38.50% |
| 5 | 38.50% | 64.42% | 48.72% |
| 10 | 45.26% | 64.42% | 48.72% |
| 20 | 47.26% | 60.22% | 53.47% |

More roots consume more of the fixed 40-candidate budget and change which
neighbors survive. The pool ceiling falls at 20 roots even though delivered
coverage improves. Counts also saturate: requests-1142 has only 5 grep chunks,
Flask 4, and pytest-7432 6. At 20 requested roots the other two tasks have 20
seed chunks but only 19 distinct graph symbols. Each arm is a separate decision
call, including arms whose candidate pools coincide; no best-of-retries selection
is performed.

Widening globs increases Flask's pool from 4 to 16 chunks and pytest-7432's from
6 to 17, without improving their pool coverage. Overall the grep pool ceiling
falls from 284/548 (51.82%) to 277/548 (50.55%), and final coverage falls from
268 to 263 lines. One broad pytest action reaches the 500-line cap; a wider
bounded search need not retain a superset of the original hits. The available
100-candidate audits do not recover additional gold lines beyond the respective
40-candidate pools in this sample. The grep/graph union ceiling is 353/548
(64.42%). Search scope, graph reachability and final selection are distinct
constraints.

The five Sonnet plans cost **$0.025494** and took a median **3.29 s**. All were
successful on their first attempt; plans were paid once, saved, and reused across
arms. There were 103 successful decision requests for 958 questions, consuming
3,044,460 input tokens and **$0.0608892**. Forty answer requests consumed 74,136
input and 18,279 output tokens, costing **$0.0516816**. Total extension API cost
was **$0.1380648**, excluding prior controls and local compute. Twelve answers
reached the 600-token cap; 28 stopped normally. These remain context-consumption
smokes, not measured fixes or answer-quality scores.

**Current decision:** use the existing planned-grep mechanism as a primary seed
control for further graph experiments, and preserve direct hits while testing
bounded neighbor expansion. The initial BM25-only seed comparison was
incomplete. Keep this implementation in labs; neither five tasks nor this seed
sweep establishes an optimal production policy or justifies eliminating dense
retrieval. A larger frozen comparison must test seed diversity and selection,
candidate dilution, graph-missing cases and actual downstream task outcomes.

### Frontier planner control

Replace only the query planner with OpenRouter
[`anthropic/claude-opus-5.5`](https://openrouter.ai/anthropic/claude-opus-5.5) or
[`openai/gpt-6-astra`](https://openrouter.ai/openai/gpt-6-astra). Both IDs were
listed in the live model catalog and returned by the actual successful calls.
The saved Sonnet 4.6 results remain the control; no baseline calls are repeated.
The same five issues, directory overviews, system prompt, structured action
schema, eligible files, graphs, candidate cap and source chunks are reused.
Each planner still produces a single plan of at most six regex/glob actions;
neither gets search results, graph information or gold annotations as input.

Both frontier requests specify `reasoning.effort=medium`, omit temperature,
and allow 4096 output tokens, including reasoning. The original Sonnet request
disables reasoning, uses temperature zero, and allows 1000 output tokens.
[GPT-6 Astra's parameter guidance](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6-astra)
does not support disabled reasoning or temperature. Medium effort and the
4096-token output allowance are explicit experiment choices. This compares
usable model/configuration pairs, not model weights at equal compute. The
provider reports zero reasoning
tokens for Opus and 812 across the five Astra calls; the requested effort does
not establish equal realized reasoning. Requested and returned model IDs,
request parameters, usage and attempts are retained in the plan receipts.

Run only `grep_decision` and `grep_union_decision` for each new planner. Keep
`perplexity/pplx-decider-v1.1-27b`, the relevance rubric, five-chunk/4096-token
context, and `openai/gpt-4.1-mini` answer smoke unchanged. This isolates planning
from deliberate reranker or answer-model upgrades, although independent calls
still introduce provider/model variance. The final answer model cannot change
the context-coverage score: that score uses the exact context it received.

All ten plans succeeded on their first attempt and stopped normally. All twenty
new inference rows and twenty delivered/pool scoring reports completed without
failures, with all five tasks in every denominator. The source/receipt audit
checks all 4924 frozen chunks and twenty delivered contexts, the unchanged
baseline hashes, and identical planner inputs and prompts.

Values below are line recall percentages. Pool columns score all candidates
before selection, and are ceilings rather than delivered-context scores.

| Planner | Grep pool, micro | Grep delivered, micro | Grep + graph pool, micro | Grep + graph delivered, micro | Grep + graph delivered, macro |
| --- | ---: | ---: | ---: | ---: | ---: |
| Sonnet 4.6, saved control | 51.82 | 48.91 | 64.42 | **53.47** | **63.81** |
| Opus 5.5, medium reasoning requested | 60.22 | 50.73 | **77.74** | 52.92 | 60.97 |
| GPT-6 Astra, medium reasoning requested | **65.33** | **51.82** | 70.80 | 50.18 | 59.23 |

Opus adds 73 gold lines to the union candidate pool (353 → 426), and Astra adds
35 (353 → 388). Delivered union coverage instead loses 3 and 18 lines against
Sonnet. Opus wins on two tasks, loses on one and ties on two; Astra wins on one,
loses on one and ties on three. Delivered union micro precision is 46.03% for
Opus and 45.23% for Astra, versus 43.67% for Sonnet. Plain grep plus decisions
gains 10 and 16 delivered gold lines, but both frontier models have lower macro
recall than Sonnet. There is no consistent model winner on these five tasks.

The per-task union results separate discovery from delivery. Each cell shows
**gold lines in the candidate pool → gold lines in final context**.

| Task | Gold lines | Sonnet 4.6 | Opus 5.5 | GPT-6 Astra |
| --- | ---: | ---: | ---: | ---: |
| requests-1142 | 67 | 65 → 65 | 65 → 65 | 65 → 65 |
| requests-6028 | 94 | 70 → 47 | 85 → 21 | 70 → 21 |
| flask-5014 | 39 | 35 → 35 | 35 → 35 | 35 → 35 |
| pytest-7432 | 180 | 153 → 116 | 153 → 122 | 130 → 116 |
| pytest-7324 | 168 | 30 → 30 | 88 → 47 | 88 → 38 |

The proxy case is the clearest selection failure: Opus finds a pool containing
85/94 gold lines, but only 21 survive in the delivered context. Both frontier
planners also reach substantially more of the expression-parser case through
the graph. Across all twenty contexts, the five-chunk limit is reached while
the 4096-token budget is not. Union contexts average 1479 tokens for Opus and
1423 for Astra. These observations motivate separate controls for stronger
candidate selection and token-budget packing; they do not distinguish those
two effects, establish that the decision model alone caused the losses, or
prove a frontier reranker will fix them. The live comparison did not change the
reranker or packing policy; the offline packing diagnosis follows below.

| Planner | Five planning calls, USD | Planning latency, median s | Decision calls / questions | Entire new run, USD |
| --- | ---: | ---: | ---: | ---: |
| Opus 5.5 | 0.047752 | 4.71 | 24 / 212 | 0.07447184 |
| GPT-6 Astra | 0.104130 | 6.86 | 24 / 218 | 0.13020400 |

The new API spend totals **$0.20467584**, including ten plans, 48 decision calls
and twenty answer smokes. It excludes the saved Sonnet control and local
compute. Four answer smokes reached their unchanged 600-token cap; sixteen
stopped normally. These outputs are still not judged fixes. The union serving
stage sums are 7.30 s and 8.80 s for Opus and Astra, versus the saved Sonnet
measurement of 6.31 s; these are neither paired end-to-end measurements nor
latency significance claims.

**Decision after the frontier control:** preserve frontier planners as explicit
experiment options, with their request settings and cost recorded. Do not
promote a new default based on these five Python tasks. The next useful
comparison holds candidate pools fixed and tests selection and packing before
attributing the remaining gap to planner strength. The offline diagnosis below
completes that packing control. Multi-round search, frontier reranking and
answer-quality evaluation remain separate, untested hypotheses.

### Offline diagnosis: where the additional context is lost

Replay both routes for all three planners using the saved decision responses.
All **30** original contexts, prediction spans, normalized scores, node IDs and
rankings reproduce exactly. The probability-weighted rubric score agrees with
the provider score. There is no observed score-to-node mismatch, reversed sort,
line-number conversion error, failed-call fallback, or accidental context loss
in this sample. The loss comes from the selected policy and available corpus.
No new model or answer calls are made during this diagnosis.

Change only packing, preserving each saved ranking. Compare the original
five-chunk cap, ten chunks, and all available candidates under the same
4096-token cap. A fourth control skips reranking and packs the original
candidate order within 4096 tokens. Gold labels are used only after each of
these contexts has been constructed. The separate gold-aware oracle described
below is explicitly a diagnostic upper bound.

| Planner, grep + graph | Original top-5 recall | Top-10 recall | Token-budget-only recall | Original candidate order, token-budget-only recall |
| --- | ---: | ---: | ---: | ---: |
| Sonnet 4.6 | 53.47% | 60.22% | 60.22% | 56.75% |
| Opus 5.5 | 52.92% | 70.07% | 70.80% | 74.64% |
| GPT-6 Astra | 50.18% | 65.88% | 66.61% | 70.80% |

These are five-task **micro line recall** results from offline repacking, not
new answer-model runs. Official scoring agrees with the independent line-set
calculation for all **150 contexts in 30 reports**, including the replay,
packing controls and explicitly labeled oracle. Every report includes all five
tasks, 548 gold lines and 41 gold symbols.

**1. The hard chunk count cuts off useful context while tokens remain.**
`pack_context` stops when five nonempty chunks have been appended, even when
thousands of tokens remain. Chunks vary in length; a two-line constructor
consumes the same slot as a long method. For `pytest-7324`, the Opus union
delivers 47 gold lines in five chunks and only 628 tokens. Its entire 16-node
pool fits in 2441 tokens and contains 88 gold lines. Astra delivers 38 lines
in 520 tokens; all 26 candidates fit in 3810 tokens and cover the same 88.
The frontier planners' additional parser neighbors therefore do not need a
larger token budget to reach the context.

Removing the count cap adds 98 delivered gold lines for Opus and 90 for Astra.
It also adds unrelated or unannotated lines. Union micro precision falls from
46.03% to 24.46% for Opus and from 45.23% to 21.83% for Astra; macro F1 falls
from 46.36% to 35.68% and from 44.85% to 31.97%, respectively. Mean context
length grows from 1479 to 3759 tokens and from 1423 to 4030 tokens. This
demonstrates a recall bottleneck, not an overall quality win or evidence that
filling every remaining token is the best production policy.

**2. Independent relevance scoring can displace useful dependencies.**
The four-level rubric scores each code chunk against the issue; it does not
explicitly optimize the coverage or complementarity of the selected group.
In `requests-6028`, the frontier pools contain relevant URL-parsing helpers,
but the reranker promotes several proxy/authentication wrappers ahead of them.
Those wrappers can be useful even when they are outside the annotated gold;
zero gold overlap alone is not proof that code is irrelevant to an answer.

| Candidate in the proxy issue | Sonnet-run rank / score | Opus-run rank / score | Astra-run rank / score |
| --- | ---: | ---: | ---: |
| `HTTPAdapter.get_connection` | 5 / 0.6897 | 7 / 0.6588 | 8 / 0.6681 |
| `get_auth_from_url` | 9 / 0.6353 | 5 / 0.7317 | 5 / 0.7341 |
| `prepend_scheme_if_needed` | 21 / 0.3288 | 30 / 0.2495 | 21 / 0.5098 |
| `urldefragauth` | absent | 19 / 0.5135 | absent |

`get_connection` contains 26 gold lines, whereas `get_auth_from_url` has none
in this annotation. Its displacement alone explains the drop from 47 to 21
delivered gold lines in both frontier top-five results. The newly retrieved
`prepend_scheme_if_needed` and `urldefragauth` account for the remaining 38
Opus-pool gold lines that still fall outside the ranked 4096-token context.
Original candidate-order packing retains 68 of this task's gold lines versus
47 with decision ordering, explaining the 21-line aggregate difference between
the Opus token-budget controls. It does not mean unreranked order is universally
better: the same control loses recall on the Sonnet pools.

The reranker model ID is unchanged, but its candidate batches and node IDs
change with the planner. Shared nodes also receive different scores in separate
calls. An offline diagnostic that substitutes the old Sonnet-call scores for
shared nodes, while keeping each frontier candidate pool fixed, restores
`get_connection` to the top five and recovers 47 gold lines on this task.
This is a mixed-call-score counterfactual, not a deployable ranking result.
It establishes that shared-node score changes are sufficient to cause the
observed loss; it does not separate batch-context sensitivity from model or
provider variance. No controlled repeated scoring experiment was run.

To separate selection quality from capacity, enumerate the maximum gold-line
union achievable with at most five candidates, then verify an actual packing
that reaches that bound within 4096 tokens. All bounds are feasible in this
sample. Opus's union ceiling is 384 gold lines (70.07%) at five chunks per task,
compared with its actual 290 (52.92%) and uncapped pool's 426. Thus even at
five chunks, better selection could theoretically retain 94 additional gold
lines; the count cap necessarily excludes at least 42 pool lines. On the proxy
task, four candidates and 1040 tokens suffice to retain all 85 pool gold lines.
These oracle selections use the annotations and **must not be reported as real
retrieval quality or fed to the answer model**.

**3. The frozen corpus sets a separate ceiling.**
The entire visible corpus contains only **434/548 gold lines (79.20%)**:
65/67 for requests-1142, 85/94 for requests-6028, 35/39 for Flask, 153/180 for
pytest-7432 and 96/168 for pytest-7324. Rebuilding the native chunk spans from
the same commits identifies 106 omitted gold lines outside those spans and
eight in a filtered test file. Of the 106 span omissions, 50 are blank lines
and 56 are nonempty source lines, including class declarations, enum values,
module constants and docstrings. The 3000-character clipping step adds no
gold-line omissions in this sample. Opus's union pool already reaches 426/434
gold lines available in this corpus, so another planner upgrade alone has
little room to raise this particular pool ceiling. Changing chunk coverage or
test-file policy requires a new frozen experiment for every arm.

**Current diagnosis and next control:** keep the historical top-five results
intact and expose the candidate-count cap explicitly in subsequent comparisons.
The existing `run --top-k 40 --context-tokens 4096` configuration removes that
cap for this 40-candidate experiment; no product default or packing algorithm
was changed. Evaluate budget-aware, complementary context selection on the
same pools before choosing a new default, and measure downstream answer quality
as well as recall/precision. Frontier reranking remains untested.

Receipts are in `verification/graph-context-debug/`: `node-traces.json`
records every ranked chunk and its gold-line loss; `results.json` and
`official/` retain the exact repacked contexts and official metrics;
`score-counterfactuals.json` records shared-node score changes;
`corpus-omissions.json` classifies every unavailable gold line. Reproduce from
the checkout root without model calls:

```bash
PYTHONPATH=. python verification/graph-context-debug/diagnose.py
PYTHONPATH=. python verification/graph-context-debug/causes.py
PYTHONPATH=. python verification/graph-context-debug/score.py
```

### Scorer compatibility correction

The pinned upstream extractor passes bytes to `parser.parse`. The installed
`tree-sitter-language-pack==1.8.1` instead expects a string; upstream catches that
exception and silently returns empty symbol sets, which its metric treats as
perfect. An actual function-extraction preflight now rejects this environment.
The authoritative scoring uses isolated Python 3.11.5,
`tree-sitter==0.20.4`, and `tree-sitter-languages==1.10.2`, exactly matching the
official requirements for Python 3.11. Upstream source and metric formulas are
unchanged. All file/line/span metrics match the previous pass; symbol metrics
now have nonempty denominators. The invalid pass is marked and retained under
`scoring-incompatible-parser/` only for diagnosis. The earlier single-case
ContextBench smoke was also rescored without new model calls.

## Frozen multilingual serving cohort

The multilingual development cohort is **100 tasks from `contextbench_verified`**,
covering 56 repositories and all eight dataset languages. Selection excludes the
five previously inspected pilot tasks. Each language receives five slots, then
the remaining slots follow its share of eligible cases; within each language,
repositories are visited round-robin with SHA-256 ordering under seed
`context-serving-v1-2026-10-11`. No gold size, issue text, graph availability or
score participates in selection. This deliberately overrepresents the smaller
language groups and is not an unbiased estimate of the full 500-task mean.

| Language | Tasks |
| --- | ---: |
| Python | 37 |
| TypeScript | 13 |
| JavaScript | 12 |
| Go | 10 |
| C | 8 |
| Java | 7 |
| Rust | 7 |
| C++ | 6 |

The source-only `scripts/benchmark_context_serving.py` reuses the pilot's grep
planner/executor, graph traversal, typed decision reranker and source-exact
packing. This is a new corpus: all tracked regular registered source files
surviving the existing vendor/generated/minified/size filters are eligible,
including tests. Visible native AST chunks retain their 3000-character limit;
bounded intervals cover source lines omitted between those chunks or after a
clipped chunk. Individual lines over the character cap are recorded as omitted.
The change is uniform across all cases and arms and does not use gold context.
This source-only policy does not change product defaults.

Graph construction uses the active primary-language backend on a private copy,
without project preparation, with a 120-second worker wall limit. The authenticated
retrieval checkout stays clean. Unavailable graphs produce an explicit empty
graph and a labeled grep fallback; failed preparation is retained in the cohort.
Cold construction and fallback frequency must accompany retrieval scores.
This run does not promote a SCIP/LSP backend or establish full cross-language
graph availability. The unrelated open PR #826 addresses JavaScript read-only
SCIP onboarding; its changes are not incorporated into this checkout's run.

Compare Sonnet 4.6 and Opus 5.5 planners, each with grep and grep+graph pools.
Use the same 40-candidate cap, five graph seeds, two hops and pplx decision model.
The primary packing is fixed at **top-10 / 4096 tokens** before evaluating this
cohort. Top-5 and token-budget-only are secondary offline controls using the same
saved ranking; do not select a winner post hoc and describe it as preregistered.
Calls for the two routes alternate order by task-ID hash. Planner calls are
shared by the paired routes. There are no solver or diagnosis calls.

Preparation receipts and exact task selection live in
`verification/context-serving-100-v3/`. Preparatory attempts in
`verification/context-serving-100/`, `context-serving-100-v1/` and
`context-serving-100-v2/` were stopped before paid inference to correct a
non-portable generation keyword, install the missing Go/Java SCIP tools, and
repair cancellation of Python SCIP's nested process session. Clean downloaded
snapshots are reused; all logs remain. The final preparation uses six spawned
processes and builds graphs in external temporary directories so ancestor
CodeNib project configuration cannot affect indexing. A subprocess regression
verifies that cancellation reaps the nested indexer before workspace cleanup.
All attempts preserve identical task IDs; retries were for implementation and
environment preparation, not quality-based sample selection.

The paid run exposed a validation mismatch on blank source-gap chunks. The
product reranker correctly sends only nonblank chunks for decisions, then appends
unscored blank chunks in their original order. The experiment incorrectly expected
one billed question for every candidate. The corrected check uses the nonblank
count while still requiring every candidate in the returned ranking and rejecting
API errors or missing cost. A deterministic regression reproduces this boundary.
The completed batch retains its original implementation and raw failures in
`verification/context-serving-100-live/`; archived source hashes are unchanged.
An offline replay in `verification/context-serving-100-replayed/` reconstructs
rankings from the saved typed responses, verifies previously successful contexts
byte-for-byte, and recovers only this named validation failure. It makes no new
API calls and does not change sampling, prompts, scores, sorting or packing.
Recovered routes have no measured original whole-rerank duration, so they are
excluded from serving latency summaries rather than assigned offline timings.

Use the managed toolchain exports printed by `make active-scip-env` before
preparation. This machine needed `make scip-go-tool scip-java-tool`; resolved
executable paths and reused checkouts are recorded in `environment.json`.
The graph worker allows ten seconds for cancellation cleanup after its indexing
wall limit; receipts include clone/load/cleanup overhead in the measured build
time. Graph construction, source parsing and API inference have separate timings.

```bash
python scripts/benchmark_context_serving.py select \
  --dataset /tmp/codenib-contextbench-upstream/data/contextbench_verified.parquet \
  --exclude verification/graph-decisions/selected.jsonl \
  --output verification/context-serving-100-v3 --size 100
python scripts/benchmark_context_serving.py prepare \
  --cohort verification/context-serving-100-v3 --workers 6 --graph-timeout 120
python scripts/benchmark_context_serving.py run \
  --cohort verification/context-serving-100-v3 \
  --output verification/context-serving-100-live --workers 4 --live
python scripts/benchmark_context_serving.py evaluate \
  --cohort verification/context-serving-100-v3 \
  --run verification/context-serving-100-replayed \
  --upstream /tmp/codenib-contextbench-upstream \
  --evaluator-python /tmp/codenib-contextbench-evaluator-venv/bin/python
```

For this historical run, execute
`PYTHONPATH=. python verification/context-serving-100-v3/replay.py` after inference
and before scoring. A new experiment uses a fresh cohort directory to freeze the
corrected source. Paid preparation/inference reject implementation drift;
read-only scoring verifies saved selection/preparation provenance without requiring
the current serving implementation to equal an archived version.

The default guards check observed spend before calls: $3 per planner model and
$0.02 of decisions per task/model. Concurrent in-flight calls and the last call
can exceed those thresholds. Retain missing-cost/error receipts; do not report
an incomplete provider bill as a known total. A fresh run directory is required,
so no retry silently replaces a failed attempt. Retrieval failures with an
available checkout are exported as empty context. Upstream returns
`no_context_extracted` for empty context and omits it from its valid-score mean.
Preserve those official reports and additionally report Recall/F1 over all 100
selected tasks, assigning zero to empty or unavailable results. This gives a
conservative population bound for preparation failures and prevents an empty
retrieval from improving the mean by disappearing. Paired comparisons use that
same retained population; common-valid-case comparisons are secondary diagnostics.

### Primary quality result

The predeclared top-10 / 4096-token comparison gives the following **per-task
macro Recall/F1 over all 100 selected tasks**, with empty and unavailable results
assigned zero. These conservative population values are additional development
statistics, not a replacement for the original official reports. F1 is derived
per instance from upstream coverage and precision before averaging.

| Planner | Route | Line Recall, % | Line F1, % | Serving API, USD / selected task |
| --- | --- | ---: | ---: | ---: |
| Sonnet 4.6 | grep + decisions | 41.98 | 25.40 | 0.011568 |
| Sonnet 4.6 | grep + graph + decisions | 42.66 | 25.02 | 0.011945 |
| Opus 5.5 | grep + decisions | 45.70 | 27.84 | 0.019899 |
| Opus 5.5 | grep + graph + decisions | **47.64** | **28.95** | 0.020451 |

Graph expansion adds 0.68 Recall points but loses 0.38 F1 points with Sonnet;
with Opus it adds 1.95 Recall points and 1.10 F1 points. The cost is low, while
the quality benefit depends on the planner and remains modest. This experiment
does not establish superiority to a dense/hybrid retriever or an end-to-end
agent: neither is a matched control in this 100-task cohort.

A zero-API ablation packs the same frozen grep pools in their original order,
retaining top-10, 4096 tokens and the same 100-task failure accounting. Original
order is the interleaved output of model-planned grep actions, not model-free
retrieval. Per-task line-set calculations reproduce all 388 valid official
top-10 scores across the two planners and two recorded routes before scoring
the new packing. No new official file/symbol evaluation is claimed.

| Planner | Original grep order, Recall / F1 % | Decision reranking, Recall / F1 % |
| --- | ---: | ---: |
| Sonnet 4.6 | 33.04 / 19.94 | 41.98 / 25.40 |
| Opus 5.5 | 39.88 / 22.60 | 45.70 / 27.84 |

Decision reranking improves Recall by 8.94/5.81 points and F1 by 5.46/5.24
points for Sonnet/Opus. Its observed contribution is larger than the fixed
graph expansion's contribution. This is evidence against dropping the decider
from this baseline, but does not compare it with another reranker. The script,
per-case contexts, input hashes and summary are retained under
`verification/context-serving-100-ablation/`.

Graph expansion is not a monotonic candidate union: it retains the first 20
grep candidates, inserts neighbors, then truncates the combined pool at 40.
It removes previously retrieved gold lines on nine Sonnet tasks and six Opus
tasks. Across the cohort, Sonnet's graph pools add 380 gold lines and lose 429;
Opus's add 804 and lose 212. Future graph controls should preserve core grep
evidence and distinguish candidate capacity from the final delivery budget.
These diagnostics do not alter the frozen runs or establish a corrected score.

Secondary graph-route packing controls reuse the same saved scores and model
charges. Each cell is population Recall / F1 in percent:

| Planner | Top-5 | Top-10, primary | Token budget only |
| --- | ---: | ---: | ---: |
| Sonnet 4.6 | 40.18 / 28.03 | 42.66 / 25.02 | 43.36 / 24.60 |
| Opus 5.5 | 41.26 / 29.98 | 47.64 / 28.95 | 49.09 / 27.98 |

More context increases Recall but reduces F1 here. The remaining gain from
removing the top-10 cap is small; these controls do not justify replacing the
predeclared primary result with a post-hoc winner.

The official evaluator has 96 valid cases for every Sonnet arm and 98 for every
Opus arm. Both models retain the two LangChain preparation failures. Sonnet also
has a zero-hit MUI task and a Flipt task with an unsupported multiline grep
pattern; both export empty context and receive `no_context_extracted` upstream.
These four cases remain zero in the Sonnet population statistics. All 12 official
reports and their original failure records remain available.

The blank-gap validation correction recovers 31 routes without API calls.
Replay verifies all 359 originally successful route rankings and 1,077 original
packed outputs unchanged. It retains the preparation and invalid-grep failures.

Only 48 of 100 selected tasks produced graphs under this cold-build policy;
50 prepared tasks use grep fallback, and two fail preparation. Python's 22
unavailable graphs hit the worker deadline. All 14 C/C++ builds lack a prepared
compilation database; Java has two deadlines and five other indexing failures,
including unresolved historical Maven dependencies. Five TS/JS builds and one
Rust build time out; one further JS build produces no graph. There are no graph
build processes left running after cleanup. These are limitations of this
bounded, unprepared benchmark setup, not backend promotion results.

The one-shot planner in this 100-task run never sees its search results. The
24-task follow-up above implements the resulting feedback-search hypothesis
and separates discovery from final context delivery. Typed graph navigation
with a learned frontier remains untested. Comparisons retain declared API,
round and context budgets and freeze policy changes on fresh manifests.
A leaderboard result additionally needs a
fixed solver and native issue-resolution outcomes. The 100 inspected development
IDs and the earlier five pilot IDs must be disclosed if reused in that run.

The source corpus covers **91.85%** of gold lines by task-weighted Recall over
the full selection. Opus's grep pool covers **51.23%**, its graph union **54.03%**,
and top-10 delivery **47.64%**. Discovery is therefore a larger remaining gap
than the final packing stage. Both planners receive truncated directory overviews
on 32 of 98 prepared tasks. Opus reaches the 40-candidate graph cap on 35 tasks
and the 512-symbol traversal cap on four; its graph pools average 1.67 blank gap
nodes, with a 95th-percentile count of 15. These motivate better seed discovery,
graph availability and candidate allocation before simply raising output top-k.
They are diagnostics, not improvements already demonstrated by this run.

On the 48 tasks with available graphs, Opus gains 4.06 Recall points and 2.30 F1
points. Across all 100 tasks its Recall improves on 16, ties on 81 and declines
on three; F1 improves on 18, ties on 67 and declines on 15. Identical ordered
pools reproduce identical final metrics, so the measured difference is confined
to cases where graph expansion changes candidates. These observations do not
establish an improvement in issue resolution.

For a matched reference, the
[published JiuwenSwarm raw outputs](https://github.com/ContextBench/contextbench.github.io/tree/main/submissions/2026-09-12_openjiuwen-jiuwenswarm)
have Recall/F1 **71.45%/25.74%** on these same 100 IDs. On the 98 cases valid for
Opus, the corresponding values are **72.41%/25.87%**, versus CodeNib's
**48.62%/29.54%**. The former measures a complete agent trajectory; the latter
is a single 4096-token context. Higher F1 with lower Recall in this different
context budget does not prove a better agent or transfer the published Pass@1.

Measured stage sums, in seconds, retain their original timing scope:

| Planner | Route | Median | p95, nearest rank | Timed cases |
| --- | --- | ---: | ---: | ---: |
| Sonnet | grep | 8.11 | 24.17 | 97 |
| Sonnet | graph union | 10.08 | 47.13 | 82 |
| Opus | grep | 8.91 | 31.07 | 98 |
| Opus | graph union | 11.28 | 36.44 | 82 |

These add planning, source materialization, grep, graph expansion where applicable,
reranking and packing under four-worker contention. They exclude initial graph
and BM25 load/build, and omit recovered routes without original whole-rerank
timing. They are not continuous end-to-end measurements. The separate graph
build stage has a 47.06-second median and 123.41-second p95 over 98 attempts,
including graph failures and clone/load/cleanup overhead. Low API cost does not
remove cold indexing or online CPU work; cached source/index/bindings and avoiding
full-corpus BM25 ordering on a graph fallback remain engineering work.

The completed audit checks 1,200 case/arm/policy rows, verifies 1,170 produced
contexts against physical source lines, unique line accounting and token caps,
and independently matches all 1,164 valid official line-metric records. The 36
unscorable rows remain explicit. Results, per-language breakdowns, paired
comparisons, checksums and timing populations are saved in
`verification/context-serving-100-replayed/analysis.json` and `audit.json`.
The focused unit tier passes 454 tests; strict documentation build and public
documentation checks pass. Both evidence sets are in the cohort receipt folder.

### Measured serving cost

All 200 case/model attempts completed. The provider receipts contain 196 planner
calls and 1,000 successful decision calls covering 8,765 candidate questions.
Two LangChain snapshots failed recursive submodule preparation and made no model
calls. The **total observed API charge is $3.56329118**, including both compared
routes and shared planning once: Sonnet $1.36550348 and Opus $2.19778770.
There were no decision API failures or missing decision cost receipts. Offline
replay adds no model requests or API cost. A separate cost audit reconciles the
per-case charges with the live run's cumulative counters.

| Planner | One graph route, USD / selected task | Verified 500, estimated USD | Full 1,136, estimated USD |
| --- | ---: | ---: | ---: |
| Sonnet 4.6 | 0.011945 | 6.18 | 13.73 |
| Opus 5.5 | 0.020451 | 10.67 | 23.62 |

The per-task denominator is all 100 selected tasks, including failures.
Projections reweight language-specific observed cost by each target population's
language counts. They preserve the present one-shot serving policy and observed
failures; repository size, issue length and future repairs can change the price.
They are estimates, not spending caps. Repeating **both planners and both routes**
with shared planning would cost about $18.36 for 500 tasks or $40.88 for 1,136.
This is higher than the original five-small-Python-task estimate.

Decision scoring for one graph route averages about $0.00208 per selected task.
Planning accounts for 82.5% of Sonnet's and 89.8% of Opus's graph-route API charge.
Graph construction, source parsing, CPU/disk cost, repeated context requests and
the final solver remain outside these numbers. A full-agent comparison requires
all of those model calls and actual resolution outcomes; the published
JiuwenSwarm charge of $0.571 per issue measures a different scope.

Cost receipts are in `verification/context-serving-100-v3/cost-summary.json`.
The evaluator's known-function probes pass for all eight dataset languages in
the isolated legacy tree-sitter environment.

For leaderboard submission requirements, use the public
[`ContextBench guide`](../evaluation/contextbench.md#preparing-a-leaderboard-submission).
The development cohort remains retrieval-only: it cannot supply the required
Pass@1, and its API cost cannot be substituted for a complete agent's bill.

## Reproduction and receipts

```bash
python scripts/benchmark_graph_decisions.py prepare \
  --dataset /path/to/ContextBench/data/contextbench_verified.parquet \
  --output verification/graph-decisions --device cuda
python scripts/benchmark_graph_decisions.py run \
  --prepared verification/graph-decisions \
  --output verification/graph-decisions-live --live --max-cost-usd 0.5
python scripts/benchmark_graph_decisions.py evaluate \
  --prepared verification/graph-decisions \
  --run verification/graph-decisions-live \
  --upstream /path/to/pinned/ContextBench \
  --evaluator-python /path/to/compatible/evaluator/bin/python

# Reuse that frozen corpus and its graphs; only the grep planner is billed here.
python scripts/benchmark_graph_decisions.py prepare-grep \
  --prepared verification/graph-decisions \
  --output verification/graph-grep --live --max-cost-usd 0.25
python scripts/benchmark_graph_decisions.py run \
  --prepared verification/graph-grep \
  --output verification/graph-grep-live --live --max-cost-usd 0.5
python scripts/benchmark_graph_decisions.py evaluate \
  --prepared verification/graph-grep \
  --run verification/graph-grep-live \
  --upstream /path/to/pinned/ContextBench \
  --evaluator-python /path/to/compatible/evaluator/bin/python

# Frontier planner; keep the decider and answer-model defaults fixed.
python scripts/benchmark_graph_decisions.py prepare-grep \
  --prepared verification/graph-decisions \
  --output verification/graph-frontier/opus-prepared \
  --planner-model anthropic/claude-opus-5.5 \
  --planner-reasoning-effort medium --planner-max-tokens 4096 \
  --live --max-cost-usd 2
python scripts/benchmark_graph_decisions.py run \
  --prepared verification/graph-frontier/opus-prepared \
  --output verification/graph-frontier/opus-live \
  --arms grep_decision grep_union_decision --live --max-cost-usd 0.5
python scripts/benchmark_graph_decisions.py evaluate \
  --prepared verification/graph-frontier/opus-prepared \
  --run verification/graph-frontier/opus-live \
  --upstream /path/to/pinned/ContextBench \
  --evaluator-python /path/to/compatible/evaluator/bin/python
```

Repeat the frontier commands with model `openai/gpt-6-astra` and distinct
`astra-prepared` / `astra-live` directories for the second control. `--arms`
selects only routes present in the frozen preparation. The planner defaults
remain the original non-reasoning Sonnet configuration unless explicitly set.

The original `prepare` makes no billed calls. It needs the embedding revision
already in the local Hugging Face cache and a working Python SCIP toolchain. Default dense
device is CPU; the recorded pilot explicitly selected CUDA. Model credentials
are read from `OPENROUTER_API_KEY`, never written to artifacts.
For the evaluator, create a Python 3.10/3.11 virtual environment and install the
pinned upstream `requirements.txt`; keep those legacy parser packages separate
from the CodeNib environment.

`prepare-grep` does not build graphs or load an embedding model. To reuse the
new Opus plans without new API calls, keep its model/reasoning/output settings
above, choose a fresh output directory, and replace `--live` with
`--plans-from verification/graph-frontier/opus-prepared/plans`. The directory
contains one `<curated-ID>.json` plan per task. Cached planner inputs, requested
model, request parameters, system prompt and executor implementation hashes
must match exactly. The older Sonnet receipts require the archived helper;
they deliberately do not pass the current helper fingerprint/configuration check.

`manifest.json` freezes selected tasks, script identity, candidate data hashes,
parameters, source spans, graph receipts and per-stage timings. A changed script,
task or candidate file is rejected before paid inference. Use a fresh prepared
directory after changing the experiment. Each live run requires a fresh output
directory; automatic retry/resume cannot silently select a preferred sample.

`results.json` contains every arm, exact delivered context, full rankings, decision
answers/usage, and final model diagnosis/usage. `evaluation/` retains official
per-case metrics and scorer logs; `summary.json` holds the official aggregates.
The `final_*` summaries are micro averages; trajectory summaries are macro
averages, following upstream. `analysis.json` adds task-weighted coverage and
timings, and `audit.json` records checks of the actual delivered contexts.
Candidate-pool ceilings are scored separately from delivered context. Only the
single delivered context step is scored: its AUC is not a multi-step agent
efficiency measurement. There is no EditLoc population without actual patches.

Retrieval is measured during frozen preparation; decisions and answers are
measured during inference. Their sum is a **stage sum**, not a continuously
measured production end-to-end latency. Loading, graph/dense construction,
provider/network variance, shared GPU contention and preparation are reported
separately or explicitly excluded. Arm order is fixed; no latency significance
claim is possible from five tasks and one run.

The discarded `pre-source-audit/` preparation used synthetic chunk headers. It
was rejected before formal model calls and is not part of any reported result.

Verification covers cycles, hop boundaries, filtered file hubs, raw edge caps,
candidate caps, method/split/decorator binding, exact original-source clipping,
deduplicated token-limited context and explicit API-failure handling. The broader
eval and typed decision tests also run without billed calls. Official extractor
checks cover all eight benchmark languages and reject vacuous perfect scores.
The initial focused evaluation/decision tier passed **411 tests**. Black, isort,
flake8, strict MkDocs and the public-document boundary check also passed.
The extension tier, including the existing grep executor tests and new explicit
seed/no-refill and planner-input boundaries, passed **438 tests**. Extension
receipts live under `verification/graph-grep/` and `verification/graph-grep-live/`;
`analysis.json`, `per-case-analysis.json`, `audit.json` and the saved `analyze.py`
retain the matched comparison and source/usage audit.
The frontier compatibility extension passed **25 focused tests**, followed by
**444 tests** in the relevant evaluation/dataset/decision/grep tier. Its receipts
live under `verification/graph-frontier/`; `analysis.json`,
`per-case-analysis.json`, `audit.json`, `model-catalog.json`, and `analyze.py`
retain the comparison, request availability snapshot and audit. Run the saved
audit with `PYTHONPATH=. python verification/graph-frontier/analyze.py`; it
makes no model calls.

Each run's preparation/inference implementation is archived as
`executed_script.py`; its SHA-256 matches that run's frozen manifest and
configuration. The initial archive predates the evaluator-interpreter option
and grep extension. To start another inference run with a changed source hash,
prepare a new directory; existing saved outputs can be rescored directly.
The frontier run adds planner request options, exact request-parameter cache
validation, a 60-second planner read timeout and explicit inference-arm
selection. Its two executed scripts are archived under
`verification/graph-frontier/` and match both new preparation manifests. The
original Sonnet helper is also retained as
`verification/graph-grep-live/executed_grep_helper.py`. Existing source/corpus,
retrieval, graph traversal, reranking and context-packing algorithms remain
unchanged between these controls.

Promotion would require a separately authorized, frozen multi-repository and
multi-language trial, paired quality comparisons against dense + the same
decider, graph-missing/unmapped cases, actual task outcomes, cold/warm costs and
p50/p95 wall latency. A useful next hypothesis is decision-selected seeds or a
learned expansion gate, but neither is implemented or authorized as a product
default by this pilot. Existing Jev dense controls are documented in
[`jev_dense.md`](jev_dense.md); there is no cross-dataset pooled score here.

### Merge-review validation boundary

The recorded graph-cohort scores predate the fix that includes all chunks bound
to a seed symbol at hop zero. They are historical receipts and were not rerun
after that correction. This does not change the graph-free follow-up receipts.
Evaluator startup now probes known functions in all eight dataset languages and
rejects hidden Git index flags. Completed repacks audit the current source before
and after reading; Claude batch audit failures retain failure/usage accounting.
Recorded decision calls with invalid usage block additional calls until reconciled.
These changes were validated without new model calls.
