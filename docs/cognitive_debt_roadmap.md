<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Cognitive-Debt Reduction Roadmap

## Objective

CodeNib should remain understandable as a product while its language and query
capabilities grow. This program reduces the number of independent concepts,
execution paths, and lifecycle states a contributor must understand. A smaller
file or a moved symbol is not progress unless it also removes an implementation,
an invariant, a public contract, or a required test matrix.

The protected product journeys are:

1. `codenib codegraph init` -> compiler -> manifest -> MCP;
2. `codenib wiki` -> compiler -> manifest -> local Wiki and Ask;
3. `RepositoryContextExplorer` -> manifest-backed repository context.

Subtraction must preserve these journeys, source identity, repository-relative
locations, manifest compatibility, multi-language routing, and explicit
capability degradation.

## Visitor Wiki iteration

Status: implemented and locally verified in
[#802](https://github.com/sysevol-ai/CodeNib/pull/802); lifecycle: `product`.
The public URL entry now consumes
AgentWiki instead of maintaining a second repository-explanation generator.
The short explanation component, source-preview page, overview protocol and
its dedicated browser harness/tests are removed. Only URL parsing is retained
from that path. Shared Markdown rendering and the existing Wiki generation,
source authority, grep/Jev operations and WikiStore boundary remain the owners.
The new adapter owns consent, bounded visitor attempts and progress; it does
not introduce another index or persistence backend. The obsolete existing-key
browser explanation adapter and GitHub browser-fetch CSP permission are also
removed. Real acceptance generated 11 complete chapters and reopened all of
them on desktop/mobile without generation. Store ownership, restart/resume,
budget and provider-free browser checks cover the replacement journey.
Demo deployment remains the maintainer's open gate.

## Baseline

The 2026-08-24 audit of `main` at
`c95ce9a2d1f1ab6bb4c3b2a8e79a699531b21b85` found:

| Surface | Python lines | Observation |
| --- | ---: | --- |
| `codenib/` | 225,398 | Up from 88,165 at `v0.1.0` |
| `test/` | 202,130 | Up from 59,931 at `v0.1.0` |
| `scripts/` | 41,280 | 37 scripts / 9,650 lines had no maintained entrypoint or documentation reference |
| research and compatibility packages | 48,123 | `eval`, `dataset`, `clients`, `integrations`, and speculative `serving` ship in the stable namespace |
| persistence packages | 24,469 | `storage` and `artifacts` implement a stronger threat model than the default local product paths use |

The audit also found three independent persisted-view loaders, three retrieval
execution paths, package-level `compiler`/`artifacts` and `web`/`wiki`
dependency cycles, and experimental implementations retained after their
promotion gates had closed.

## Lifecycle Classes

Every top-level package or substantial feature must have one current class:

| Class | Meaning | Compatibility expectation |
| --- | --- | --- |
| `core` | Required by every protected product journey | Stable internal direction; strict regression gates |
| `product` | User-facing adapter or application | May depend on core, never the reverse |
| `optional` | Supported backend selected by an explicit capability | Tested contract; no default-path import cost |
| `experimental` | Time-bounded candidate with a named promotion gate | No stable re-export; owner and exit date required |
| `labs` | Evaluation, research, or upstream-policy compatibility | Separate install/test surface from the product core |
| `legacy` | Compatibility-only surface awaiting removal | Deprecation owner and removal release required |

Unknown is not a valid long-term class. New experimental code must declare its
class before it lands.

## Subtraction Rules

1. A failed or superseded experiment retains its decision, receipts, and
   aggregate evidence in a durable document. Its implementation, executable
   harness, private ABI, environment switches, and dedicated tests are removed
   unless a new approved gate names them as inputs.
2. No experiment remains indefinitely because it is default-off. Default-off
   code still consumes review, documentation, build, and test attention.
3. Trivial helpers are not centralized merely to reduce line count. Extract a
   shared component only when it owns a repeated workflow or invariant.
4. A new facade must replace at least one old path before the iteration closes.
   Parallel migration paths require a removal milestone.
5. Stable public exports require a documented product consumer. Research
   scripts and examples import explicit submodules or a labs package.
6. Tests for deleted behavior are deleted. Historical evidence belongs in the
   roadmap, not in permanently runnable tests for an unavailable candidate.
7. Each iteration reports production lines, test lines, scripts, public entry
   points, and architectural paths removed. Net line count alone is not the
   acceptance criterion.

## Target Dependency Direction

ContextBench remains a `labs` workflow: its task loader and annotation schema
live in `dataset/`, while trajectory conversion, agent-run orchestration, and
official scoring live in `eval/`. It uses the existing agent runtime and adds
no product dependency on benchmark labels. The public evaluation architecture
guide records the remaining older SWE-Explore and client-policy coupling;
this addition does not complete or claim a package-wide subtraction iteration.

The ContextBench context-serving experiments are also `labs`: the source-only
`scripts/benchmark_graph_decisions.py`, `scripts/benchmark_context_serving.py`,
`scripts/benchmark_decision_loop.py`, `scripts/benchmark_context_agent.py` and
`scripts/benchmark_context_claude.py`
compose existing retrieval, typed decisions, native agent tools and scoring.
Their owner, matched controls, receipts and promotion gates live in
`docs/experiments/graph_decisions.md`. They add no product retriever, model
registry, persistence boundary or dependency on evaluation labels.
The 100-task graph cohort has modest planner-dependent gains with graphs on
48 tasks. Those graph metrics predate the merge-review fix that includes every
chunk of each seed symbol at hop zero; they have not been recomputed. In the
24-task graph-free follow-up, Opus feedback expands candidate
Recall to 55.57% but the old 4K delivery retains only 37.90%. Repacking the same
scores at 32K delivers 55.11% Recall, with lower F1; output truncation and noise
are therefore separate measured limitations. The native search/read reference
reuses `AgentRunner` and completes all 24 tasks: cumulative Recall/F1 is
57.50%/29.76%, versus 64.46%/27.45% for the matched public leader. Its 32K delivery
retains 57.26% Recall; $23.7864 in recorded API charges makes this a strong-model
quality reference, not an ultra-low-cost route. The October 11 iteration is
complete; implementation and recorded results were merged through
[#827](https://github.com/sysevol-ai/CodeNib/pull/827). Retrieval SOTA was not
achieved. The 32-task validation selection excludes all 105 prior development
IDs; its completed October 16 result is recorded below. The full-500 quality
gate remains unrun. Paired final decision selection adds essentially no gain at
16K/32K. Deterministic completion of touched callables reaches 64.97%/29.06% at
16K and 66.95%/27.42% at 32K on the development 24; its new reads remain distinct
from the original Opus trajectory and its intervals do not establish SOTA.
The requested Claude Max alternative passes an official Agent SDK smoke with
actual Opus 5.5 and the same three source tools; SDK metering is separated from
unknown actual billing. Its follow-up batch was stopped with 3 completed,
1 interrupted and 20 unrun tasks, without a cohort quality claim. No further
model experiment from that closed batch is scheduled. The October 16 follow-up
adds online function completion, directed compiler navigation and a Verified
downstream solver adapter to source-checkout scripts in draft
[#834](https://github.com/sysevol-ai/CodeNib/pull/834), retaining `labs` ownership.
The fresh eight-task online control changes Recall 41.13% → 41.67% and F1
33.67% → 31.91%, with both paired intervals crossing zero. The first completed
native solver sample and fresh matched one-task solver pair are unresolved; the
reference patch passes the same native harness. The first optional-graph
comparison completes but makes no graph calls. The targeted-hop trial makes eight
graph calls, returns source on three tasks, and passes its development selection
gate: Recall 37.43% → 41.29%, F1 30.18% → 32.85%, with both intervals crossing
zero. Its frozen independent three-arm, 32-task validation rejects the candidate:
Recall changes 69.64% → 68.76% and F1 29.01% → 26.16%. Every arm retains 31 valid
scores and one source-preparation failure as zero. All eleven available graphs
are called, but only six tasks receive graph source. The one independent Verified
solver task resolves in each arm, 1/1 each; this establishes no comparative or
population Pass@1 gain. Return to development IDs for diagnosis and require a
new independent selection for any revised policy. Reliable directed-graph gains,
the other native harnesses and full-agent outcomes remain open. The rejected
policy does not advance to the official 500; no product route or package
lifecycle is promoted.
The current first-source graph policy prioritizes callable neighbors and skips
observed source. Its complete 24-task development primary pair changes Recall
53.35% → 54.02% and F1 31.18% → 30.15%; the declared quality gate fails.
All eleven available graphs are called, and three tasks in Go/Rust receive 139
previously unseen annotated physical lines. The function-only ablation aborts
after four completed cases and one partial error, retaining 19 not-run cases.
The SDK normalizer now preserves unavailable attempts through terminal usage and
excludes error feedback from source credit. Graph instructions appear only when
the tool is registered; a separate one-task reliability check passes, without
replacing the failed experiment or recovering its unknown usage. An offline C++
probe restores one missing graph through compilation-database preparation in a
private clone. Broader graph coverage and revised matched quality evidence remain
required before a new independent selection. Earlier failed gates and mechanism
checks stay in the experiment record; no product route is promoted. The new solver pair is
0/1 resolved in each arm on the repeated development task; all 18 model inputs
have hashed, confirmed receipts including actual output truncation. The first
Multi C++ native environment check rejects its test-only control and accepts the
reference repair with 18 passing tests, without model calls or an agent score.
A separate solver image removes reference artifacts and Git history while
preserving the selected source tree. The broader offline preparation check has
14/24 graphs, with eight new successes
and five losses; a separate authenticated composition has 19/24 available.
Replaying the same actual graph queries with that composition increases canonical
annotated source absent from native from 49 to 194 lines across C and Go. The
earlier unchanged-49 claim missed container-prefixed annotations; official scores
remain unchanged. The evaluator now exports the pinned canonical annotation
intervals, and retrospective corrections preserve original frozen records.
Optional conversation-aware
function completion preserves the original callable selection and removes 726
repeated line deliveries in a fixed 24-task replay, with identical cumulative
source and 3.63% fewer reply characters. Neither replay establishes an online
quality gain; all 24 final 16K/32K payloads remain byte-identical to their
historical controls. A fresh three-arm late-relationship pilot completes all
eight development tasks per arm. Recall changes 39.85% → 41.62% and F1
32.85% → 35.29%, with both primary intervals crossing zero. Nine graph calls add
200 annotated C lines, 197 absent from native, but no other language gains
annotated graph source. Its two-language gate fails. A zero-model diagnosis
reproduces all nine replies and three cached C++ graphs. Choosing the smallest
containing parsed range fixes 38 clangd graph spans without changing compiler
identities or edges. The affected query now selects the observed constructor,
but adds no source; eight replies remain identical. Nearby reference windows
provide little unseen source. A separate JavaScript fixture reproduces missing
assigned-function definitions in SCIP under three project configurations.
The existing LSP route also loses compiler name spans before function
initializers; the shared client now recovers them through advertised `navtree`
support with exact, unambiguous name/body matches. A complete 764-file NodeBB
graph makes 31,351 reference queries without errors and recovers 26/18 references
for the two previously queried targets. Their unchanged fixed-query replay adds
132 physical lines but no annotated source; omitted neighbor ranges also have
no unseen annotations. This supports neither a larger graph budget nor an online
quality claim. Useful relationship selection across languages and active SCIP
definition coverage remain open; no new independent or official-500 run follows.
The shared clangd and LSP consumers gain correctness fixes with 206 related LSP
tests passing, without a new parser, route, schema or benchmark-label dependency.
The 72-record development source-gap audit reproduces every prediction and
official line count. Most recoverable missing source is outside the callables
already touched. A bounded whole-file remainder hypothesis preserves existing
replies and uses the same completion budget, but its fixed-trajectory F1 falls
on both the 24-task and eight-task cohorts. It is rejected before agent
implementation or new inference. Its private replay and negative aggregate
evidence are retained; no optional product surface is added. Evaluation-only
annotation diagnostics identify 331 unavailable lines across nine development
tasks, preserve official paths/denominators, and enter the existing evaluator
bridge rather than a new framework. Future experiment archives include that
bridge. The current BM25 feasibility check also fails on the disclosed 24 tasks:
a 2K source union adds 38 annotated lines, but F1 falls 31.18% to 26.62%; the
overlapping latest eight add none. Its source and candidate hashes are frozen
before annotation analysis. No automatic injection or new retrieval tool follows.
A canonical-source ablation also rejects removing read completion wholesale:
it loses 219 annotated lines from the first-source 24 and ten from the overlapping
latest eight, including 150/seven absent from native. Both original baseline
counts reproduce before comparison. Keep the existing completion behavior; the
fixed traces provide no new online or independent quality result.
Source attribution now reuses one environment adapter for controlled reads and
searches in the existing native solver loop. It records exact per-file versions
and rendered input positions, preserves native truncation, and credits only
complete file-qualified matches in confirmed inputs. Search caps and skipped
files stay explicit. A scripted source-only container verifies the boundary with
no model calls: 94 of 113 larger-search matches survive native truncation;
changed files and ordinary shell output receive no base-source credit. The first
fixture edit failed because of its command string and remains recorded; the
corrected fixture passes and both containers are removed. Existing fmt receipts
validate 411/375 initial source lines;
their shell reads remain unattributed rather than reconstructed from requests.
This source-checkout adapter creates no product tool or persistence boundary.
Its partial coverage is not an official trajectory: arbitrary shell output and
modified-file base coordinates remain open. The first real reader-adoption
check fails and retains its five calls and $0.695992 SDK estimate. The corrected
usage feedback passes a separately frozen check: two confirmed standalone reads,
eight calls and $0.932850 in SDK estimates. Its repeated fmt repair passes 18
native tests, but both reads were already in the initial context and add zero
original-source lines. The same environment adapter supplies the feedback;
no shell interpreter or product tool is added. Both protocols remain separate,
and source coverage stays partial. Fresh matched runs and relevant repair/test
feedback remain required before a quality claim. A frozen actual-model search
check on the disclosed C task has nine confirmed calls and two controlled reads,
adding 70 original-source lines, but no controlled-search call. Its repair fails
eleven native tests; both owned containers are removed. Keep the failed adoption
gate and partial source coverage. Repeated one-task checks establish no gain.
The fmt pilot
has source-only solver preflight and a separate pinned native adapter, with exact
patch replacement and owned-container cleanup. Its matched development pair
resolves one C++ task in both arms, 1/1 each, passing 18 tests with ten confirmed
model inputs and $1.321802 in SDK estimates. This task had no graph during its
original retrieval, so the result establishes no graph-source contribution or
comparative Pass@1 gain. The same adapter now supports the validated Gson Java
pilot, with exact repository/task identity and three authenticated native script
hashes. Its source-only reader and native reference/negative controls pass with
zero model calls; all 96 required test classes pass for the reference while a
known always-failing class remains recorded. These grading fixtures are not agent
repairs. Reuse the existing scripts and native harness, without a backend registry
or new product surface. A fresh Gson pair then resolves the task in both arms,
passing all 96 required classes with eleven confirmed inputs and $1.088503 in
SDK estimates. Each arm adopts the same reader; its new-source contribution is
0/1 line, with partial totals of 318/294. The original retrieval had no graph,
so the pair establishes no comparative or graph-source gain. All owned containers
are removed and frozen hashes match after grading. Jackson's tracked
build-configuration rewrite remains deferred. Other Multi repositories, Poly/Pro
harnesses, broader matched outcomes and shell-source attribution remain open.
The existing Multi adapter also admits the validated ponyc C pilot, adding only
one repository and one pinned native module. Its negative control fails seven
required tests and the reference passes all 1,057 required tests. A missing-library
link failure persists outside those labels and is reproduced on unmodified
source; full standard-library coverage is not claimed. Source-only image recipes
add a hash-verified Python runtime and precompile base source without changing
the original grader. Source/history/reader checks pass with no model calls.
The fresh frozen C comparison is complete and negative: native/functions submit
patches but fail 12/11 native tests, including five preservation tests in both;
the graph arm ends at its SDK budget with no prediction. The function-only
payload already covers the earlier graph response, so it establishes no unique
graph benefit. All 29 model inputs are confirmed and all five owned containers
are removed. One valid terminal usage event was omitted by post-loop accounting;
an offline amendment recovers it without replacing the failed attempt. The
adapter now persists usage on receipt before the SDK can raise, retaining the
failure and exhausted-budget stop. Offline diagnosis confirms that the five
compatibility cases are grader-added, while all three initial contexts already
contain the complete casting helper and FFI call implementation; another expansion
of those functions is unsupported. A 33-query graph audit finds original-direction
ranking opportunity only in Rust. A general empty-response opposite-hop replay
improves the late eight but fails its two-cohort gate. A distinct, explicit
late-only experimental option now has a completed matched comparison on eight
other metadata-selected development tasks: all 24 runs finish, but its gate
fails. Recall/F1 point estimates rise while both intervals include zero; none
of the eight graph replies changes when the fallback is disabled. No novel
annotated graph source is absent from matched native. The option stays confined
to checkout experiments for reproduction, with no product/default promotion or
further online retry justified by this sample. Source-bound coverage work now
fixes C++ callable names and conversion-method kinds in both query paths,
preserving compiler identities and every edge in three rebuilt graphs.
TypeScript/Java preparation raises latest-eight availability from five to seven,
but unchanged queries add 146 physical lines and zero annotated lines. No new
online batch follows. The C source guard correctly rejects seven tracked
submodule helper changes; an explicit build-overlay treatment remains open.
Relevant source selection and active SCIP assigned-function coverage remain
gates before another policy trial. These diagnostics make zero model calls.
The retrieval runner also checkpoints terminal SDK metering before an iterator
error; the old loss is reproduced without model calls. Known cumulative SDK
estimates before the latest online/repair batch were $90.165263, not an account
bill. A separate frozen 24-task Jev ordering control makes 96 API calls for
$0.025743858, with complete usage records. At the same 2K budget it improves
Recall/F1 from 0.53%/0.83% to 9.11%/7.77%, but the whole BM25 candidate pool
covers only 11.99% of annotated source. Static addition still reduces F1 on both
cohorts, so no injection/tool is promoted. The subsequent frozen grep-pool
control uses 484 existing Opus candidates at 4K: Jev improves Recall/F1 from
32.46%/22.90% to 36.56%/28.52%, with 55 successful calls for $0.012341112.
It passes the ranking gate but still fails static addition. The subsequent
eight-task live native/chunks/Jev control completes all 24 sessions and fails
its retrieval gate: Jev changes Recall/F1 from native 42.36%/36.80% to
40.97%/31.94%. Main-model API SDK usage is $5.49084920; 55 Jev decisions add
$0.00552048, with exact source/receipt checks and no missing usage. Runtime
`cf4bc60b` is checkout-only: bounded source-block selection precedes the next
model turn, API routing is explicit, and uncertain decision usage stops the
stream. Do not promote this automatic source-block policy or expand the cohort.
The two predeclared compatible development repair pairs are complete: both fmt
repairs pass 18 native tests, while both ponyc attempts reach their SDK guards
without a prediction. Each arm resolves 1/2 selected tasks. The Jev C attempt
confirms three searches and four reads adding 130 original-source lines; this
establishes narrow actual adoption without a repair gain. The recorded continuation
completes only the previously unrun Jev C arm after a fully metered error, with
no baseline retry or budget increase. Repair SDK estimates total $7.24264130;
all frozen hashes and owned-container cleanup verify. The prospective solver fix
collects existing edits after a fully metered SDK budget exception while retaining
its error; it makes no extra model call and gives terminal diffs no source credit.
Historical failed attempts stay unchanged. Current per-route usage estimates
and unreconciled account billing remain in the results export; actual charges
and remaining promotional credit are unknown.
Stop BM25 variants and do not expand the rejected automatic source-block policy.
Require an actionable search-selection diagnosis and a fresh matched protocol
with the corrected patch-collection policy. No revised independent gain is established.
The further verbatim-grep read-hint experiment also fails: all 24 sessions
complete, but Jev lowers Recall/F1 from native 43.29%/37.08% to 38.73%/34.17%.
The unranked secondary also misses its gate. Original replies and candidate/order
receipts replay exactly, and suggestions gain no unread-source credit. Its API
usage value is $4.848424762. All four repair attempts submit for $6.03557620 in
Max SDK estimates; both fmt grades pass with nearly identical costs, while both C
patches fail native resolution. Each arm resolves 1/2; frozen input/prompt and
owned-container audits pass. Stop these Jev output-format variants. The isolated
optional retrieval-note handoff completes all six native grades: source 1/3,
notes 0/3, with notes costing 24.0% more in repair SDK estimates. Reject it;
all source/prompt/hash/usage/container audits pass, and both C budget exceptions
retain their failed status and captured patches. A read-only failure audit finds
Pylint collection errors in both arms and native test-patch conflicts in both
Multi notes arms; do not interpret those as behavioral test failures. Native
readiness must require an executable empty-control baseline, beyond a passing
reference control; prospective patch-application rules remain an open gate. Its
$9.058375 Max SDK value is usage estimation, not confirmed billing. Pylint source/image and
21 native regression labels verify against empty/reference controls without
model calls. The explicit solver API route and empty-context control pass 86
local tests and pinned hooks. All six repairs and native reports in the
$1-reserved Haiku screen finish for $0.26029216 across 55 SDK queries. Issue-only
and frozen source both resolve 1/3, with 35/20 repair queries. C failures fall
from 11 to seven of 1,017 tests, but neither repair resolves; Python collection
fails in both arms. Actual model/route, frozen hashes, source-only initial-prompt
contrast and owned-container absence verify. Allocating prior Opus retrieval
raises the source pipeline estimate to $0.683119 versus issue-only $0.130250,
so neither resolution nor pipeline-cost gates pass. There are no notes or new
retrieval calls. Report its repair cost separately from the upstream Opus
retrieval allocation. No revised independent or leaderboard gain is established.
The user-directed priority is pure ContextBench retrieval, with Recall first and
F1/cost reported; preserve historical frozen gates. No further repair or native
harness batch precedes useful replicated context evidence. The eight-task source
audit finds 1,768 missing available annotated lines in observed files and 773 in
unobserved files; 125 unavailable annotated lines remain in official scores.
The completed four-task file-symbol pilot fails: Recall/F1 40.75%/39.77% →
39.37%/36.83%, $1.496507 API SDK usage, all reliability checks passing. Reject it.
The hit-preserving nearby-source policy passes its Recall-first development
screen and unchanged-policy replication. Across all eight development tasks,
Recall improves 35.24% → 38.91%, while F1 falls 31.26% → 28.20%; the exploratory
Recall interval [-8.05, +14.66] points includes zero. All 16 sessions complete
for $3.64572980 in API SDK estimates, with no repairs. A failed auxiliary grep
rerun comparison and its separate receipt-only correction remain recorded;
original attempts and official scores are unchanged. Source/model/route/usage/
delivery audits pass. Keep this as development evidence. The completed independent32 retains every selected case and records Recall/F1
65.52%/26.12% → 68.93%/23.30%. The Recall interval is [-2.27, +10.08] points; its frozen
gate does not pass. The run costs $12.63056860 in API SDK estimates and makes no repair
calls. Runtime gold remains empty; selected annotations load only after terminal output
receipts, and confirmed-source projections preserve raw attempts. Both validation
cohorts are sealed. This validation covers seven languages, with no C++; future
untouched tasks also lack Java. Do not promote the unchanged policy into repairs,
official500 or a product default; the positive point estimate has not established a
stable independent gain.
The new default-off `scripts/contextbench_file_source.py` remains labs-only and
appends exact unread same-file windows while preserving native reads. The unchanged first-read source-window policy fails its complete 24-task development stability gate. Recall 58.39% → 63.91%, F1 31.97% → 27.94%; repository-cluster 95% Recall interval [-2.76, +12.52] points across 24 repository clusters. All 48 selected SDK sessions are retained for $9.86110630, with no repairs. Independent and official500 gains remain unestablished.
The late physical-source compiler and actual-input reviewer remain labs-only.
The source consumer now replaces its CLI loop with one bounded Messages request;
Opus 5.5 requires adaptive thinking, so requested disabled thinking was an invalid
assumption. Free token preflight, 2,048 output tokens and no automatic retry avoid
CLI continuation; 104 focused tests and 24-case source construction parity pass.
The new complete 24 source-delivery engineering gate passes: Recall 58.39% → 65.18%,
F1 31.97% → 26.26%; descriptive repository 95% Recall interval
[+3.23,+11.36] points. New Messages usage $4.25474400 is separate from SDK usage;
all failed SDK studies/gates remain, with no retrospective source-credit change.
The source opportunity was already scored: not independent quality or fresh paired
adaptive-agent behavior. A new 32 task-level cohort is fixed from untouched metadata across six languages
and 23 repositories; all 32 exact-source checks pass with empty runtime gold.
Its original execution fails locally before labels after eight native investigations
and 14 consumers ($2.56634860); that failure remains. Runtime `8d5e0b87` corrects
tokenizer-marker literal counting, with 129 focused tests and seven byte-identical
pair replays. A pre-label amendment reuses all completed calls, changes no source
selection or quality gate and completes 32 native/64 source-consumer calls for
$11.18372580 under the same $24 whole-study cap. Recall 74.94% →
79.09%, F1 30.13% → 22.32%; repository 95% Recall
interval [+1.99,+6.85] points across 22 clusters.
The amended gate passes; labels load only after terminal source-prediction
receipts. All three 32 cohorts are sealed. This is source coverage with extra
volume/cost, not fresh paired adaptive agents or repair gain. No product lifecycle
change or repair/full500 starts automatically; verified500 still needs a measured
budget and source/native readiness plan. See [current results](evaluation/contextbench-results.md)
and draft [PR #834](https://github.com/sysevol-ai/CodeNib/pull/834).
The four-task Max/Batch route engineering gate now passes under protocol
`4cb3ebd8`, runtime `4143a045`: four normal Max investigations and eight normal
Batch reviews,zero unknown usage/retries/repairs. New API usage is $0.430218;
Max SDK nominal $1.2485557 is separate. 52 focused tests (29 new Batch cases),
durable one-create intent and source/prompt/runtime/metering receipts pass.
Current known API usage is $59.09398873, leaving $40.90601127 within the same
$100 ceiling. This verifies the small execution route, not new quality or 500 quota.
Source-only runtime `50519a3a`, protocol `87b5c023`, verifies all 500 exact parent
snapshots with zero preparation errors or unstarted positions. Three cases retain
empty unmapped gitlinks explicitly; complete child-source availability remains
unestablished, and neither tasks nor gold denominators are filtered. The initial
strict attempt (452 ready, 3 errors, 45 unstarted at its 12 GiB floor) stays failed.
A frozen two-worker continuation uses an 8 GiB admission floor after the large
language sources finish. Repacking 16 newly owned non-donor Git stores reclaims
1.82 GiB with source, refs, configuration, alternates and donors unchanged;
connectivity checks pass. Thirteen local Git tests pass before 122 relevant unit
tests, with eight integration cases deselected. Pinned hooks pass.

All 24 development and 32 sealed packet pairs match current compiler bytes and
complete consumer params. The original whole-file check rejects 24 for the
disclosed two-keyword packing fix and remains failed; a separate AST/code-path
audit establishes parity. Another 32 original native prefixes qualify through
full source/profile/code parity and equality with the fixed validated built-in
catalog. Preserve their initial strict empty-catalog failure. Reuse every matching
case without new scoring: 88 native prefixes, including 56 complete pairs,
leave 412 fresh investigations and 444 candidate inputs.

The original canonical500 native execution `cf2f2e47`/`e8f6b675` fails after37
attempts/36accepted when assistant output precedes the optional quota event and
the guard interrupts before terminal usage. Preserve the failed gate and one
unknown Max terminal usage; do not substitute partial tokens or rerun the case.
It calls no source tools, so both arms retain zero source for that case within
all500 denominators.

Quota-order protocol `21456878`/runtime `1b0ad5ac` completes65accepted additional
investigations, then stops before dispatch at its4GiB artifact admission floor.
All65usage/source/quota receipts are known and valid,all intents reconcile and
children are terminal;the capacity stop addszero failed model cases. Preserve
that stopped state and all original traces.

Capacity protocol `ac478f89`/unchanged `1b0ad5ac` completes53known-usage cases;
52passitsstrict unavailable-attempt ban. One otherwise normal investigation tries
`mcp__source:read`; theSDK rejects it with`No such tool available` and no source.
Preserve the originalfailedstrictgate andfull attemptedaction/error/rawreceipts.

Prelabel source-receipt protocol `4daea3d3` qualifies actual read/grep/glob inputs
only after exactSDK rejection/identity/is_error checks,withzero rejected-action
source. No toolalias/execution/capability is added. Native geometry andcompiled
packetbytes equal their versions withoutthe rejectedaction;five adversarial
boundaries failclosed. All53streams audit and242prefix/56pair replayspass without
labels/newcalls. Model/prompt/source/compiler/budget/statisticalgate unchanged;
originalstrictgate staysfailed. Reuse242qualifiedprefixes/56pairs,retain1original
failedzero/unmeteredcase andall500denominators,continueonly257unclaimed IDs.

All500 native records are terminal and source-sealed under protocol `f7057ac6`:
499 valid and the one original failed empty-source case, retained as zero in the
fixed500 denominator. Line task-macro Recall/precision/F1 are72.83%/27.97%/33.95%
across eight languages/59 repository clusters. This is basic read/grep/glob, not
an indexed-context gain or independent validation. Prior122 annotation access is
disclosed; the original all500-before-first-Gold schedule was not followed.
The CLI's unchanged micro summary is retained separately, not confused with task
macro. Native selected known Max nominal usage is91.1558898USD plus the original
unknown terminal; this is not an API cash bill.

The SDK adapter now validates exact typed budget terminals after post-result
exceptions, with60 relevant tests. The historical214-case continuation's strict
failure remains; a pinned-SDK source-only replay qualifies its saved terminal
without altering messages/cost or retrying. Immutable `fdb6b9ee` under protocol
`1cda4a24` runs only43 never-claimed cases, all43 accepted with known usage and
Max included receipts,8.5949313USD nominal/zero API. All500 cases and source gaps
remain. All three32 cohorts stay sealed; no failed-case retry or443-consumer
restart occurs. The earlier122-only baseline remains76.81%/39.00% Recall/F1,
but is not a representative substitute for the full population.

The pinned public submission also re-scores all500 locally on the exact same
Gold/source/environment: line Recall/F1 is75.30%/25.70%, compared with native
72.83%/33.95% including its original zero-source failure. Recall is2.46points
lower and F1 is8.25points higher. Local validity is500/500 versus499/500. These
are different model/harness systems and context-only versus repair workflows,
not a core enhancement ablation, matched-cost result or current rank.

Public leader predictions match all122 IDs. The same local Gold, snapshots and
official environment score122valid on both sides: native line Recall/F1
**76.81%/39.00%**, versus openJiuwen+DeepSeek **79.24%/29.65%**. The native
baseline has lower Recall and higher F1; this is a cross-model/harness comparison,
not an enhancement gain. It uses CodeNib read/grep/glob and source/trajectory
adapters, with no indexed graph, vector/BM25 or Jev retrieval. The unstarted443 late4K consumers are cancelled, releasing the $40 reservation.
No new model/API calls are needed for these comparisons.

Component-specific indexed-context claims require actual indexed-tool evidence;
CodeNib system agent configurations do not require an index. The earlier
source-only `benchmark_context_core.py` consumer
builds vector/symbol-graph views through the existing compiler and registers
`explore_context_impl` in the actual SDK loop. The adapter returns only exact
complete projected source lines after content authentication; elided, partial
or index-only fallback text gets no source credit. No BM25/Jev, generic storage,
memory framework or explicit Haiku controller is added. The first source-sealed four-case development comparison has four valid cases per
arm: native versus optional core-tool Recall **71.61% → 73.69%**, F1
**33.68% → 40.92%**, source3,209→2,927lines and Max nominal1.1760282→1.1217879USD;
new API usage and repair calls are zero. This does not establish core benefit:
only JavaScript invokes the tool, and its Recall/F1 both decline. The other
three never invoke it. Python's graph times out at600seconds and Go has no
`scip-go`; only JavaScript/TypeScript have graphs. Preserve all four cases and
cold preparation costs; no favorable subset or independent-gate claim.

Independent main-based [#835](https://github.com/sysevol-ai/CodeNib/pull/835)
fixes exact query-symbol preservation and named-route source admission.
105 focused tests and same-query/index/source/budget replay pass without new
model/Gold calls. The original trajectories remain unchanged. The actual
metadata-after-read development iteration at `7db07141` combines that fix with
bounded authenticated compiler-reference hints and on-demand source bodies.
All4/4 cases per arm are valid, with thirteen actual graph-hint deliveries and
two unavailable-graph notices. Recall declines71.61%→68.24% versus native while
F1 rises33.68%→40.60%; source lines fall3,209→2,693 and Max nominal usage is
1.0819325USD. Only graph-unavailable Go calls the core tool, once. Reject this
policy for the Recall-first objective; no expansion or causal/independent gain
claim. Latest89 relevant tests pass, as do pinned hooks/strict MkDocs/public-docs
boundary checks. No API or repair calls. Warm exact-case cache preparation is
reported separately from original cold index builds, including Python's graph
timeout. Further work must improve useful discovery and source selection; adding
reference counts alone does not establish that outcome. No new generic memory,
archive or persistence framework is authorized by this experiment.
The subsequent surface-first prototype `95cbc503` passes its four-task
Recall-first development screen: Recall/F1 74.02%/42.40% versus original native
71.61%/33.68% and one predeclared post-hoc native repeat71.37%/31.76%. All four
actually query CodeNib surfaces,22 returned candidates/15 later-read locations,
but none uses body handles. Those body/freshness/dedup contracts are locally
tested; runtime body benefits remain unproven. Source falls3,209→2,610lines,
tool-reply estimates46,257→37,376tokens and Max nominal1.1760282→1.0091893USD.
The repeat costs1.1046278USD nominal and never replaces original500 results.
This exposed small-sample screen is not a causal or independent validation gate.
Latest99 relevant tests pass. SDK cache counters and full cold/warm preparation
remain separate from tool-reply estimates and API cash charges.
A supplementary source-checkout accounting helper deduplicates repeated
assistant response blocks and reconciles all93 response input records against
terminal SDK counters over the12 completed four-task streams. Missing/conflicting
usage cannot become zero or a complete maximum;62 usage/surface/SDK tests pass.
The mean per-task maximum single-response input is25,980tokens for original
native,24,965.75 for repeated native and22,048 for surface. This post-hoc input
measure preserves all scores/cost records and claims neither physical KV memory
nor cash savings. A separate local replay over seven ready real indexes checks
14 handles: exact source, zero metadata source credit, repeat suppression and
unchanged artifact bytes all pass. Those synthetic receipts are never agent
inputs and add no actual body calls; all selected locations fit one page.
An audit of actual four-task model inputs finds17 exact unsplit callable surface
ranges,12 subsequently read in full by native `read`, five unread and none
partially read. Reopening only these already-exposed canonical annotations finds
no missed annotation lines inside any surfaced range; the unread five overlap
none. Body-dump oracle coverage would add zero Recall on these traces, not a
measured policy gain. Missing-context discovery remains the development target:
104 missing annotation lines are in already observed files and121 in files
without dense L2 documents (overlapping raw-line counts, not task-macro scores).
Retain the one out-of-source Go annotation and all original denominators. No
pending dense8 policy/input/budget/source score changes follow this diagnosis;
fresh default-only labels remain untouched.

Keep the labs adapter source-only and its graph-optional pilot distinct from the
next eight-language dense-only ablation. The new vector-only preparation option
avoids an unnecessary graph build for that arm; authenticated prepared caches
must prove exact case/revision/snapshot identity, zero SDK events and no model
intent. No new product route, handle archive, generic persistence or cache-metric
migration is introduced. A read-only Jina ranking diagnostic retains original
artifact bytes and scores; mostly unchanged focused-query leaders do not justify
a product metric switch. The frozen eight-language/eight-repository ablation's initial source-only gate
fails on Ansible symlink aliases and the pilot's unadapted C label, with6/8 ready
and zero model calls. Preserve those failures and all selected positions. Generic
schema-8 discovery correction [#836](https://github.com/sysevol-ai/CodeNib/pull/836)
is independent/main-based and passes228 tests plus real Ansible replay, retaining
both regular targets and strict source validation. The pilot resolves C at the
dataset boundary through registry metadata, preserving the public factory
contract.179 feature tests and190 compiler tests pass. Runtime `85b7f0d2` rebuilds
only the two failed preparations under source-only correction `4ddf8f93`; the
six canonical successful artifacts stay intact. That source correction also
fails before models: C computes16,536 L0/396,364 L2 embeddings, then its canonical
documents exceed the256MiB save limit after2,545.82seconds. Seven of eight
preparations are ready; the audit stops before live-protocol freeze or any agent
dispatch. Keep all eight IDs/source scope, both failed gates and cold costs;
there is no expanded agent quality result. A full-producer admission fix checks
the exact canonical bytes for all requested levels before initializing any
embedding model, preserving the limit/save guard/format/publication boundary.
93 index and448 compiler tests pass. Independent main-based
[#837](https://github.com/sysevol-ai/CodeNib/pull/837) passes539 tests and fresh
required unit CI. The same complete C corpus rejects the oversized payload
in243.37seconds with zero embedding-model initializations; that admission probe
is separate from the failed experiment. #835/#836/#837 remain ready, unmerged
production dependencies with successful required unit CI.

The labs file/function policy reuses authenticated source reads, registered
per-file tree-sitter chunking and surface/body handles without a product route,
persistent index or provider.66 underlying context checks pass. Initial source
protocol `b8df97c5` prepares all eight unchanged source snapshots; C retains107,570
files and takes45.81seconds on the existing filesystem. This is not matched
cold-machine timing. Its first two SDK attempts fail before assistant/tool output
on an omitted legacy allowance field, with six positions unclaimed. Preserve
all eight outcomes and both unknown terminal fees. The narrow typed/raw receipt
fix passes75 tests and both actual-event replays, retaining rejection of paid,
missing or conflicting evidence.

Receipt-corrected optional policy `b51f7dc1` completes8/8 with zero context calls.
Primary-handle policy `aa32ebf0`, runtime `5b79410a`, is frozen from non-adoption
before current annotations are reopened; two SDK delivery checks pass. Both
all-eight source arms seal before common official evaluation. Native versus
optional versus primary line Recall/F1 is69.91%/39.66%,70.06%/38.72% and
69.40%/37.35%, each8/8 valid. Primary has7/8 surface adopters and1/8 body adopter,
zero tool errors, and15.58% more mean per-case maximum provider input. The one
actual body returns164 new lines while omitting one prior grep line. Its declared
body-adoption and Recall gates fail. This file-outline policy stays a rejected
development experiment; it is not promoted to product, independent32 or500.
Ordinary agent source tools remain the working baseline, with embedding/graph
optional only for a concrete failure and measured benefit. Earlier indexed-four
positive evidence, all source gates/costs and the two unknown failed-session fees
remain separate. New16 Max sessions report$3.2396939 nominal, with zero new
API-key/repair usage. Fresh default-only validation, candidate500, native outcomes
and submission evidence remain open.

Current ContextBench agent milestone: implemented and measured in draft
[#834](https://github.com/sysevol-ai/CodeNib/pull/834), lifecycle `labs`.
CodeNib is the system, including native source tools, agent execution, context
delivery and model routing. Embedding and graph are optional. Native agent
configurations belong to CodeNib; component-specific causal claims need matched
ablations. Product lifecycle and dependency direction stay unchanged.

The fixed native500 control is complete: line Recall/F1 **72.83%/33.95%**, with
499 valid and the original empty-source failure retained as zero. Raw and input
audit archives reproduce the captured controls, including the earlier122-case
annotation-timing disclosure and unknown terminal fees. This is a CodeNib native
system baseline, not an indexed-context gain or official submission. Verified500
is now exposed. After the completed independent32 comparison,591 metadata-only
default candidates remain, excluding closed cohorts and issue/source aliases.

The current before-search router at `0fc02a85`, protocol `712569fe`, selects one
actual native Haiku/Opus source-search worker from the issue alone. Worker
prompt/tools/medium20turn/4096output and per-model budgets stay unchanged.
Known-fee capped/malformed classification defaults to Haiku without retry;
unknown model/usage/auth/source integrity rejects. All25 router tests and166
focused checks pass without billed tests. All8 exposed development tasks execute,
seal source before labels and score valid: **71.04% Recall/40.17% F1 at
$0.044898795**, versus original Haiku68.07%/36.66% at$0.043406125 and
Opus69.91%/39.66% at$1.4261087. All8 workers are Haiku, so the mixed-router screen
fails. No quota is forced. Differences from the original Haiku run are stochastic
repeats, not evidence that the router selects better workers. Main-input
completeness remains unknown because of same-model CLI auxiliaries. Keep all24
case rows and known all-model costs in the evaluation report.

The independent CodeNib system comparison is complete. Design `cab3bc5a`,
metadata selection `059fec72` and execution protocol `63d57a66` retain32
default-only tasks, four per language across23 repository-alias clusters and
at most two per cluster. Selection precedes issue/source/label access; all32
source preparations pass before inference and all96 worker source outcomes seal
before selected Gold opens. All96 execute and score valid, with known fees and
no retries or replacements. Native Haiku Recall/F1 is **45.17%/28.63% for
$0.252892905**, native Opus **49.86%/33.58% for$6.218777900**, and the unchanged
issue-only router **44.11%/28.40% for$0.898562665**. Routing is29 Haiku/3 Opus.
All predeclared quality gates fail. The router is dominated by Haiku on point
Recall/cost; Haiku and Opus are nondominated among these three only. Router minus
Haiku Recall has a repository-cluster95% interval[-3.49,+1.71] points, so do not
claim a reliable harm or a demonstrated gain. No failed policy expands to500.
Default-only language balancing differs from verified500; this is not a new
leaderboard score. Keep all96 public case rows and aggregate/bootstrap receipts.

The completed32 supports a post-seal retrieval diagnosis, without changing any
official score or reopening it for tuning. Only140/22,359 annotated lines are
unavailable in the base source. Of the original Gold denominator, native Haiku
observes45.17%;37.16% remains unread in files with some source already observed,
16.73% is in unlocated files and0.94% is unavailable, averaged by task. All96
observed-source intersections match official reports. This motivates a real
candidate-selection/on-demand-reading experiment with a shared source organizer,
not another model-only router sweep or a promise to recover the whole gap.
Keep closed tasks excluded from new independent validation and distinguish
source relevance routing from predicting which model will search successfully.

Live two-worker runtime `71e489e5`, protocol `f2bba747`, now passes its separate
development screen on the same eight exposed tasks. All32 actual worker sessions
and32 configuration positions score valid after source sealing. One common
first Haiku search supports both matched pairs; the independent repeat keeps
the original issue, while the complementary worker gets only the issue and
version-bound navigation metadata. Its2,048-token metadata cap is not a source
body cap. Real worker source reads feed the same source assembler; metadata and
assembly add no invented read credit. No parent reread, graph, embedding, Jev
request or repair is required by this policy.

Native Haiku Recall/F1 is **68.83%/32.26% for$0.046606145**; two independent
Haiku searches **70.79%/30.17% for$0.085153860**; the complementary pair
**71.62%/29.29% for$0.100641385**; fresh native Opus **70.56%/39.69% for
$1.119668300**. Both pairs pass Recall-within-two-points and at-most25%-cost
development targets. The complementary pair's0.83-point increment over the
independent pair has95% interval[-3.69,+5.26]; its1.06-point increment over Opus
has interval[-4.51,+6.65]. Retain the substantial F1 tradeoff and all32 public
case rows. This is a low-cost candidate, not stable independent, repair or
leaderboard evidence. It also does not establish fewer reads or better context
density: the complementary pair observes6,548 unique source lines versus5,839
for the independent pair and3,686 for Opus, summed per task. Cross-worker
repetition is3,979 versus3,141 lines. The scout policy bundles navigation metadata
and a complementary-search instruction; this contrast does not isolate metadata
alone. That development screen alone does not authorize500 or reopening the
earlier closed32 for tuning. Fifteen new source-contract tests and56 combined
focused checks pass; all eight historical source-union identities reproduce
without new inference.

Successor independent design `bcaf5253`, metadata selection `34023146`, freezes
the same runtime/policies and selects32 new default-only tasks, four per language
across22 repository-alias clusters with at most two per cluster. The591-task
metadata-only pool leaves559 after reserving this cohort and aliases. All32
exact-source checks pass with empty issue/Gold fields before selected issue-only
projection. Execution protocol `914a4a4f` completes128 SDK searches with the
unchanged development parameters. Four synthetic postprocessor fixtures verify
the source seal precedes Gold and source/fee/duplicate failures block grading.
All128 source outcomes seal before selected Gold; every configuration scores32/32
valid. Native Haiku Recall/F1 is **33.98%/26.53% for$0.250863650**, the independent
pair **38.82%/27.82% for$0.480771855**, the complementary primary
**40.00%/27.18% for$0.499195665**, and fresh Opus **37.91%/31.15% for$6.427971300**.
The primary passes the frozen Recall/cost gate and stronger point target at7.77%
of Opus cost. Its2.09-point Recall difference versus Opus has95% interval
[-1.91,+6.48]; its1.17-point difference versus the repeat pair has[-2.41,+5.12].
Neither stable superiority nor metadata-alone causality is established. The
128-row public CSV/summary retain all file/symbol/line metrics and fees. Source
volume/repetition rise:30,640 unique and16,234 repeated lines for the primary
versus24,341 unique for Opus and14,322 repeated for the independent pair. All128
source intersections reconcile; unavailable Gold stays in the denominators.
Only the predeclared primary may advance to500. Current8 results and the earlier
failed32 remain immutable; no new repair batch starts. Candidate500 still needs
a frozen execution guard and complete live source authentication.

The earlier late-critic policy remains rejected:68.90% Recall/41.14% F1 at
$0.113543820, one Opus review and zero follow-up searches. Preserve both earlier
stopped integration cohorts and their full denominators/costs. Callable-tail and
file-outline policies remain rejected; no rejected policy expands to32/500.
Detailed historical traces and every failure remain in
`docs/evaluation/contextbench-results.md` and immutable receipts.

Production fixes remain independently reviewable and unmerged:
[#835](https://github.com/sysevol-ai/CodeNib/pull/835) named-symbol routing,
[#836](https://github.com/sysevol-ai/CodeNib/pull/836) regular lexical discovery,
[#837](https://github.com/sysevol-ai/CodeNib/pull/837) canonical-document
pre-admission, and [#838](https://github.com/sysevol-ai/CodeNib/pull/838)
query-focused source delivery inside long anchors. Their recorded exact required
unit CI succeeds; #838 has298 local checks with7 skips and unchanged-source MUI
replay. This proves delivery, not ranking or agent-quality gains. #834 remains
draft with its required unit gate skipped/incomplete; do not merge it prematurely.

Independent32 adds **$7.370233470** in known nominal API usage; the$16 reservation
closes with$8.629766530 of budget released. Known cumulative usage is
**$66.914800665/$100** immediately after that cohort. The live-pair development
batch then adds **$1.258857400** for its32 distinct sessions, counting the common
first worker once in the program ledger. Its$10 reservation closes with
$8.741142600 of budget released. The completed complementary-search independent32
adds **$7.157075170** for128 distinct SDK sessions. Its$12 reservation closes,
releasing$4.842924830 of budget. Current known usage is **$75.330733235/$100**,
leaving **$24.669266765** with no active reservation at closure. Language-weighted
500 search cost is estimated at$5.905410835; the observed-maximum-per-language
scenario is$8.702784340. These are arithmetic scenarios, not caps, and exclude
repairs/compute. Readiness checks verify1,499 original500 files and500 matching
Git commits; full live source authentication remains required. Historical
unknown Max fees remain unknown; nominal usage is not a reconciled cash bill.
Native solver controls, candidate500,
reproducible candidate/outcome archives and official submission remain open, but
no new repair batch starts. The unstarted443 late4K consumers remain cancelled.
No package lifecycle promotion, storage boundary or mandatory embedding/graph
path is authorized by these system experiments.

```text
source and manifest contracts
  -> view builders and language backends
  -> repository runtime and query executor
  -> product adapters (CLI, MCP, Wiki, Python API)

Wiki consumer -> WikiStore -> SQLiteWikiStore
manifest-bound views -> BM25 / FAISS / igraph / portable file artifacts
source-only H1 experiment -> published artifact contracts
labs and experiments -> published contracts only
```

`web` must not own Wiki domain behavior, MCP must not own the reusable
repository runtime, and `compiler` and `artifacts` must not import each other.
Generic catalog, snapshot, job, lease, and object-store machinery is not on a
protected product path. The bounded H1 experiment is evidence-gathering outside
the installed package, not another product runtime.

## Workstreams

### Agent Retrieval Bench labs boundary

Status: implemented and locally verified in
[PR #831](https://github.com/sysevol-ai/CodeNib/pull/831).
Owner: retrieval and evaluation maintainers. Lifecycle: `labs`; no stable
package re-export or product-route dependency.

The initial surface covers the four positive static releases only. Dataset
code owns released samples and corpus identity; eval code owns BM25 execution,
checkpointing, external ranking imports, and the pinned upstream metric bridge.
This uses the same dataset/eval ownership boundary as ContextBench and adds no
generic benchmark, storage, or index registry. Official scoring remains an external-checkout test
tier; unit tests make no billed calls.

Outcomes: 345/345 samples and 294 release/snapshot pairs pass preparation;
8/8 real cases pass retrieval and official scoring, with 64/64 metric cells
and packing details matching offline rescoring. With both benchmark adapters
present, the related unit tier passes 1,062 tests and ten integration tests run
against the pinned ARB and ContextBench evaluators. Public documentation records
the protocol, scope, and reproducible smoke receipt.

Complete-release counts are enforced before selection; native recovery verifies
an atomic checkpoint receipt and rebuilds exported predictions. CLI upstream
failures use a concise exit code, and hidden evaluator edits are rejected.

Open research gates: a full 345-case retrieval measurement,
selective/abstention, interactive trajectories, graph/Zoekt/vector adapters,
and upstream submissions remain subsequent scope decisions. The next proposed
comparison holds the agent model and budgets fixed and tests iterative search with and without
graph expansion; full-source access must be distinguished from corpus-only
retrieval.

### S0: High-confidence subtraction

Status: in progress.

- [x] Remove the unreferenced `codenib.agent.utils` module.
- [x] Retire the rejected per-file native Python chunk POC.
- [x] Retire the rejected repository-batch chunk gate implementation while
      retaining its recorded result in the SCIP roadmap.
- [x] Remove every build switch, environment variable, test, profiler, and
      active documentation instruction owned only by those experiments.
- [x] Retire the unowned RFC #133 retrieval comparator and manual skill smoke,
      plus the standalone metric recomputation superseded by
      `codenib.eval.retrieval_eval`.
- [ ] Inventory scripts by command entrypoint, module consumer, documentation,
      tests, and lifecycle owner; a missing literal path reference alone is not
      dead-code evidence. Delete confirmed orphans in independently reviewable
      groups.
- [ ] Retire the failed, default-off SCIP MCP consumer provider and its gate
      without removing the promoted `FactQueryIndex` and clangd consumers;
      retain the exact negative M2 receipts in the SCIP roadmap.

### S1: Wiki-only database boundary

Status: complete.

- [x] Declare the Wiki-only `codenib.storage.WikiStore` facade and its SQLite
      WAL adapter as the only product database boundary.
- [x] Keep `RepoManifest`, BM25, FAISS, igraph, and portable context payloads
      as manifest-bound file artifacts rather than catalog records.
- [x] Remove the optional Web `index_storage` configuration, retained job
      orchestration, reconciliation, and runtime-activation path.
- [x] Freeze the generic v0.2.2 `codenib.storage` contents and explicit
      retained-storage CLI commands while their consumers and release boundary
      are audited; add no
      PostgreSQL, S3, generic GC, or backend discovery.
- [x] Remove the unreleased audit, direct MCP, index dual-write, benchmark-gate,
      durable-execution slices, and the two unreachable retained BM25
      generation plan/replay helpers left by that execution stack. The orphan
      closure removed 231 production lines, zero test lines, zero scripts, and
      zero public entry points. At that stage, the frozen CLI surface contained
      only the two published `artifact import-cache` and `artifact materialize`
      compatibility bridges.
- [x] Use v0.2.3 as the removal boundary for the complete frozen generic
      surface: replace the package with a single Wiki-only
      `codenib.storage` facade, and delete the two retained artifact commands,
      retained compiler bridges, and catalog-backed FactBatch publication
      experiment. Existing catalogs must be materialized with a pinned v0.2.2
      environment before upgrade.
- [x] Move portable-artifact validation contracts into the artifact-neutral
      `_bounded_json` module so default Web imports no longer cross a generic
      storage namespace.
- [x] End the v0.2.3 Wiki JSON migration window by removing legacy cache
      lookup, parsing, writes, adoption, file locks, provenance bookkeeping,
      and file-based audit accounting. Keep existing SQLite rows readable and
      make a missing `WikiStore` explicitly memory-only. The closure removes
      354 net production lines and 256 net test lines while deleting zero
      scripts and zero public entry points.
- [x] Bound each built-in Wiki generation-lock wait to 30 seconds without
      stealing the owner's lock or duplicating generation. Copy a checked,
      quiescent SQLite database into a private immutable snapshot for cache
      audits and prewarm dry runs; reject WAL, SHM, rollback-journal, or source
      changes during capture without adding a public constructor option or
      another Wiki protocol method.

Exit condition: protected product journeys use only the Wiki database facade
and manifest-bound artifacts; no generic storage surface remains.

### S1E: Time-bounded hybrid-index evidence

Status: experimental through 2026-10-04.

Owner: index/artifact maintainers.

- Keep the candidate under `scripts/experimental/hybrid_index/`, outside the
  installed `codenib` namespace and every default product import.
- Accept exactly one verified BM25-only portable artifact and use exactly one
  SQLite WAL catalog plus local SHA-256 CAS implementation.
- Add no stable export, official CLI/configuration, Web/MCP route, backend
  registry, additional view, job, lease, GC, overlay, or remote adapter.
- Treat a real non-test consumer, a representative benchmark, portable
  round-trip compatibility, and the recorded failure matrix as promotion
  prerequisites rather than follow-up work.
- By 2026-10-04, make an explicit promotion decision or delete the
  implementation, executable script, and dedicated tests while retaining the
  evidence record.

Exit condition: H1 is either promoted by a new scope decision that names the
old path it replaces, or fully removed. Source-only and default-off code does
not receive an indefinite exception.

### S2: Stable versus labs packaging

Status: pending.

- Correct the package-discovery boundary: `exclude = ["eval*"]` does not match
  the shipped `codenib.eval` namespace, and stable console scripts currently
  expose evaluation and benchmark commands. Treat their removal as a
  release-managed compatibility change rather than incidental cleanup.
- Classify `eval`, `dataset`, `clients`, `integrations`, `serving`, legacy model
  pipelines, and graph incremental patching.
- Move research console scripts and dependencies out of the default wheel.
- Preserve upstream compatibility claims only where a maintained product or
  labs package owns their verification.

Exit condition: a normal CodeGraph or Wiki install does not ship research-only
commands or speculative model serving.

### S3: One repository runtime

Status: in progress.

- [x] Make Web graph eligibility use canonical `RepoManifest` freshness and
      remove its weaker local implementation and the unusable legacy vector
      sidecar discovery path.
- Define the supported `RepositoryContextExplorer` context protocol, migrate
  duck-typed consumers to canonical manifests, and only then remove its
  status-only freshness compatibility fallback.
- Extract one neutral manifest/source/view loader from MCP `ServerContext`, Web
  `RepoRegistry`, and `compiler.skill_context`.
- Make MCP, Web, Wiki, and `RepositoryContextExplorer` adapters over that
  runtime.
- Delete the superseded loading and authorization paths in the same program.

Exit condition: BM25, vector, graph, source authority, and cleanup semantics
have one implementation and one conformance suite.

### S4: One query executor

Status: pending.

- Centralize sparse/dense execution, fusion, graph expansion, reranking, and
  source validation behind one executor.
- Keep Wiki page diversification as a policy over the shared executor rather
  than another retrieval engine.

Exit condition: MCP search, native exploration, and Wiki evidence cannot drift
on the same plan and manifest.

### S5: Restore ownership boundaries

Status: pending.

- Move page graph projection out of `web` so Wiki does not import a Web adapter.
- Give publication contracts one owner and remove the
  `compiler`/`artifacts` package cycle.
- Split the CLI into command modules after the underlying ownership boundaries
  are stable.
- Add repository-wide import-boundary tests for the target direction.

### S6: Repeated workflow extraction

Status: pending.

- Replace repeated chunker traversal with declarative grammar profiles and
  narrow language hooks.
- Share dataset checkout/cache/filter machinery.
- Share strict view-publication state machines while retaining format-specific
  validation.
- Share agent-client lifecycle adapters without weakening SDK-specific safety.

## Decision Log

| Date | Decision | Evidence retained | Implementation disposition |
| --- | --- | --- | --- |
| 2026-08-24 | Begin the cognitive-debt program with pure subtraction | This baseline and the SCIP acceleration receipts | First subtraction iteration opened |
| 2026-08-24 | Treat failed Python chunk acceleration candidates as closed experiments | Exact revisions, parity, timings, RSS, and gate decisions remain in `scip_multilanguage_roadmap.md` | Remove POC/gate code, ABIs, commands, tests, and active instructions |
| 2026-08-24 | Retire three unowned retrieval evaluation scripts | RFC #133 remains closed and canonical retrieval metrics remain covered in `codenib.eval.retrieval_eval` | Remove the transitional comparator, manual smoke, and duplicate metric recomputation |
| 2026-08-24 | Start runtime convergence at manifest freshness | `RepoManifest.index_is_current` already owns commit, fingerprint, and source-selection identity; authenticated graph loading requires a `symbol_graph` entry | Delete the weaker Web-only freshness rule and the unusable vector-sidecar discovery path; add rejection regressions |
| 2026-09-01 | Make WikiStore the only product database and stop the generic backend program | Storage audit counts, protected-journey consumer tracing, and the one-table Wiki conformance results are summarized in `storage_backend_roadmap.md` | Remove the Web retained-storage vertical; freeze generic storage/CLI compatibility code and delete it by proven consumer; close PostgreSQL, S3, generic GC, and dynamic-registry plans |
| 2026-09-02 | Use the next release as the generic-storage removal boundary | The consumer audit found only the two retained artifact bridges and one catalog-backed FactBatch profile; ordinary product routes were independent | Delete the complete generic package and callers; keep v0.2.2 only as the pre-upgrade materialization tool |
| 2026-09-03 | Publish the subtraction as v0.2.3 and reserve `codenib.storage` for the Wiki database | `WikiStore` is the only product database contract, while the old package name remains the clearest public entry point | Add one lazy Wiki-only module with an exact export list; keep all generic submodules, protocols, catalogs, and CAS implementations retired |
| 2026-09-03 | Authorize one source-only BM25 H1 evidence experiment without reopening the stable storage boundary | `docs/experiments/hybrid_index_h1.md` records its schema, linearization points, verified failure matrix, commands, and diagnostic benchmark | Keep it outside the wheel and product routes; promote through an explicit demand-backed decision or delete code, script, and tests by 2026-10-04 |
| 2026-09-05 | End the one-release Wiki JSON compatibility window | The v0.2.3 release notes and #749 record the migration window; production compositions already inject the Wiki store | Delete the legacy implementation and compatibility tests; preserve SQLite data and the `WikiStore` API |

## Completion Gate

The program is complete only when:

- protected product journeys pass packaged acceptance;
- stable code has one persisted-view loader and one query executor;
- package dependency direction is enforced without lazy-import cycles;
- protected product paths do not import or configure generic storage machinery;
- every non-core surface has a lifecycle class and owner;
- failed experiments leave durable evidence but no dormant implementation;
- stable installation, public API, and required CI exclude labs-only work.
