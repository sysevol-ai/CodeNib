# Guided startup review: use occurs without a qualifying advantage

An explicit instruction gets CodeNib used in this internal source-review task.
The guided answer uses fewer model tokens, but takes more complete cold time
and misses two required source criteria. Keep comparative promotion on hold.

## Result

Two fresh agent sessions review whether BM25 preparation can move from startup
to the first query while preserving source safety. Native uses its usual tools;
the CodeNib condition is instructed to call `explore_context` once and may then
use native tools freely. This measures conditional use, not voluntary adoption.

| Measure | Native | Guided CodeNib |
| --- | ---: | ---: |
| Complete cold time, seconds | 112.915 | 116.120 |
| Question phase, seconds | 109.326 | 95.019 |
| Actual SDK tool calls | 26 | 20 |
| Included MCP calls | 0 | 1 |
| Normalized final main/auxiliary tokens | 881,821 | 581,218 |
| SDK estimate, USD | 1.0547224 | 0.7925121 |
| Source criteria met | 7/7 | 5/7 |

Guided CodeNib takes 2.8% more complete cold time. Its question phase is 13.1%
shorter, normalized usage is 34.1% lower and its SDK estimate is 24.9% lower.
These are descriptive observations from one task, with different answer
coverage and no independent adjudication. Tool calls can contain multiple
shell operations; their count is not a count of searches or model turns.
SDK estimates are not invoices.

## What the source review finds

One unblinded maintainer applies seven criteria fixed before either model
question. Both answers discuss deferring the loading of an existing BM25
artifact, rather than removing its earlier construction during CLI onboarding.
Neither proposes an implemented or fully tested patch. References to dirty
source must mean divergence from authenticated indexed bytes: Git-dirty source
that was freshly indexed must remain usable.

The guided answer omits the CLI construction entry point and confuses the
agent runtime explorer with the MCP exploration path. It proposes loading
BM25 in search helpers, inside the query boundary. The actual MCP
`explore_context_impl` checks whether a ranked view is loaded **before** calling
those helpers. With no loaded view, its new loader would never be reached.

A post-terminal offline control uses the unmodified installed product, verifies
all 626 product files and changes only the context's in-memory loaded-view
state. With the loader in the suggested helper, the original exploration path
calls that helper zero times and returns zero files. Loading the view before
the capability check calls the helper once and returns five files. This is a
counterexample to the described insertion boundary, not an executed complete
model patch or a regression in the unchanged product. The answer also places
compiler-lock acquisition below the indexed-query lock, conflicting with the
server's documented lock order.

The native plan identifies the earlier CLI work, the MCP capability gate and
the compiler-before-query boundary. Its accepted source review does not prove
that a future implementation will be correct or faster. No causal claim about
the tool producing either answer's quality follows from this one pair.

## Actual use and setup

The original MCP reply equals the decoded observation delivered in the SDK
stream, including every source body and projection marker. The request sets
`include_dependencies=false`. It delivers eight windows from the MCP server
and manifest-reload tests through BM25, with graph routing explicitly
unavailable. Native observations receive no source-read credit in this partial
audit, and no ContextBench score is calculated.

A preceding manual readiness query takes 1.099 seconds after 14.608 seconds of
setup. It finds relevant guards and tests but misses the CLI construction entry
and includes unrelated scripts. That query is not a scored comparison arm.

The two scored conditions use exposed source `c25f191c`, installed candidate
wheel `8b3ca291`, Agent SDK 0.2.174 and CLI 2.1.310. The main model is Opus 5.5
at medium effort with adaptive thinking and automatic API tool choice; final
usage also includes Haiku 5.5 auxiliary work. Each receives two CPUs, 4 GiB,
256 PIDs, 24 turns and a 600-second question deadline. Native runs first. Both
original SDK terminals succeed, usage is complete and source is preserved.
Selected initialization/exploration function ASTs still match inspected main
`2a28e48b`; this is not a current release-wide admission.

## Protocol and decision boundary

The [selected exact evidence](codegraph_guided_startup_2026_11_01.json) retains
the sealed prompts/criteria, original final answers and terminals, exact MCP
exchange and confirmed delivery, source judgments and counterexample. Full
execution/SDK/source originals remain local and hash-bound; this is not a
complete public transcript. The witness's initial CA-volume and manifest-schema
controller errors and the collector's field-name correction remain recorded.

This new, separate source-review protocol has no word-count acceptance gate.
Every earlier frozen criterion, word-limit rejection, task and timing stays
unchanged. Its selected primary endpoint is complete cold time, so lower SDK
estimates do not replace a failed time/quality gate after observing outcomes.

The pair adds two questions, 1,463,039 normalized tokens and $1.8472345 in SDK
estimates through the existing included-plan route, with no API-key/account/
provider/model fallback. Invoices and total HTTP request counts remain unknown.
No model request belongs to the manual query or the offline counterexample.
All owned containers are absent at closure.

No typed-graph benefit, independent success, external participant or qualifying
whole-workflow advantage is established. Preserve opt-in navigation and the
open gates in [adoption A0](../adoption_roadmap.md) and
[#874](https://github.com/sysevol-ai/CodeNib/issues/874). Result coverage and
initialization cost remain concrete candidates for consumer-driven improvement;
this one exposed task does not authorize repeating cohorts to obtain a win.
