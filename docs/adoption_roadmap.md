# Adoption and distribution roadmap

Owner: CodeNib product, retrieval, and documentation maintainers.
Reviewed: 2026-10-31 (Pacific). Status: active.

## Outcome and product decision

A new visitor should understand why CodeNib helps their coding agent, see
source-linked output, and try it on their own repository. Public Wiki pages
are a shareable preview and an acquisition channel. The primary activation
event is a successful, source-linked agent query on the user's repository.

The immediate product priority is A0: establish whether CodeGraph improves
real coding tasks after setup, indexing and source-update costs are included.
The pilot targets at least 20% improvement in a preselected task-time or
model-usage endpoint with no reduction in task acceptance. It must support
an evidence-based promotion, narrower product scope, or no-go decision.

Model-planned grep followed by Jev remains a candidate retrieval path under
A2/A3, using the user's OpenRouter account. It must not require embeddings,
a GPU, or a completed graph before the first query. Typed graph navigation
remains supported when its language provider is ready. Promoting an onboarding
default requires its own matched product evidence; neither the grep/Jev
research result nor graph-free ContextBench outcomes settle A0.

The shipping `codegraph init` path and the experimental grep/Jev result must
remain clearly distinguished until that route passes the product gates.
Using the caller's agent to plan grep is a separate, unevaluated variant.

## Audited starting point

| Surface | Current outcome | Open gate / dependency |
| --- | --- | --- |
| Agent setup | Released `codegraph init` prepares typed graphs. Source-only `codenib init` now connects OpenRouter and registers grep/Jev through native Claude Code/Codex CLIs, merged in #785. | Published 0.2.4 installation guides use the package; promote OAuth setup after actual provider-consent acceptance. |
| Retrieval evidence | Historical research scores 58.62% → 71.40% Recall@5. Fresh product evaluation in merged [#787](https://github.com/sysevol-ai/CodeNib/pull/787) scores 48.37% → 65.57%, counting one failed case as zero across all 100 attempts. | Keep the two runs distinct; neither measures Claude Code token savings. The multiline correction is separately tracked in #792. |
| OpenRouter | Shared grep/Jev configuration and local PKCE are merged in #782/#783. The opt-in browser trial uses direct provider calls and a short in-memory credential; real-source browser retrieval acceptance uses provider fixtures. Published 0.2.4 CLI retrieval and an 11-chapter visitor Wiki pass with the maintainer’s existing OpenRouter key. Real Linux SecretService and macOS/Windows vault round trips pass with fake entries. | The maintainer chose desktop CLI consent acceptance first; interactive unlock and hosted browser acceptance remain. |
| Wiki demo | Source-grounded stories/graphs and static cached-story publication are merged in #772/#784. The real 15-page Requests corpus passes offline desktop/mobile browsing. An opt-in browser trial adds source-linked grep/Jev results without an operator key. | The maintainer deployed the live Wiki. The #801 short-explanation path is superseded by full visitor Wiki generation, persistence and progress; actual PKCE consent remains unverified. Live operator browsing/Ask can still intentionally generate. |
| Live Wiki cache and maps | Runtime fixes in [#800](https://github.com/sysevol-ai/CodeNib/pull/800) batch authenticated source reads, retry busy generators, and build System Maps from cached outline files plus indexed references before child prose exists. Overview illustrations require admitted architecture plans; unrelated call-flow fallbacks are omitted. Deployed desktop/mobile checks pass, with maps available for 23/27 repositories. After full prewarming, the 2026-09-26 audit reports 1,001 ready pages, 19 degraded pages, and no cold pages across 1,020 entries. | jq/MicroPython graph artifacts fail captured-source validation; Axios/Preact graph coverage lacks usable cross-area implementation references. Repair these four index artifacts and resolve the 19 page-quality failures before declaring the whole demo ready. These data-quality gates remain separate from the runtime fixes. |
| Static distribution | Static export and reusable Pages workflow exist; #784 verifies cached stories, citations, page maps and CodeNib backlinks without a backend. A 15-page public repository is locally curated. | The no-embedding workflow default is released in 0.2.4; the maintainer will deploy the static roots on DGX Spark later. |
| MCP protocol | [#780](https://github.com/sysevol-ai/CodeNib/pull/780) merged as `bcd2c730` after all executed CI passed; its squash message was verified. [#779](https://github.com/sysevol-ai/CodeNib/issues/779) is closed. | Released in 0.2.4; the public PyPI wheel passes installed modern and legacy MCP identity checks. |
| GitHub discovery | `mcp`, `mcp-server`, `claude-code`, and `codex` are set, with the six existing topics retained. The About description now names source context, call navigation, Claude Code, Codex and MCP. | Description, topics and unchanged homepage verified through GitHub metadata on 2026-09-26. |
| Releases | [v0.2.4](https://github.com/sysevol-ai/CodeNib/releases/tag/v0.2.4) is published on PyPI, the official MCP Registry and GitHub, marked latest. Tag and accepted TestPyPI commit are `3f3bc2cf44ba83d1d86e9e5eb74f8da0f8bc63ac`. Both wheels and the sdist match across PyPI, GitHub and SHA256SUMS. | Protected production run [36252998442](https://github.com/sysevol-ai/CodeNib/actions/runs/36252998442) passed. Post-publication docs #798 update installation pins and navigation. Current-source agent presets select LiteLLM's coupled MCP transport dependencies, and installed Ask runs on release PRs as well as full main builds; this does not republish 0.2.4. Real consent and maintainer-owned website deployment remain separate gates. |
| Related work | #777/#778 improve the Jev blog evidence; #773 changes blog layout. | Preserve their ownership; do not duplicate their chart or hardware corrections. Merged #770/#752 supply optional Wiki image generation and contextual repository diagrams; they are independent of the visitor/adoption acceptance gates. |

## Delivery order and acceptance gates

### A0 — Prove CodeGraph's net benefit in daily coding

Status: active; hold promotion. Established 2026-10-26 (Pacific), with target
decision review 2026-11-09. The completed
[source-payload confirmation](experiments/codegraph_payload_results.md) covers
three paired repositories, twelve workflows, sixty tasks, 48 exact-source
judgments and twelve original native grades. Native / Serena / Codegraph /
CodeNib accept 11 / 10 / 8 / 10 tasks out of fifteen. Every arm passes two of
three repairs; all 596 original selected outcomes execute. No complete
eligible alternative exists, so the frozen decision is insufficient evidence.
This supplies zero independent success confirmations.

The repository-wide style gate is repaired in merged
[#947](https://github.com/sysevol-ai/CodeNib/pull/947), with the pinned hooks and
160 targeted unit cases. Eleven Python files preserve their executable AST
and prompt literals, allowing only removal of a redundant exception alternative
and its unused import. Generated homepage bundles retain their original bytes.
All 17 executed PR checks pass, with eight conditional/publication skips.
[#946](https://github.com/sysevol-ai/CodeNib/pull/946) is merged as `ad87b872`;
its [current installed-package admission](experiments/codegraph_python_wildcard_reconciled_2026_10_31.json)
passes actual compiler/MCP, post-edit rebuilding and full-source preservation.
Only the four verified style-base modules differ from the earlier wheel;
all four complete-source artifact hashes remain unchanged.
[#945](https://github.com/sysevol-ai/CodeNib/pull/945) is merged as `90d4a8ed`;
its [combined installed admission](experiments/codegraph_source_definitions_reconciled_2026_10_31.json)
passes MCP/fallback, lifecycle and complete-source controls. All 21 executed
final-head checks pass with eight explicit skips; the merged tree equals the
reviewed head. These bounded checks do not close the workflow-benefit gate.

CodeNib's observed complete time is 738.588 seconds versus Native 558.727 and
Serena 666.964, with no recorded CodeNib or Serena product call. Against Native,
CodeNib takes 32.2% more time and 21.0% more normalized tokens; against Serena,
10.7% more time and 0.7% more tokens. These descriptive observations do not
identify a causal tool effect. The unchanged promotion gate requires quality
parity, no additional serious error, at least 20% less complete time against
the strongest complete eligible alternative and at most 10% normalized-token
regression. Keep setup, update, failure and retry costs and every rejection.
One unblinded maintainer-agent reviews source; no population quality claim or
independent adjudication is established.

The installed candidate is merged
[#931](https://github.com/sysevol-ai/CodeNib/pull/931), with all 485 packaged
Python files and native bytes admitted before inference. Short source windows
and unused-budget reuse are verified within the original caps; they do not
establish model-token savings or task benefit. Main Docs, lightweight install,
unit/integration and all twenty release verifiers pass. Conditional
serial/native/consumer skips do not repeat the full original chain executed
for #917. Public package 0.2.4 is unchanged.

The [unchanged prospective protocol](experiments/codegraph_payload_confirmation.md)
binds 5,294 exact inputs before inference; the separate
[results packet](experiments/codegraph_payload_results_2026_10_30.json) retains
535 original output records, all source/native judgments and source-derived
counterexamples. All sixty SDK tasks terminate with complete usage, and all
ninety named current/retained containers are independently absent. No reference
patch or grading feedback enters a model. No case, test, criterion, word limit
or denominator is replaced after observing outcomes.

| Retained evidence | Current interpretation |
| --- | --- |
| Original [v1](experiments/codegraph_matched_assessment_v1.md) and [v2](experiments/codegraph_matched_assessment_v2.md) | Preserve every judgment, timing and the original v1 criterion ambiguity. Neither establishes the frozen complete-workload advantage; CodeNib remains unused in v2. |
| [Exposed development](experiments/codegraph_development_run_v1.md) and permanently stopped [task-fit cohort](experiments/codegraph_task_fit_result.md) | Development is separate from independent success. Retain stopped execution and unknown usage; never resume or rerun that cohort. |
| [Source-first development](experiments/codegraph_source_first_results.md) | All 60 tasks, 1,352 native outcomes and 48 source judgments complete; 23 explanations and 9 repairs pass. Accepted totals 9/7/8/8; no eligible complete alternative. Neither independent success confirmation. |
| [Internal adoption diagnostic](experiments/codegraph_guidance_adoption_results.md) | Two voluntary BM25-only calls but 0/2 fully accepted answers; no comparison, graph benefit or external participant. |
| [Source windows](experiments/codegraph_complete_source_windows.md) and [installed guidance](experiments/codegraph_installed_guidance.md) | Verify original source delivery, installed identity and unchanged metadata/routing. Component correctness does not prove whole-task savings. |
| [Prospective source-payload result](experiments/codegraph_payload_results.md), [#932](https://github.com/sysevol-ai/CodeNib/pull/932) | Full four-arm outcomes retained; 31/48 source explanations and 8/12 repairs accepted. Hold promotion; no complete eligible alternative and no CodeNib call. |

Dirty initialization, stale-query rejection, source-current explicit reloads,
scoped Python identity and cold deferred source-first exploration are merged.
The [Python identity controls](experiments/codegraph_python_identity.md),
[recovery delivery](experiments/codegraph_mcp_recovery.md),
[reference semantics](experiments/codegraph_reference_semantics.md) and
[SDK spill delivery](experiments/codegraph_sdk_output_delivery.md) remain bounded
correctness evidence. Explicit graph requests retain real provider preparation,
resource failures and update costs. BM25/native fallback is not typed-graph use.
The three latest confirmation sources are now exposed and stay frozen; fixed
Matplotlib/Django/xarray issue, source and grading material remain unopened.

The [Python identity repair](experiments/codegraph_python_walrus.md)
is merged in [#933](https://github.com/sysevol-ai/CodeNib/pull/933) as `d32b073c`.
It repairs both reproduced
Pylint construction blockers: an unqualified module variable and a decorated
field write corroborated by an independently declared field in the same LSP
response. An installed two-module control admits all 878 selected documents,
with partial indexing disabled, and returns four graph-backed source replies.
All 28 windows match original source bytes; the two `_compute_sims` probes and
one `_check_astroid_module` probe include all five returned caller locations.
The original ambiguous `add_message` reference reply remains recorded; exact
names resolve it, with one result capped at 40. Full construction and these
selected source checks pass. The [exact CI wheel](experiments/codegraph_python_identity_wheel_2026_10_30.json)
repeats the full admission with all 485 packaged Python files and its native
extension verified, without source overlays. Actual model use, workflow
quality/time, independent confirmation and external reuse remain open.
No completed cohort, test selection or score changes.
Main's unit, integration, serial integration, SCIP/core and graph-consumer
checks pass, along with all twenty executed release verifiers. Slow and
publication-only skips remain explicit; no public package is republished.

The [source-contract adoption diagnostic](experiments/codegraph_reference_adoption_freeze_2026_10_30.json)
binds 24,225 inputs before one exposed Pylint question. Its
[completed assessment](experiments/codegraph_reference_adoption_results_2026_10_30.json)
records four native Grep and four Read calls, zero CodeNib calls and no typed
graph preparation. The core contract/callers are correct, but the full answer
fails the unchanged <450-word gate at 463 words and recommends a test order
that clears its appended inputs. SDK connection and tool inventory are verified;
actual HTTP tool visibility and choice causation remain unknown. All four owned
containers are independently absent and every frozen input is unchanged. This
single product condition supplies no comparative efficiency, independent success
or external-use evidence. Stop repeating paid exact-name probes; establish a
real workflow/discovery need before another efficacy cohort. The reserved set
stays unopened.

The [SDK loading audit](experiments/codegraph_sdk_context_audit.md) completes
two metadata-only sessions without submitting a model question. In the fresh
CodeNib connection, the client reports `explore_context` loaded and accounts
for server instructions; its offline estimates add 1,042 context tokens.
All 54 observed proxy CONNECT attempts to Anthropic are denied, so these are
not API-measured counts or savings. Both owned containers are absent, the copied
source is unchanged and all 24,225 sealed inputs reverify. Historical HTTP
schema exposure and model-choice causation remain unknown. This connection
does not support a missing/deferred-tool diagnosis. Prioritize a real
source-linked cross-file maintenance task with a verifiable edit/test outcome
over further discovery rewrites or paid exact-name probes. Internal use does
not satisfy the external-user gate.

The [internal source-update maintenance control](experiments/codegraph_internal_maintenance.md)
uses the installed wheel on CodeNib's complete selected 1,172-document Python
surface. Both full graph builds pass with 33,178 nodes and 159,676 edges;
26 returned source contexts/pointers match their captured versions. The
README/docstring correction preserves changed-source rejection and same-session
recovery. Source queries take 0.787s initially and 1.491s after a 3.360s BM25
rebuild; first and post-edit graph requests take 210.506s and 219.915s, versus
0.614s on a prepared graph. Two operator argument errors remain recorded.
Providers/dependencies are warm and no model task runs, so these component
observations establish neither comparative benefit nor voluntary adoption.
The [source-selection gate optimization](experiments/codegraph_preparation_policy.md)
addresses a measured preparation cost without bypassing source-policy guards.
On one fixed full artifact, three fresh-object repeats per implementation
reduce median validation time from 27.925s to 2.084s (92.54%), preserving the
query surface, all 786,078 ordered occurrences and the complete quality report.
The targeted suite passes 518 cases and the full local unit tier passes 8,989;
an independently installed local wheel verifies all 485 Python files/native
bytes and serves a source-verified graph query. The packet retains setup
failures, twelve exact source windows and all four owned containers' absence.
This is gate-only source-checkout timing; the installed admission reuses the
captured index. Merged [#937](https://github.com/sysevol-ai/CodeNib/pull/937)
passes all seventeen executed PR checks and twenty executed main release
verifiers; main unit/integration and Docs build/deploy also pass. Conditional
native/serial/consumer/slow/tag skips remain explicit; public 0.2.4 is unchanged.
The [separate full preparation pair](experiments/codegraph_preparation_pair_2026_10_30.json)
observes first graph requests of 185.678s → 188.577s and post-edit requests of
217.153s → 212.595s with prepared dependencies/providers. One sequential pair
shows no clear overall gain; SCIP generation varies substantially. Both final
complete graph/occurrence/report outputs agree and 24 source windows verify,
but initial full artifacts were not exported, leaving that prospective quality
gate partly unobserved. Retain all 97 records and this coverage gap. No model
question runs; the full matched coding, confirmation and external-use gates
remain open.

The [shared SCIP text scanner](experiments/codegraph_scip_text_scan.md) removes
another measured decoding cost while preserving the existing parser contract.
Three fresh decoders per implementation on one complete saved artifact reduce
median decode time from 13.316s to 6.409s (51.87%); every ordered node/edge
attribute and endpoint agrees. The SCIP unit tier passes 368 cases, five
existing synthetic C++ parity cases pass, and the full local unit tier passes
9,009 tests. An installed developer wheel verifies all 485 Python/native bytes
and reproduces exact normalized text, full graph/occurrence/report outputs and
artifact receipts from fixed raw compiler output. All 126 records, three
harness failures and exact owner cleanup remain. This additional component
is not measured in the complete preparation pair or the frozen coding cohort;
no broader benefit, independent success or external user is added.
Merged [#939](https://github.com/sysevol-ai/CodeNib/pull/939) passes all seven
executed main CI jobs, all twenty executed release verifiers and Docs
build/deploy. Slow and five tag-only publication skips remain explicit;
public package 0.2.4 is unchanged.

The [source-isolated live Python query controls](experiments/codegraph_native_source_queries.md)
retain CodeNib's complete selected source, stale-result rejection and explicit
recapture with rename/line-shift/Unicode checks. Merged
[#940](https://github.com/sysevol-ai/CodeNib/pull/940) passes main Docs and all
twenty executed release verifiers, with five tag-only publication skips.
Its original cold omission and every earlier outcome stay retained.

The [shared LSP transport repair](experiments/codegraph_lsp_stdio.md) preserves
coalesced/partial messages and late replies that the existing client dropped.
Seven initial real-pipe failures reproduce on base; the final eight cases also
verify successful late definition/reference replies clear stale errors. The
full local unit tier passes 9,017. The raw server replies confirm a separate
cold omission (1/3 references before file enumeration) in both client versions.
Explicit preparation of every selected Python file yields 3/3 in the fixture
and six matching compiler location sets for three fixed symbols on all 2,390
CodeNib source files. The first guarded position result, including capture,
offline installed setup and all 1,172 Python files' preparation, takes 7.142s
and 7.702s in the two candidate-version observations, both retained. The installed
post-SCIP replay preserves exact normalized
text, all 786,078 ordered occurrences, query surface, quality and receipts.
All eleven original positional deliveries, source copies, provider inputs and
five owners' cleanup per version verify. The initial packet and final-version
supplement remain separate. This is still an offline
Docker feasibility result, without product live routing, multi-hop graph parity
or a coding comparator. Portable source/configuration closure and representative
cold completeness remain open before routing promotion. No model query,
independent success or external user is added; whole-task gates remain open.
Merged [#941](https://github.com/sysevol-ai/CodeNib/pull/941) passes all seven
executed main CI jobs, all twenty executed release verifiers and Docs
build/deploy, with slow and five tag-only publication skips explicit.

The [complete-source Python virtual-filesystem diagnostic](experiments/codegraph_python_vfs.md)
retains every byte of all 2,390 files in both provider workers. A prospectively
fixed 200-position sample completes 400 queries: the original browser provider
agrees with compiler sets 346/400, and a separate ordinary-server diagnostic
354/400. A separately frozen software-path repair, without upgrading the engine
or changing project bytes, removes all nine browser/ordinary reference
differences: 400/400 sets agree between those two forms. This is exposed-source
attribution, not accuracy or an independent success. The 46 shared compiler
differences include an omitted alias call; reference semantics and product
source-generation lifecycle remain open before live MCP routing. Preserve the
failed 128 MiB capture control's 96 unadmitted staged replies and 304 unexecuted
positions, plus all setup/verification failures. No timing comparator, product
change, model inference, external enrollment or promotion is added.

The [shared-client generation-owner repair](experiments/codegraph_lsp_generation_owner.md)
bounds shutdown writes/waits, retains an unreaped owner for retry and rejects
replacement or false readiness after a missing initialize result. Eight final
regressions reproduce on unchanged source; 16 related cases, the 9,025-pass
serial tier and the 9,024-pass parallel/timeout tier are retained. The initial
remote timeout-fixture failure and its correction remain separate. A separately
installed replay preserves the entire prior
graph/occurrence/quality/receipt admission and observes the real provider exit
with pipes closed. Source-only alias controls recover the exposed call and
verify post-response/stale rejection, explicit recapture and bounded cleanup,
but still cover only 10/11 required fixture calls with 0/5 negative calls
included. Those failed controls remain unchanged. Merged
[#943](https://github.com/sysevol-ai/CodeNib/pull/943) passes main Docs and all
twenty executed release verifiers and all seven executed main CI jobs,
including serial integration, SCIP/core and graph consumer. Slow and five
tag-only publication skips remain explicit. Public 0.2.4 is unchanged.

The separate [function-flow diagnostic](experiments/codegraph_python_function_flow.md)
finds all eleven required fixture calls and excludes all five negatives, but
does not establish a complete reference route. A conditional reassignment
collapses distinct functions to one callable type and is wrongly excluded by
the first wrapper. A conservative analyzer-flow guard marks that call partial,
but also rejects the required origin on complete source. Both failed controls,
an unresolved import in the original recapture fixture, and a controller
exception-class mismatch remain retained; separate corrections do not replace
them. All 267 captured replies/deliveries and twelve owners' cleanup verify.
Merged [#944](https://github.com/sysevol-ai/CodeNib/pull/944) passes main Docs
and all twenty executed release verifiers, with five tag-only skips explicit.
Its documentation-only merge does not rerun full CI; #943's seven executed
main CI jobs remain verified. The private function-flow route remains
unpromoted.

The [source-bound definition candidate](experiments/codegraph_source_definitions.md)
in [#945](https://github.com/sysevol-ai/CodeNib/pull/945) admits an exact
captured Python function/class at the deferred full-surface MCP boundary.
The original installed and unmodified CI-wheel controls cover all 2,390 source
files, graph fallback, immediate source revocation, prompt worker cleanup,
stale-query rejection and explicit same-session reloading. The final original
unit tier passes 9,052 with 24 skips; the 95-record
[CI supplement](experiments/codegraph_source_definitions_ci_2026_10_31.json)
retains all five worker exits and 21 passing executed source-head checks, with
nine explicit skips. Full-source worker RSS is about 1.54 GiB in one
observation, without a comparative memory or timing claim.

The definition component is merged in #945 on top of #947/#946. Only the four
style-base modules and #946's two admitted product
files differ from the older definition candidate. The separate 105-record
[combined installed admission](experiments/codegraph_source_definitions_reconciled_2026_10_31.json)
passes MCP/fallback/lifecycle/complete-source controls with five source workers
and one ordinary language server reaped; preserve the
older 10/11-plus-one reference result rather than rescoring it. #946 separately
finds 11/11 positives with the same raw-compiler alias-binding negative.
Default exploration, representative provider/configuration coverage, actual
source-linked use, whole-task benefit and independent/external gates stay
open. #946 passes all 18 final executed checks and is merged with its reviewed
tree preserved. #945 also passes all 21 final executed checks, preserves its
reviewed tree and uses a real-newline squash message. Main `90d4a8ed` Docs,
lightweight installation, no-model publish smoke and all twenty executed
release verifiers pass. All seven executed trusted CI jobs also pass, including
serial integration, native SCIP/core and graph consumers; slow is explicitly
skipped. The later #948 documentation merge retains the same product bytes.
No model question, public package release or participant is added.

The [alias-binding diagnostic](experiments/codegraph_python_rebinding.md)
retains 170 exact records, including both controller failures. Standard
definition navigation returns the same original-function/assignment candidates
before and after rebinding. With all 21 selected documents opened, standard
references at the function return eight import names rather than the required
call sites. The graph's earlier 11/11-plus-one result stays frozen. A fresh
original-source runtime witness calls the replacement after assignment,
confirming that exposed negative without changing the oracle. Check actual
Serena REPL behavior and a real alias-traversal maintenance need before another
provider or paid efficacy round; none of these observations prove task benefit.

The [ignored-cache repair](experiments/codegraph_ignored_cache.md) is a local
candidate for a real source-update interruption: Python bytecode generation
previously invalidated the retained inventory despite unchanged selected files
and fingerprint. Six corrected Linux/Windows-fake regressions fail before the
change and pass after it; two scan-race cases still reject. All 234 source-binding
cases pass with five platform skips. The same installed MCP driver reproduces
the error on the admitted CI wheel and passes on a one-module developer overlay:
cache creation reuses the connection, worker and manifest without rebuilding,
while a genuine selected edit still rejects before another provider request
and reaps the worker. Both owners exit normally. Preserve the initial test
controller and umask failures; the latter reproduces on unchanged main.
The corrected-environment full local unit tier passes 8,957 cases with 51
skips; an unmodified current CI-wheel admission remains open. No
complete-workflow comparison or success is added.

The latest four-arm round records 8,821,055 normalized final main/auxiliary tokens
and $9.311356 in SDK estimates, not invoices. The separate diagnostic adds
78,348 tokens and $0.1850155 in SDK estimates within its $1 guard. The recorded
estimate lower bound across rounds is $44.6073663 plus earlier unknown usage.
Keep the authorized
$500 API ceiling and existing native subscription route; no API-key/account
fallback or complete HTTP-count claim. External enrollment, consent and
seven-day repeat use remain 0/5, and no outreach is authorized.

Owner: CodeNib product and CodeGraph maintainers. Tracking issue:
[#874](https://github.com/sysevol-ai/CodeNib/issues/874). The detailed decision
contract is [the pilot plan](experiments/codegraph_net_benefit.md). Prioritize
an actual source-linked cross-file decision, useful voluntary calls and
representative post-edit workflows. Two prospectively frozen success
confirmations and external repeat use remain required. Defer unrelated
language/backend and default-rollout expansion; existing A1-A6 obligations
remain in force.

| Gate | Current outcome | Next acceptance |
| --- | --- | --- |
| G0: baseline | Source-baseline and candidate SCIP/BM25/MCP fixtures cover clean/warm setup, dirty first use and modified/created/deleted/renamed-file rebuilds. A separately installed, hash-verified public PyPI 0.2.4 wheel confirms dirty-init rejection and stale facts in an existing MCP session after editing. Registration CLIs are isolated stand-ins; MCP is real, dependencies/toolchain warm and no model runs in these controls. Fifteen bounded query observations check five Requests/Flask definitions, position lookups and graph roots against source; two short-name ambiguities remain recorded. [Original receipts](experiments/codegraph_workflow_2026_10_26.json), [installed-release control](experiments/codegraph_pilot_admission.md), [execution/source checks](experiments/codegraph_execution_2026_10_27.json). | Broader provider correctness remains open; these source checks and fixture timings do not establish complete graph accuracy or whole-task benefit. Preserve the original failed admission as a historical control. |
| G1: daily workflow | Dirty initialization, source-current queries, explicit same-session reload, Python identity recovery, bounded source delivery and deferred preparation are merged. The [workflow receipts](experiments/codegraph_reload_2026_10_27.json), [identity repair](experiments/codegraph_python_identity.md), [recovery probe](experiments/codegraph_agent_recovery_v1.md) and [reference semantics](experiments/codegraph_reference_semantics.md) retain their original outcomes and costs. The [wildcard-reference repair](experiments/codegraph_python_wildcard.md) in [#946](https://github.com/sysevol-ai/CodeNib/pull/946) now passes the normal strict compiler/MCP route: all 11 original positive call lines are found, with the original post-rebinding negative retained. Thirteen final boundary cases, both decoders and the 9,038-pass/24-skip unit tier pass; the full installed CodeNib replay preserves all 786,078 occurrences and existing graph bytes. Builder identity 10 requires explicit rebuilding; graph layout 5 is unchanged. #945 has a separate [combined CI-wheel admission](experiments/codegraph_source_definitions_reconciled_2026_10_31.json) on merged #946/#947: actual MCP/fallback, lifecycle and complete-source controls pass with all six owners reaped, and its admitted source head passes 21 executed checks with eight explicit skips. Both components remain unreleased and do not change frozen cohorts. | The [scikit-learn cold-start gate](https://github.com/sysevol-ai/CodeNib/issues/901) remains closed by the original full verification chain. The fix’s separate [CI-wheel admission](experiments/codegraph_python_wildcard_ci_2026_10_31.json) verifies identical product bytes and actual post-edit compiler/MCP behavior, with 17 executed source-head checks passing and nine explicit skips. The baseline all-files repair #947 is merged and incorporated. The [reconciled CI-wheel admission](experiments/codegraph_python_wildcard_reconciled_2026_10_31.json) passes compiler/MCP, declared-edit reloading and a fresh complete-source replay; all four artifacts remain byte identical. Its 87-record packet preserves the older controls. #946 is merged as `ad87b872` with its reviewed tree preserved and 18 executed final checks passing; #945 is also merged as `90d4a8ed` with its reviewed tree preserved and 21 executed final checks passing. Main `90d4a8ed` passes all seven executed trusted CI jobs, including serial integration, SCIP/core and graph consumers, plus all twenty release verifiers; slow and tag-only publication skips remain explicit. #948 changes documentation only and retains those product bytes. Complete representative wildcard/configuration/update coverage. The raw compiler alias-binding error remains separate from intentional static unions. The [standard-navigation/runtime diagnostic](experiments/codegraph_python_rebinding.md) confirms the original counterexample and shows why one definition query does not repair it; check actual competitor alias composition and a real maintenance need before another provider or efficacy round. Preserve static binding unions and the failed physical-call gate. Actual source-linked decisions, useful voluntary calls, representative whole-workflow costs, external consent and repeat use remain open; automatic rebuilding is not authorized by a stale query. |
| G2: matched benefit | The [source-payload confirmation](experiments/codegraph_payload_results.md) completes 60 tasks, 48 source judgments and 596 original selected native outcomes. Native/Serena/Codegraph/CodeNib accept 11/10/8/10 tasks and each passes 2/3 repairs. CodeNib takes 738.588s versus Native 558.727s and Serena 666.964s; recorded MCP attempts are 0/0/6/0. All 5,294 inputs and previous outcomes remain unchanged. No complete eligible alternative; insufficient evidence and zero independent successes. | Hold promotion and comparative-efficiency claims. An actual source-linked task and useful voluntary use must precede another efficacy cohort. Keep complete quality/cost gates, two independent success confirmations and external reuse unchanged; the further fixed confirmation set remains unopened. |
| G3: repeat use | The user confirms no recruitment program is currently available; no external participant is enrolled. SDK probes are maintainer-operated connectivity checks; the pilot contract now contains an unsent invitation/consent hand-off. | Five consenting users attempt setup on their own repositories; observe a first successful source-linked query and seven days of actual use, targeting voluntary return by at least three. Confirm recruitment and consent; keep missing observations explicit. |
| G4: product decision | Hold promotion after the complete matched cohorts, development and prospective source-payload result fail to establish benefit. Keep claims at source-current navigation and bounded indexed references where providers are ready, with explicit rebuild/resource costs. Preserve every failed repair, source judgment, word-limit rejection and previous outcome. Successful native fallback cannot establish graph efficacy. | A real source-linked consumer, representative G1 coverage, useful voluntary calls, the unchanged complete-workflow benefit gate, two independent success confirmations and five-user repeat-use evidence remain required. The bounded hold decision is recorded; A0 and #874 stay active. |

Preserve complete denominators, actual costs and unavailable outcomes. Static
ARB scores, graph-query speed, ContextBench rank, source-delivery checks and
website clicks do not satisfy G2 or G3. If the review date arrives with gates
open, record them explicitly; an unrun external trial is neither a performance
failure nor an achieved benefit.

### A1 — Make the existing product understandable

Status: [#781](https://github.com/sysevol-ai/CodeNib/pull/781) merged into
`main` as `28910cc9`; production deployment verification remains. README has
two setup commands, an early authentic product image, a
sourced comparison and prominent language/reproduction links. Landing uses a
static preview instead of live demo iframes. Public method, comparison and
architecture pages are added. Strict MkDocs build, public-doc boundary audit,
pre-commit checks and desktop/mobile/no-JavaScript browser checks pass. The
landing page makes zero external requests and its setup links reach the two
commands without overflow. The recorded agent clip and source-linked poster
are merged in #788 (A4) as `dc2b4df0`, including user-controlled playback. A fresh
published-package recording and paired token comparison remain open;
production grep/Jev authorization remains in A2/A3.

- Rewrite README and website around finding source context for coding agents.
  Lead the website with a usable, source-pinned example and a clear Wiki entry.
  Keep the verified 71.4% Recall@5 result below the first interaction, with its dataset,
  comparator (+12.8 percentage points), experiment label, and public method.
  Never substitute issue resolution rate, token savings, or a Spark latency.
- Keep README Quickstart to installation and `codegraph init`. Link advanced
  source policies, prerequisites, diagnostics, Wiki setup, and architecture
  to docs. Do not silently change the shipped retrieval default in copy.
- Put an existing authentic product image above the long-form content now.
  Replace/augment it with the actual agent recording at A4, not fabricated
  terminal output or a mock represented as a real run.
- Add a compact, sourced comparison with grep/read, Serena, DeepWiki, and an
  explicitly identified CodeGraph project. Distinguish local tool execution
  from the agent's model, persisted typed graphs from LSP symbol navigation,
  and view reuse/rebuild from file-level incremental repair. The user confirmed
  `colbymchenry/codegraph`; README and the detailed table use that project,
  checked at `ba3c21e50d9129d2f5f3843ec3728868ae6d47a1`. Its local typed graph,
  broad language support and incremental watcher are shared or competing
  capabilities, not gaps to claim. Document its telemetry controls and keep
  upstream agent-token results separate from CodeNib retrieval measurements.
- Make the generated language matrix and pinned method reproductions
  prominent. Describe compatibility separately from reproduced paper scores.
- Add missing GitHub topics and direct GitHub Release links. Audit the
  existing publication chain before making any release automation change.

Acceptance: strict docs build and public-link audit; published claims traced
to committed results; desktop/mobile inspection of the actual landing page;
all first-screen links work; quickstart remains exactly two commands.

#### ContextBench result publication

Status: the [English article](https://codenib.ai/blogs/contextbench-500-tasks/)
and its homepage/blog-index entries are published on 2026-10-23 UTC from
`5c2edd00`, tracked in [#852](https://github.com/sysevol-ai/CodeNib/pull/852).
Its figures follow the other posts: a Geist-embedded mechanism diagram serves
as both hero and sharing image, with a narrow variant for phones, and the
task-source chart adds model usage per task. Local and public browser
acceptance passes at 1440/820/390/320 pixels, including metadata, figure
variant selection, dated ranking scope, no overflow and exact asset hashes.
Wide/narrow teaser labels fit their SVG bounds. Delayed/failed-style
opening/refresh regressions pass locally and publicly at 1440/390/320 pixels.
Strict MkDocs,
public-doc boundaries, support tables and pinned pre-commit checks pass.
The article leads with 77.07% search-only line recall and the 0.83-point
increase to 77.90% after confirmed repair reads. It explains the fixed
first-search/scout/repair roles, iterative source-tool loop, observed-source
union and bounded navigation handoff, distinguishing role routing from
dynamic model selection and navigation metadata from source bodies.
The separate 32-task comparison retains uncertainty about the complementary
policy's gain over a simple repeat. The complete-system result remains
388/500 native positives (77.60%), 28.11% final line F1 and $0.6778 recorded
search-plus-repair model usage per task; search-only model usage is $0.0114.
The [official submission PR #4](https://github.com/ContextBench/contextbench.github.io/pull/4)
merged on 2026-10-22 UTC, and the full agent is published on the
[Agent systems leaderboard](https://contextbench.github.io/). As of
2026-10-23 UTC it ranks #1 in line recall and Pass@1, verified against the
[published score snapshot](https://github.com/ContextBench/contextbench.github.io/blob/4f979971e04a1fe0a743fbe45e60fa8edf79440d/src/data/agent_results.json).
The teaser, homepage/index copy and social metadata state the full-agent
scope and date while retaining the separate search-only result.
Known annotation exposure,
three reused development repairs, partial shell-source attribution and cost
scope remain explicit. The measured Haiku search/Opus repair route is kept
separate from shipping CodeGraph and experimental Jev/graph claims.

Open gates: CodeNib PR #852 review/CI. The external submission and leaderboard
publication gate is closed.

#### First-visit activation

Status: the shared first-visit surface is merged in
[#809](https://github.com/sysevol-ai/CodeNib/pull/809) as `7748b3df`; its
#803 → #808 dependencies are merged. The subsequent map-led marketing hero in
[#817](https://github.com/sysevol-ai/CodeNib/pull/817) is merged as `0ba5e7ec`.
The question-first marketing iteration is merged in
[#828](https://github.com/sysevol-ai/CodeNib/pull/828) as `99d33164` and published
to `codenib.ai` with the matching Wiki frontend on 2026-10-12 UTC. Live
desktop/mobile acceptance passes; hosted measurement remains opt-in and off.

The marketing homepage now starts with a complete code-understanding task:
**question → explanation → indexed calls → source**. The Requests
redirect/authentication example sits beside concrete value copy and one
primary “Try an example” action, with local-agent setup as the secondary action.
Three task choices cover authentication, the redirect request path and callers
that could be affected by changing `rebuild_auth()`. They use the same four
source-pinned symbols and three recorded calls, and explicitly limit request
and impact answers to that selected path. They are neither a runtime trace nor
an exhaustive impact graph. The evidence stays bundled locally; browsing the
example makes no model call, graph request or live source fetch.

The page order is example, own public repository, local Agent setup, technical
evidence and documentation. Repository entry shows OpenRouter funding,
spending-limit behavior and the existing hosted archive/file/chunk limits
before sending visitors to the existing full Wiki pipeline. It continues to
resolve prepared Wikis before offering user-funded generation, and makes no
new arbitrary-repository cost, latency or graph-coverage promise. The Agent
section explains local indexing and source navigation, distinguishing CodeNib's
model-free graph route from the agent's own model and the grep/Jev experiment.

The dark/cyan example remains the visual focus. Hero, repository and Agent
sections share a content width and column split; typography, card padding,
contrast and vertical spacing are consistent throughout the page. On phones,
the default example precedes task choices and the repository form. Both
`/#example-source` and the existing shared `/#requests-auth` link reach the
example. The homepage sharing image uses the revised headline and real source.

Local acceptance: the focused marketing browser harness checks task answers,
all source controls, indexed call anchors, keyboard focus, stable source
selection, section alignment, 1440/1024/860/768/390/320-pixel layouts, repository
handoff, both fragment links and the no-JavaScript fallback. Those checks pass,
along with 174 frontend unit tests, the production build, strict MkDocs and the
public-doc boundary audit. Syntax artifacts match all four captured excerpts.
The focused log-summary regression passes before, after and duplicate events
for independent tabs and origins.

Live acceptance: all six viewport sizes pass the same question, source,
keyboard, alignment, stable-selection, fragment-link and repository-handoff
checks against `codenib.ai`, together with no-JavaScript and fixture-backed
opt-in privacy checks. The production host injects its existing Cloudflare
analytics script; it does not enable CodeNib's collector. The actual repository
form also opens the prepared Requests Wiki at 1440 and 390 pixels, where the
redirect chapter and an indexed source citation are navigable without API
writes. The served entry module, shared component, stylesheet and sharing PNG
match the merged release bytes and expected content types; the real title is
“CodeNib: Understand Unfamiliar Code with Evidence.” Both frontends use a
matching published source build and the preserved hosted runtime configuration. The new
frontend passed health and shared-example checks before one Caddy reload
activated both entry points; the prior frontend and ingress settings are
retained for operator-owned rollback. The existing Wiki backend reports healthy
with 27 repositories.

Marketing first paint: [#830](https://github.com/sysevol-ai/CodeNib/pull/830)
is published to `codenib.ai` on 2026-10-16 (Pacific), using paired build
`05eeed7c`. The shared example imports its styles as a module dependency and
installs them synchronously with its shadow markup. Its first painted frame
has aligned line numbers, syntax colors and one selected source note; a slow
stylesheet cannot expose concatenated numbers, raw code or every note. The
readable Wiki fallback stays available if the style dependency fails.
The focused regression reproduces the original public failure and passes
locally and on the public homepage at 1440/390/320 pixels, opening and
refreshing with delayed and failed style dependencies. It samples painted
frames before releasing the delayed dependency. Web CI runs this regression
and also triggers for changes to the static homepage and shared example assets.
The six-width marketing interaction, alignment, source-selection, legacy-link,
repository-handoff, no-JavaScript and opt-in privacy checks pass locally and
on the public host; its existing Cloudflare analytics injection remains
separate from CodeNib's disabled collector. The matching Wiki frontend passes
real prepared-page, source-opening and shared-example checks at desktop/mobile
sizes. Served entry and style-module hashes match the published release.

Wiki refresh stability: [#830](https://github.com/sysevol-ai/CodeNib/pull/830)
is published to `demo.codenib.ai` in the matching `05eeed7c` frontend build.
The reader keeps the Overview column policy fixed,
publishes prose with its resolved leading map/boundary (including unavailable
and error outcomes), and measures the map before paint.
Optional visuals have a five-second deadline that includes waiting for the
outline. Stalled map/boundary requests are aborted; an outline that arrives
after the deadline cannot insert a map above readable prose. An explicit map
retry can recover it, and the elapsed indicator covers the visual wait.

Local and public production-build regressions pass all 72 opening/refresh
combinations: 1440/1024/768/390 pixels, delayed page/map/boundary responses,
failed visuals, an unavailable map, and stalled map/boundary/outline requests.
They verify stable painted reading widths and map heights, readable prose at
the deadline, ignored late results and explicit retry recovery. All 176 frontend
tests, the production build, strict MkDocs, the public-doc audit and pinned
pre-commit hooks pass; Web CI runs the browser regression.
Real prepared Requests Overview and Redirect Resolution pages also pass at
1440 and 390 pixels with delayed visual requests, stable opening/refresh
geometry, chapter navigation, source opening and the shared homepage example.
The actual marketing form opens that Wiki at both widths without API writes.
Served entry assets and the preserved hosted runtime configuration match the
published build's SHA-256 hashes and content types. The prior frontend and
ingress configuration remain available for rollback; prior assets are retained
for already-open browser tabs. The existing backend remains healthy with 27
repositories and was not restarted for this frontend publication.

Measurement reuses the existing opt-in first-party collector. Inspecting source
(`example_source_open` / `example_github_open`) followed by `repository_submit`
or `agent_setup_open` is the marketing funnel; viewing the hero, choosing a
task or clicking the primary button alone is not activation. Events contain
only the fixed event, surface and tab-session ID, with deduplication and DNT/GPC
suppression. The log summary counts a next step only when it is observed after
the tab's first source event; an earlier setup/submit is excluded. The collector
remains disabled by default. Observations are
separate by origin and do not prove a successful own-repository agent query.

Open gates: explicitly enable hosted measurement with the matching collector
and log-retention policy; establish a baseline and observe 5–8 unfamiliar
visitors before claiming better conversion. Broader question examples,
arbitrary-repository cost/latency, consent, corpus quality and non-Python graph
coverage retain their existing acceptance gates in A3/A5.

### A2 — Ship grep → Jev as a bounded product route

Status: [#782](https://github.com/sysevol-ai/CodeNib/pull/782) is merged into
`main` as `d6af8f79` and ships as an opt-in route in 0.2.4. CLI `codenib explore` and MCP share a
bounded source-only grep/Jev runtime, one OpenRouter credential, reported usage,
source/exclusion checks and cancellation. There are no automatic model retries;
existing indexed routes remain the released defaults.

The candidate audit in [#787](https://github.com/sysevol-ai/CodeNib/pull/787),
merged as `714218ab`, completes all 100 frozen plans with HTTP disabled. Ordered candidate text and
corrected spans match in 94 cases; six pools differ. Frozen-plan grep retains
58.62% Recall@5; aggregate frozen reranking is unset because three pools have
new unscored text. The separate range audit preserves the historical research
orderings' 58.62% / 71.40%.

Fresh product evaluation in #787 attempts all 100 cases: 99 succeed and one
rejects a multiline planned expression. Under the predeclared failure-zero
policy, Recall@5 is 48.37% for grep and 65.57% after Jev (+17.20 points;
repository-bootstrap 95% interval +8.88 to +25.44). Reported cost is
$1.043885892 with no unknown call charges. Every case and the separate earlier
incomplete attempt are retained without retries or substitutions. These are
localization results, not an agent/token benchmark.

The multiline correction is implemented in
[#792](https://github.com/sysevol-ai/CodeNib/pull/792), merged as `fbfe1fbb`
and ships in 0.2.4.
Its 51 focused route tests pass, including Windows newline handling; the
preceding evaluation is not a measurement of that correction. Provider-consent acceptance, paired agent measurement,
default-route promotion remain open. The 71.40%
historical research result stays distinct from the fresh product measurement.

- Extract the already measured plan/search/chunk/rerank path from the
  research runner into the existing repository-context runtime. Do not
  import experiment scripts from installed product code.
- Give CLI and MCP one explicit route/configuration. Plan regex/glob queries,
  execute `rg` locally without a shell, map hits to source-checked chunks,
  deduplicate/cap candidates, then call Jev Decisions. Preserve locations,
  source identity, cancellation, timeouts, and observable provider failures.
- Match the measured limits initially: at most six planned searches, 100
  candidates, 3,000 visible characters per candidate, batches of ten for Jev.
  Verify current runner defaults before freezing a product protocol.
- Use one OpenRouter credential/configuration for planning and reranking.
  Keep the backend dependency small; no default embedding download, GPU, or
  full graph build. Report explicit no-credential/offline behavior; do not
  quietly present BM25 as equivalent to the selected route.
- Avoid shell injection, arbitrary filesystem access, symlink escape, and
  ignored/excluded source leakage. Reuse existing source authority and path
  policy, not a second repository database or lifecycle.

Acceptance: deterministic tests exercise the real production route with
provider fixtures; source/candidate parity against the frozen experiment;
fresh CPU-only install; bounded timeout/cost/cancellation behavior; explicit
live smoke with recorded model identities before promoting it as default.

### A3 — Connect OpenRouter without collecting a master key

Status: local CLI/MCP authorization is implemented in
[#783](https://github.com/sysevol-ai/CodeNib/pull/783),
merged into `main` as `f2a805eb` and released in 0.2.4. Browser and headless PKCE use S256,
one-use in-memory verifiers and direct OpenRouter exchange. Saved keys resolve
for both retrieval stages. Default storage uses a supported OS keyring;
an explicit POSIX file fallback enforces 0700/0600, no links, no Git checkout
placement and atomic replacement. Imports reject management/provisioning keys.
Status and logout distinguish local presence, provider verification and provider
revocation; keys never appear in ordinary output or repository/MCP config.

Local callback behavior, provider metadata access and lightweight installation
are verified; detailed validation is recorded in #783. Logout handles malformed
files and unavailable keyrings explicitly. Credential preparation and logout
recover owned temporaries under the existing directory lock; a process-kill
test verifies recovery. A clean installed package also passes real Linux
SecretService save/read/delete against GNOME Keyring 46.1 in a private D-Bus
session and data directory. It uses the unchanged installed auth module, leaves
zero fake entries and never accesses the existing desktop vault or a model.
Interactive desktop unlock and real provider consent remain open.
A real browser directly called OpenRouter key metadata, planning and
Decisions with an existing normal key; all succeeded without exposing it to
the local fixture server, DOM or browser storage. This establishes authenticated
transport, not provider consent or hosted product acceptance. Detailed transport
validation is recorded in #783.

Lightweight installed-package acceptance in
[#793](https://github.com/sysevol-ai/CodeNib/pull/793), merged as `377ea365`,
passes on Linux, macOS and Windows with PATH empty, no embedding/graph/model
SDK packages and fixture-only
provider responses. macOS and Windows also pass real OS keyring save/read/delete
round trips with unique fake entries. The Windows source-read correction in
[#795](https://github.com/sysevol-ai/CodeNib/pull/795), merged as `873f0c64`, preserves lexical HANDLE
bindings for ancestors and full repository-root version/inventory checks.
Its deterministic regressions allow unrelated sibling creation and reject
repository mutation or an ancestor replacement. The combined platform check
passes. Its complete release-artifact, installation and unit checks passed
before merge and the package is published in 0.2.4; real provider consent remains open.

The browser trial in [#796](https://github.com/sysevol-ai/CodeNib/pull/796),
merged as `56472bfe`, exchanges a one-use S256 grant directly with OpenRouter
and holds its key in a private JavaScript field for up to ten minutes. No key
enters UI state, storage, URLs, exported assets or source-service requests.
The nonce-scoped callback supports denial, expiry, replay rejection and
disconnect without requiring a popup opener. Provider metadata must explicitly
confirm an inference key. If exchange or verification fails, the UI retains a
safe provider settings link for revocation, including cancellation and local
agent handoff. Query submission is separate from authorization. Reported-cost
thresholds stop subsequent calls at $0.10/query
and $0.50/connection, with unknown costs and provider revocation stated
explicitly. These thresholds are not billing caps.

Real Chromium acceptance browses all 15 Requests pages under the opt-in CSP,
then enters through the mobile Wiki header and desktop question bar and
exercises actual source capture, ripgrep and chunking with fixture-only provider
responses. Correct method spans link to the pinned commit. Desktop/mobile
checks cover error/disconnect, failed-grant revocation guidance and absence of
credentials from DOM, browser storage, cookies and CodeNib HTTP requests.
The CSP blocks inline
script execution, and callback responses have a separate restrictive policy.
Actual provider consent and production-host headers/callback behavior remain
open. The public browser-trial guide documents same-origin script trust,
callback grant logging, IP-proxy trust and the explicit deployment gate.
The reviewed trial tree includes merged bundled runtime #793 and passes 7,318
unit tests (35 skipped; 190 heavier tests deselected; umask 022), 108 frontend
tests, the production frontend build, strict MkDocs, the public-document audit
and changed-file pre-commit. Provider fixtures
support these results; none makes a new quality or token-savings claim. A fresh
Linux installed-package check runs both MCP retrieval and the public source
API with PATH empty and no model/graph SDKs, credentials or real provider
calls. The source-API check passes on Linux, macOS and Windows in the installation
job. Browser acceptance uses `localhost` against a real IPv6 listener. Export,
service configuration and frontend admission consistently reject IPv6 literals:
browser CSP host sources cannot match them. The guide documents the hostname
route; the script and connection policies remain restrictive. All five review
threads are addressed. All executed checks passed on the accepted head, including
three-platform installed-package coverage, manylinux artifact verification and
Python 3.10–3.14 ABI3 smokes. The merge message was inspected; actual provider
consent and deployment remain open.

Published-package installation guides in
[#798](https://github.com/sysevol-ai/CodeNib/pull/798) use verified 0.2.4 and
its bundled frontend. PyPI, Registry and GitHub Release publication are
accepted; authorization and hosted-trial promotion gates remain explicit.
The maintainer selected real desktop CLI login acceptance before browser
trial acceptance. A fresh installation of the CI-built manylinux x86-64 wheel needs only
the `grep` extra to serve real HTTP planning metadata and source candidates:
MCP, auth, graph and model SDK packages are absent, credential headers are
rejected, and no provider call or credential read occurs.

Native registration is merged in
[#785](https://github.com/sysevol-ai/CodeNib/pull/785) as `50215406`. `codenib init` checks or
requests authorization and registers source-only MCP through Claude Code/Codex
without indexing or model calls. Independent server names let CodeGraph coexist.
The existing pending receipt supports recovery and refuses unmanaged/drifted
configuration. Registration reloads ownership under the existing directory
lock and merges concurrent client selections. Status inspects each client
independently; uninstall preserves source and credentials. Native CLI setup
and MCP discovery are verified in isolated real clients; detailed versions
and checks live in #785. A real Claude Code query on pinned Requests source
used the source-only route, found both authentication/redirect methods and
returned verified source without creating an index. Source and user profiles
were preserved. This is a connectivity smoke, not a quality or token benchmark.
Provider consent and interactive desktop unlock remain open. The combined
installation tree passes 7,286 unit tests (35 skipped; 190 heavier tests
deselected), strict documentation checks and the public-document audit.
Bundled runtime #793 includes both merged dependencies and preserves that
verified product tree.

The lightweight `grep` extra in
[#793](https://github.com/sysevol-ai/CodeNib/pull/793) includes `ripgrep-bin`.
CLI startup, source capture entry and the retriever use its installed executable even when the
agent has not activated the Python environment; an existing system `rg` is a
fallback. A clean Linux installation verifies actual source-linked retrieval
with PATH empty, no embedding/graph/model SDK packages and fixture-only provider
responses. The installed-package smoke is scheduled on Linux, macOS and Windows;
the latter two also exercise the real OS credential store with a unique fake
entry that is removed afterward. The three platform jobs and macOS/Windows
vault checks pass. Actual provider consent remains an acceptance gate. These installation checks do not establish
retrieval quality or agent token savings.

- Prefer OpenRouter OAuth PKCE (S256), using a fresh verifier and one-time
  local callback. Bind callback state to the initiating session, validate
  the callback host/path, and reject replay. Test against OpenRouter's actual
  authorization protocol rather than assuming generic OAuth fields work.
- Local CLI/MCP calls OpenRouter directly. Keep credentials in the OS keyring
  where available; offer an explicit permission-restricted user file only
  when needed. No credential in repositories, command arguments, telemetry,
  ordinary logs, exports, URLs, or browser `localStorage`.
- Show that query text and selected source snippets go to the configured
  planning/scoring providers. Explain the difference between local source
  search and remote model processing before connecting private code.
- Support disconnect/revocation and an application spending limit. Provider
  credit caps and application estimates are different guarantees; verify
  provider support for OAuth-created keys. Do not request a Management API
  key. An OAuth-issued key is not automatically ephemeral or narrowly scoped.
- For a hosted trial, first evaluate a browser session calling OpenRouter
  directly while the server supplies bounded, public-repository candidates.
  Gate on real CORS/Decisions support and XSS/CSP review. If that cannot work,
  make a session-only backend proxy explicit: it can read the key, keeps it
  out of logs/storage, limits lifetime and request budgets, and deletes it on
  disconnect. Do not claim encryption makes the key inaccessible to us.

Acceptance: success, denial, replay, expiry, cancellation and disconnect
tests; redaction coverage at observable HTTP/log/export boundaries; private
code disclosure; no automatic billed retry after budget exhaustion.

### A4 — Show the benefit on the user's own repository

The matched task-benefit study and repeat-use decision now belong to A0 and
its pilot contract. This recording remains a demonstration, with its original
measurement limits; it is not a separate proof of the A0 outcome.

Status: the actual CodeGraph/Claude Code recording and first-screen media are
merged in [#788](https://github.com/sysevol-ai/CodeNib/pull/788) as `dc2b4df0`,
independently of A2. The clip replays selected CLI output on pinned Requests source; waits
are condensed and source/profile preservation is verified. GIF, WebM, MP4 and
a static poster are served locally, with transcript/provenance and a renderer
that makes no model calls. The source build includes merged #780; a newly
published package install remains to be recorded. A paired agent/token study
and website deployment remain open. Detailed versions and validation
live in #788 and the public CodeGraph recording guide.

- Record about 15 seconds of real `codegraph init`, a Claude Code
  `explore_context` call, and source-linked results. Retain the repository,
  commit, command, tool result, and recording procedure. Label edited waits;
  a 15-second clip is not a promise of 15-second cold setup.
- Build a small opt-in comparison command on the existing evaluation
  machinery only after its metric contract is clear. Compare the same
  repository revision, tasks, model, and budgets with/without CodeNib.
  Report input/output/cache tokens separately, localization quality,
  retries/failures, wall time and recorded cost. A shorter tool response
  alone is not a measured end-to-end token saving.
- Publish one reproducible real-agent result before advertising token
  savings; retain the grep/Jev localization result until then.

Acceptance: the GIF contains actual tool output with usable source locations;
clean-repo two-command setup verified; user bench defaults never trigger
billed calls without an explicit opt-in; paired results include failures.

### A5 — Generate complete Repo Wikis and activate coding agents

Status: navigable visitor Wikis, incremental reading and explicit community
publication are on `main`. Function declarations in landing/demo excerpts
([#821](https://github.com/sysevol-ai/CodeNib/pull/821)) and nested Python indexing
with saved-map backfill ([#820](https://github.com/sysevol-ai/CodeNib/pull/820))
and upfront source admission with saved-map catalog thumbnails
([#822](https://github.com/sysevol-ai/CodeNib/pull/822)) are merged and deployed
at `c7eb07e0` on both `codenib.ai` and `demo.codenib.ai`. Public desktop/mobile
acceptance passes for declarations, saved maps, thumbnails and source rejection.
Landing now
states the hosted size limits; the generation form checks the actual archive
without an API key, saved attempt, index or model call, and gates key entry on
the result. Limits/languages come from the production source constants and
registry. Community and My Wikis cards read their saved System Map, retaining
owner opt-in publication and the separate Featured collection.

The SGLang default branch at `8854857a991e06b3ef9c4caeae34c94fc3588336`
fails the actual 20 MiB hosted archive download check before model calls.
The public smolagents Wiki at `227ef5e49ddd82339295939072f0223249aa8d38`
now has four System Map regions and three links, survives refresh, and displays
its saved map on the Community card. All six eligible missing visitor maps
are backfilled; all 106 pre-existing non-graph envelopes remain unchanged.
Desktop/mobile public SGLang checks reject the oversized archive before key
entry or submission, send no credentials and save no browser/server attempt.
This batch makes no paid generation calls. Broader hosted graph-language
coverage, large-repository admission and the existing prepared-corpus quality
gates remain outside these fixes.

The source-excerpt correction in
[#821](https://github.com/sysevol-ai/CodeNib/pull/821) is merged as `7db7fea8`.
Landing and Wiki excerpts retain a function declaration above cropped body
lines, with an explicit gap and real file line numbers. Long decorated Python
signatures retain the `def` line; highlight ranges are bounded by actual excerpt
length. Cached prepared pages upgrade through a versioned, model-free refresh
without regenerating prose. Saved visitor pages derive declarations from their
saved citation source without a download or stored-prose write. Live
desktop/mobile checks pass for all four bundled landing/demo excerpts and
the cached Requests redirect chapter.

Story browsing is merged in
[#772](https://github.com/sysevol-ai/CodeNib/pull/772) as `f0a4cd00`; static
publication is merged in [#784](https://github.com/sysevol-ai/CodeNib/pull/784)
as `ae4225f9`, not yet deployed. A real 15-page Requests corpus exports from SQLite
with all pages generated and grounding-valid. Export verifies 159 citation
ranges, retains 148 inline excerpts and omits 11 credential-shaped previews
while preserving their repository links. Source/model identity, cache prompt
versions and file hashes accompany the static artifact.

Main includes the graph-boundary, evidence-serialization
and source-range fixes in [#789](https://github.com/sysevol-ai/CodeNib/pull/789),
[#790](https://github.com/sysevol-ai/CodeNib/pull/790) and
[#791](https://github.com/sysevol-ai/CodeNib/pull/791). Desktop and mobile checks
cover all 15 pages, citations, unavailable pages and the local-agent handoff,
with no backend/external requests, browser errors or horizontal overflow.
Export makes no network calls and preserves its source database/configuration.
The local corpus and export are verified, and their dependencies are merged.
An explicit `--trial-api-base` opt-in adds browser-owned OpenRouter queries to
cached story exports with an explicit public repository slug and full commit.
Index-derived previews retain local-agent setup; they reject that opt-in before
repository work because their directory label is not a public source identity.
Wiki header, footer and question-bar controls expose the opted-in trial.
The separate public source app exposes only published repository metadata and
candidate search, reusing the product source authority and grep/chunker path.
It has no credential exchange, model, generation, submission or indexing route.
Allowlisted source identities, origin/host checks, request/response caps and
one-process admission limits bound its work. Source workers own their slots
through cleanup; deterministic concurrent tests verify that limit.

Without the opt-in, static Ask keeps local-agent setup. Existing live Wiki Ask
is unchanged. Local browser acceptance uses real Requests source and fixture
provider calls, with no paid calls. Trial code review is complete in merged
#796 and is released in 0.2.4; actual provider consent, deployment and broader corpus curation
remain open. The CI-built 0.2.4 base wheel exports all 15 cached pages using
its packaged frontend without a CodeNib checkout, frontend override, Node.js,
MCP/auth/graph/model SDKs or a network request. The same installed CLI with
only the `grep` extra also exports the optional trial with a placeholder HTTPS
origin. Both exports leave SQLite, configuration and registry bytes unchanged;
the trial output is acceptance material, not a public deployment.

The maintainer has deployed the live Wiki on their separate DGX Spark.
The acceptance target is a complete, navigable Repo Wiki from an arbitrary
public GitHub URL. #801's bounded source preview and optional one-call
explanation did not meet that target. Its 0.930-second Flask source preview
and $0.025725 explanation are historical measurements, not Wiki generation
latency or cost. The explanation implementation and its dedicated harness
are removed in the visitor-Wiki iteration.

Current iteration: merged in
[#802](https://github.com/sysevol-ai/CodeNib/pull/802), deployed at `63cb8374`
with visitor generation enabled and public desktop/mobile entry checks passing.
URL entry now uses the existing AgentWiki
pipeline with user-funded OpenRouter grep/Jev retrieval, a chapter sidebar,
actual stage/page progress, incremental reading and persisted results. The
maintainer authorized link-readable, unlisted server persistence. Visitors
explicitly consent to that publication and to a server-memory credential for
one bounded generation run; the browser-owned Ask trial is unchanged.
The visitor Wiki database uses the existing WikiStore facade. Temporary
source is bounded and never executed. Refresh reads saved state, while an
explicit owner resume reuses healthy pages without billing them again.

Local acceptance covers archive limits, persistence, cancellation, service
restart/resume, credential isolation, cost stops and a deterministic competing
owner race. Rejected repositories consume no saved-attempt slots; stopping a
queued resume cancels that request before generation. Completed Wikis stop
polling, and a timed-out contender cannot overwrite active progress.
AgentWiki cache identity survives a fresh temporary source download;
ready chapters are recovered without another model call. Provider-free browser
checks cover desktop/mobile creation, progress, stop/resume, read-only sharing
and refresh with no credential persistence.
The complete local unit tier passes with **7,355 passed**, 35 skipped and 190
excluded by marker; the frontend has **138 passing tests**. Production build,
pre-commit, strict MkDocs and public documentation checks pass.

A real run against `pallets/itsdangerous` at
`672971d66a2ef9f85151e53283113f33d642dabd` completed **11/11 chapters**, all
passing source/quality gates: **45 calls**, **$0.838654398** reported by
OpenRouter, first readable chapter in **about 170 seconds**, full completion in
**643.23 seconds**. The $2 run limit was not reached; no charges were unknown.
All 11 persisted chapters pass desktop/mobile navigation, source-citation and
refresh acceptance with only same-origin reads and no model request. These are
one local run's measurements, not a hosted latency guarantee. Source-only
visitor Wikis from that run predate the bounded Python graph path below.
The deployment updates both backend and frontend and enables
`CODENIB_VISITOR_WIKI=1`; the published 0.2.4 wheel predates this path.
Hosted feedback exposed a source admission failure for `AMA-Bench/AMA-Bench`
at `ddfd319e0be33424288c13806f1eafc63e625b59`: a 7.2 MiB PNG stopped the run
before any model call, while empty-chapter UI still implied work was pending.
Oversized files are now skipped before decompression and their paths/sizes
persist with the attempt. Replaying that source preparation retains 65 files
and produces 304 chunks without inference. Stopped runs prominently display
the reason, stop polling, and require an explicit owner retry. Archive path,
retained-size and member-count limits remain enforced. The owner's hosted retry
subsequently completed **14/14 chapters**, all passing the existing grounding
and quality gates: **58 calls**, **$1.177581516** reported cost, no unknown charge,
and **886.09 seconds** from retry start to completion. This establishes hosted
completion for this pinned repository; source preparation alone did not.

New visitor Wikis use a **focused** reading profile: Overview plus at most four
core chapters, with a separate cache identity from full and legacy concise
profiles. The generation form defaults to DeepSeek V4.1 Flash and offers Sonnet
4.6. Model and scope stay pinned on resume. The previous four-claim detail-page
cap was too aggressive: source checks passed while the explanation lost key
mechanisms and sometimes left a fact under the wrong heading. Detail pages now
keep the supported argument and short explanatory leads, remove duplicate
framing/tables/excerpts, and receive one bounded editorial revision when the
existing call budget allows. On new runs, that revision inspects rendered prose
for repeated claims and helper inventories, not just the JSON plan. Revised
plans pass normal source/quality admission. The final page receives the same
newcomer reading rubric as prepared examples; it remains a diagnostic, not a
replacement for source admission or a guarantee of prose quality.
Cancellation and unknown charges cannot fall through to another billed call.

The same native architecture component serves prepared and visitor Wikis.
Existing saved semantic architecture plans become visible without another model
call or source download, with source buttons on roles and connections. These
remain model-authored, source-grounded architecture explanations, not the
compiler-indexed maps. Hosted Python generation now also runs the existing
SCIP indexer against a source-only scratch copy before planning; indexed
relations are available to AgentWiki. It saves the same bounded graph/source
projections used by the prepared reader. CodeGraph opens without a model call
and has pinned source previews. System Map displays indexed chapter regions
even when no cross-chapter calls are recorded, with an explicit explanation.
Completed Wiki pages collapse generation diagnostics and lead with readable content.

Nested Python source indexing and saved-map repair are implemented in
[#820](https://github.com/sysevol-ai/CodeNib/pull/820), merged as `20b24233`.
The old Pyright include pattern indexed no files in `src/` layouts. The repair
indexes the captured scratch root and rebuilds saved graphs at each Wiki's
pinned commit, under the existing generation guard, without model calls or
changes to prose, cost or visibility. The operator dry run reuses the private
read-only maintenance snapshot and leaves source database/lock files untouched.
At the saved smolagents commit, the live result now has 75 indexed files,
2,544 symbols, 6,636 edges, four map regions and three links. The same guarded
repair restored the other five eligible visitor maps. All 106 non-graph
envelopes remain unchanged, including prose, cost and visibility; public
desktop/mobile reading, refresh and Community thumbnail checks pass.

Browse presents featured and explicitly published community Wikis, plus the
owner's My Wikis collection. The homepage uses Continue reading cards with
chapter status, removing attempt IDs and storage-oriented copy. Only the owner
can publish or unpublish; only completed, source-checked pages can be listed.
Older unlisted Wikis stay unlisted. A reader in a fresh browser can discover a
published Wiki without credentials; unpublishing preserves the existing share
link. The WikiStore publication envelope is separate from progress so late
worker saves cannot undo an unpublish. Local publication and browser acceptance
cover these boundaries without provider calls. My Wikis still uses browser
recovery access; this iteration does not add accounts or cross-device ownership.
Completed owner pages explicitly explain that the result is saved in My Wikis
and still unlisted, with **Publish to Community** as the main action. My Wikis
cards show unlisted status and a publication hint; the form explains the next
step before generation. Failed publication stays unlisted and can be retried.

Real workload evidence: the owner's Flash generation of
`fishmingyu/qrv2-gpu-mode` completed four chapters in **42.76 seconds**, with
**22 OpenRouter calls** and **$0.025278336** reported cost. Its short detail pages
were 224/174/259 whitespace-separated words. This proves production-route
completion speed on one repository, not reader-quality parity with the gallery.

The pipeline audit found concrete differences despite both routes using
`AgentWiki`:

| Surface | Prepared examples | Visitor Wikis |
| --- | --- | --- |
| Source retrieval | Prepared BM25/dense artifacts as available, outline anchors and indexed relations | Model-planned grep/Jev and tree-sitter anchors; bounded SCIP relations for Python when indexing succeeds |
| Search task | Chapter query against the index | New runs use a chapter-specific prompt; old runs used the issue-location prompt |
| Supporting source | Outline and context can include tests | New chapter search includes tests; the legacy grep filter silently excluded them |
| Reader assessment | Enabled by default; diagnostic | Enabled for new runs; absent from the two existing saved Wikis |
| Reading scope | Fuller outline with child chapters | A small chapter list; legacy concise detail claims were also capped |

New attempts pin `wiki_grep_jev_v2` as their retrieval/cache identity. Old
attempts retain their previous retrieval policy and review budget on resume.
The ordinary issue-search, MCP and browser-trial defaults remain unchanged.
A deterministic audit of the pinned Requests source with the same redirect-test
search produces **0 vs 23** grep candidates and **2 vs 12** writer evidence
items before/after enabling supporting source. This isolates the filtering bug;
it includes no model call and does not measure final ranking or prose quality.
It uses the existing pinned checkout and excludes hosted archive admission.

The prose comparison freezes the existing QR run's commit, outline and retrieved
evidence and uses DeepSeek's official API. All four newly composed pages pass
ordinary source/structure checks. The reader rubric gives 7/8, 5/8, 3/8 and 8/8;
the recursive-panel chapter still fails its failure/boundary explanation. Three
of four pass the rubric. Re-reviewing the prior focused output also found
repetition despite passing source checks: extra words alone did not fix quality.
These are model judgements on one repository, not an independent human grade.
The comparison excludes retrieval, benefits from warmed provider caching and
is **not** a new OpenRouter end-to-end timing result. Broader reader-quality
assessment, non-Python hosted graph coverage, and runtime/embedded-kernel
relationships remain open gates. Do not claim gallery parity or invent edges.

The regenerated QR Wiki at commit `527ec55cfffeed8eefa5e1500c80d00265e99721`
exposed a separate render failure: the label “Compact `(H, tau)` artifact” contained inline
code typography and caused the whole architecture contract to be rejected.
The deterministic repair now flattens inline typography inside prose role labels
while still rejecting labels that consist only of source symbols or paths. The
existing six-role plan renders without rewriting prose or using a model. Missing
architecture and graph failures are explicit in the reader instead of silent
omissions behind a ready chapter.

A same-commit source/graph probe took **3.29 seconds**, including download,
chunking, indexing and four reader projections, with no model call. SCIP emitted
365 nodes and 1,345 edges; these include containment and references, not 1,345
calls. Four bounded chapter CodeGraphs are available. The initial Overview view is
limited to its cited symbols and shared bridges (12 nodes / 5 edges), rather
than a large neighbourhood dominated by uncited implementation helpers. No direct cross-chapter
connections were found for the cited symbols; the three System Map regions
therefore have no invented arrows. Python indexing is capped at 60 seconds and
16 MiB of SCIP output; cancellation terminates the owned child process. It uses
only captured Python source, an empty external-package environment, and no
repository configuration, package installation or repository execution.
Unsupported languages, missing tools, timeout and projection failure leave the
source-checked prose available and display the missing graph coverage.

The existing QR result was supplemented under the generation guard, preserving
all four page envelopes, cost accounting and publication state. No visitor key
is reused and old results are never automatically published. Tests cover the
reported typography case, exact reference/source previews, isolated indexing,
stop/timeout cleanup, and read-only persisted graph routes. The Wiki/Web/grep
unit tier passes **1,348 tests**, with one skip; frontend tests pass **142**.
The prepared cache audit preserves 1,001 ready and 19 pre-existing retryable
pages across 27 repositories, with 23 existing System Maps available.

Live feedback still distinguishes model response receipt, worker progress and
server connectivity, with immediate submit feedback, progressive chapter reads,
45-second waiting explanation and stopped animation after 180 seconds without
progress. The extra editorial pass reports its actual review stage. Refresh and
reconnect remain read-only, and existing paid runs must drain before deployment.
The review chain is #803 → #804 → #805 →
[#806](https://github.com/sysevol-ai/CodeNib/pull/806) →
[#807](https://github.com/sysevol-ai/CodeNib/pull/807) →
[#808](https://github.com/sysevol-ai/CodeNib/pull/808); restack it in that order
before merging. Deployment does not imply the stack is merged.

The community, visitor-quality and #808 graph fixes are deployed as of
2026-09-27. The reported QR result now renders its saved six-role architecture,
three indexed chapter regions, and four CodeGraphs at its original URL. Public
desktop/mobile checks pass without provider calls. The graph-node preview,
pinned source link and map-failure retry were also exercised. The three saved
visitor Wikis were preserved, with no automatic publication or prose rewrite.
The main services run `ba27c81b`; the verified staging services were drained and
removed after cutover, and the previous asset hashes and database snapshots are
retained for recovery.
Acceptance passes 1,341 Wiki/web/grep-MCP unit tests (one skipped), 142 frontend
tests, production build, strict docs and the provider-free desktop/mobile
publication and waiting matrices. Publication acceptance includes the completion
invitation and failure/retry path. Public browser checks render the existing visitor architecture with
seven roles and pinned-source navigation, keep completed diagnostics collapsed,
preserve unlisted visibility and refresh without mutations. The two existing
visitor Wikis and all 27 prepared repositories remain readable. Cache coverage
is unchanged: 1,001 ready pages, 19 pre-existing retryable pages, zero cold pages
and 23/27 indexed System Maps available. Database snapshots, service backups and
prior frontend assets are retained; visitor work was idle before the backend
restart. The previous concise prose is not automatically rewritten: owners can
start a new version from the result header with a new funded request.

| User action | New behavior | Who pays / data boundary |
| --- | --- | --- |
| Open home | Paste a GitHub URL, continue reading, or open Browse to discover ready Wikis. | Catalog metadata and static assets; no model call. |
| Open a repository | Precomputed overview and story Wiki, architecture map and source citations. Choose an example task to inspect its recorded evidence. | Static assets; preserve source/model provenance. |
| Explore a map or citation | Browser renders exported page boundaries/graphs and navigates to commit-pinned source. | Static data; disclose missing full runtime graph functionality. |
| Enter a new question | Explain “Connect OpenRouter” and the snippet/cost boundary, or “Run on your machine”. Do not spend the site's key on anonymous requests. | User-authorized provider account; bounded request. |
| Connect an agent | Copy a tested local setup now. Add remote MCP only after A6 passes. | Local user machine and user's agent/model. |
| Submit a public GitHub repository | Open a prepared Wiki, or explicitly generate a focused Wiki with chapter progress and saved links. | Bounded temporary public source on the server; user-funded OpenRouter; unlisted Wiki-only persistence. |

Implementation boundaries:

- Separate public-preview mode from the existing local Wiki server. A local
  user may intentionally configure their own backend model; do not break it
  by globally disabling Ask or generation.
- Reading public pages must not invoke outline generation, Wiki generation,
  an LLM fallback, or graph label generation. Missing precomputed pages show
  a useful unavailable state. Offline generation runs with an explicit
  operator budget and a versioned input/model/prompt identity.
- Reuse the SQLiteWikiStore facade for Wiki persistence and the existing
  static exporter. Verify story prose, source snippets, page boundaries and
  system-map export parity with #772; deterministic WikiBuilder output is
  not automatically the generated story cache.
- Do not embed all live demo pages as landing-page iframes: use a static
  preview, then let a deliberate click open the demo. Visits should not
  implicitly hit Spark or start model work.
- Distinguish public examples, session-specific Ask results, and saved
  operator-generated Wiki content. A visitor's key must not silently finance
  shared Wiki generation or persist its result across users. Visitor-created
  Wikis are a separate, explicitly consented link-readable publication; their
  credentials stay in the active server run and are never saved.
- Check desktop/mobile, empty/error/auth/budget states and keyboard/source
  navigation. Test the static build with backend access blocked.

Acceptance: browse all exported pages with zero inference calls; 0 backend
requests from the marketing preview; reliable commit-pinned citations;
successful disconnect and budget exhaustion; one clear action from preview
to local agent activation. The public site remains usable during Spark outage.

### A6 — Expand distribution with measured demand

Status: the existing Pages workflow and composite Action default to `fast`,
which produces a static index-derived preview and portable BM25 artifact without
an embedding download. The real publication smoke exercises this default and
checks the resulting artifact views. The public example pins the accepted v0.2.4 commit and passes `preset: fast`
explicitly. The workflow default is now released; users retaining v0.2.3 still
need the explicit setting to avoid its semantic default. Semantic views remain opt-in. This does
not substitute BM25 for the selected grep/Jev query route. Cached-story
generation in Actions, curated-corpus expansion and hosted services remain separate gates.

1. Improve the existing Pages workflow and generated-site backlink. Pin all
   external actions. Offer a no-embedding preview and explicit OpenRouter
   generation using repository Actions secrets; never export credentials.
2. Start with 5–10 useful public repositories, covering the measured language
   groups. Precompute pages and graph payloads once, publish static assets,
   measure click-through and successful agent starts, then consider 100–200.
   Check size, source licensing and refresh cost before each expansion.
3. Prototype remote MCP for that curated corpus only. Reuse existing query
   runtime over commit-bound artifacts; do not add a generic storage system.
   Test Streamable HTTP, OAuth/bearer behavior, origin/host validation,
   per-user/IP limits, request/result caps, timeouts and disabled arbitrary
   tool execution. Most model generation stays with the user's agent, but
   OpenRouter planning/Jev remain paid operations requiring their own budget.
4. Add arbitrary hosted indexing only after demand justifies it: repository
   access verification, size/time limits, daily global cap, bounded queue and
   cancellation. “Request a repository” is the first lower-cost alternative.

Acceptance: static traffic does not reach Spark; artifact commit verification
survives updates; generated backlinks work; a real remote MCP client connects
before publishing `mcp.codenib.ai` setup instructions. No speculative service
or unbounded inference is required to finish A1–A5.

## Measurement and rollout

[CodeNib 0.2.4](https://github.com/sysevol-ai/CodeNib/releases/tag/v0.2.4)
is published on PyPI, the official MCP Registry and GitHub, with the stable
GitHub Release marked latest. Its annotated tag resolves to
`3f3bc2cf44ba83d1d86e9e5eb74f8da0f8bc63ac`, the exact commit accepted by
[TestPyPI run 36252579501](https://github.com/sysevol-ai/CodeNib/actions/runs/36252579501).
[Production run 36252998442](https://github.com/sysevol-ai/CodeNib/actions/runs/36252998442)
passed all artifact, installed-service, protected ownership, public PyPI and
Registry discovery gates. Both manylinux wheels and the sdist have matching
SHA-256 values in PyPI metadata, GitHub assets and the release checksums.
The #779/#780 MCP version correction is now available in the public package.

Post-publication [#798](https://github.com/sysevol-ai/CodeNib/pull/798) switches
installation pins and their contract check to verified 0.2.4, removes the
candidate navigation label and uses the packaged frontend in preview guides.
All Pages examples pin the full accepted release SHA with the version comment.
The clarified CodeGraph comparison names `colbymchenry/codegraph` and uses its
pinned source and documented capabilities. OAuth onboarding and the browser
trial remain opt-in while real provider consent and interactive unlock are
unverified. The maintainer chose desktop CLI login acceptance first and will
has deployed the live Wiki on their separate DGX Spark. The URL-entry frontend still requires its own deployment acceptance.

The release used the existing protected environments after explicit maintainer
approval. TestPyPI admits `main` and production admits `v*` tags; those rules
remain intact. Manually dispatched
[Docs run 36252581263](https://github.com/sysevol-ai/CodeNib/actions/runs/36252581263)
passed, with verified anonymous docs and README image access before the tag.

Native ARM verification in [#799](https://github.com/sysevol-ai/CodeNib/pull/799)
keeps the complete pinned cibuildwheel step, ownership/protocol smoke and every
other job unchanged. All executed checks passed before merge. The native ARM
job took 2m35s; the preceding accepted QEMU job took 29m34s. This is an observed
pair of CI runs, not a controlled hardware or product benchmark. The accepted
main and TestPyPI runs both use that workflow and pass the complete x86-64 and
AArch64 manylinux verification plus Python 3.10–3.14 installed ABI3 checks.

Local candidate validation passes 7,318 unit tests, 62 release/Registry/CI
contracts, strict public docs and the release all-files pre-commit check.
The local x86-64 ABI3 wheel and sdist pass build and metadata checks; they are
not the production pair of manylinux wheels. Python 3.12 with MCP SDK 2.2.0
reports 0.2.4 in modern and legacy handshakes. Real installed Wiki, checkout MCP
and portable-artifact MCP smokes pass, with a permanent installed-version guard
in the release harness. Fixture-backed source-only MCP and the public source
API pass without embedding/model/graph SDKs or billed calls. Baseline formatter
failures are corrected in a separate commit with unchanged production
non-import ASTs and 85 focused tests. Public TestPyPI and production publication
are complete. Real consent remains open; the next URL-entry deployment acceptance belongs
to the maintainer’s DGX Spark rollout.

Record baseline and subsequent counts for documentation-to-install clicks,
successful local setup, first source-linked query, repeat use, Pages backlink
visits, and OpenRouter connection completion. Prefer opt-in local reports and
aggregate web events; do not collect code, prompts or credentials. Establish
baseline measurements before assigning numerical conversion targets.

Release in focused PRs: evidence/onboarding, production retrieval,
authorization, demo mode/export, and distribution. Keep native onboarding
stacked on #783; reconcile the Wiki producer fixes before #784. Each milestone lists current
outcomes, remaining gates and PRs rather than an implementation diary.

The broad goal stays active until the requested core surface is implemented,
locally verified, documented, and reconciled with relevant PRs/issues. A draft
plan, one merged README, or a green unit suite alone is not completion.

## Evidence and external contracts

- Public [grep/Jev report](https://codenib.ai/blogs/jev-model-grep-reranking/);
  internal committed source: `docs/experiments/jev_dense.md`, results JSON
  and per-case CSV. Keep excluded experiment paths out of public-page links.
- [OpenRouter OAuth PKCE](https://openrouter.ai/docs/guides/overview/auth/oauth)
  and [Jev Decisions](https://openrouter.ai/docs/guides/community/jev).
- [Language capabilities](language_capabilities.md),
  [method compatibility](agent_integrations.md), and
  [DGX Spark deployment](guides/reference-deployments/dgx-spark.md).
  A deployment recipe is not evidence of a token-saving claim.
