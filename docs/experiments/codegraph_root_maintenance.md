# Root maintenance decision: hold CodeGraph promotion

This exposed internal source audit produces no accepted answer and no CodeNib
efficiency win. Keep CodeGraph promotion on hold, finish the reliability work,
and inspect unused initialization cost before funding another similar cohort.
The complete-workflow benefit and external-use gates remain open.

## Result and product decision

Each condition answers the same one-question audit of concurrent ARB provider
roots on CodeNib source `c25f191c`. Total time includes common preparation,
fresh installation, initialization/indexing, the SDK question and cleanup.

| Condition | Cold total, seconds | MCP calls | Final words | Source criteria |
| --- | ---: | ---: | ---: | ---: |
| Native | 51.254 | 0 | 609 | 6/6 |
| Serena | 63.931 | 2 | 560 | 5/6 |
| colbymchenry/codegraph | 126.104 | 1 | 601 | 6/6 |
| CodeNib | 74.674 | 0 | 550 | 5/6 |

All four exceed the prospectively frozen 450-word limit, so acceptance is
**0/4**. Every SDK terminal succeeds and every source-preservation check passes.
The source judgments come from one unblinded maintainer review. Serena and
CodeNib omit the condition that makes a later preparation fetch again: a
reusable successful graph skips checkout cleanup. Native gives a retry
alternative; Codegraph explicitly selects a failed-build retry. The detailed
review retains qualifying notes and the original answers.

CodeNib takes 16.8% more total time than Serena and 45.7% more than Native on
this one case. Its 12 MCP tools are connected and advertised in the actual SDK
initialization; the model uses native tools throughout. Serena's two calls are
initial instructions and a symbol outline. These observations establish neither
a causal tool effect nor general product efficiency. The earlier complete
cohorts and their failed repairs remain unchanged.

The round records 851,304 normalized final main/auxiliary tokens and $1.4367266
in client-side SDK estimates, not invoices. CodeNib's estimate is 7.3% below
Serena's, below the unchanged 20% benefit threshold and accompanied by a failed
answer. Its normalized tokens are 92.1% above Native's. Four question attempts
are distinct from assistant turns and the unknown HTTP request count.

The next bounded action is local inspection of cold initialization, which
takes 12.600 seconds in the CodeNib condition despite zero subsequent product
calls. Preserve opt-in navigation and explicit preparation costs. No further
cohort of this kind is planned without a product change or a real consumer
need. Independent successes remain 0/2; consenting external users remain 0/5.
The broad objective stays active and the remaining confirmation set stays
unopened.

## Frozen setup and evidence limits

The [selected exact-record packet](codegraph_root_maintenance_2026_10_31.json)
binds the question, criteria, all answers, original run receipts, retained
failures and source review. Full SDK/hook streams, original control receipts
and remaining source snapshots stay local with SHA-256 bindings; the packet is
not a complete public execution transcript.

All conditions use Claude Agent SDK 0.2.174, Claude Code 2.1.310, Opus 5.5 at
medium effort, adaptive thinking, automatic tool choice, a requested 16,384
output-token limit including thinking, and 24 turns. Actual cumulative usage
also includes Haiku 5.5 auxiliary activity. Each question gets a fresh session,
600 seconds, two CPU cores and 4 GiB. The source-only task has no prepared
project runtime or tests. Native target-project APIs remain allowed; no such
invocation occurs. No product call is forced and no subagent is enabled.

CodeNib uses the unmodified installed CI wheel `8b3ca291`, with all 626 product
files verified, a full MCP surface and explicitly deferred graph preparation.
Serena 2.1.0 uses its REPL interface; Codegraph 1.6.2 uses its standard MCP
entry. All four final controller preparations pass before inference. The same
real pinned Claude executable is supplied in every container, without login
data or an SDK home. The existing signed-in subscription route is preserved;
there is no API-key, model, provider or account fallback.

The originals retain the local-image reference failure, duplicate CA mount,
incorrect ownership-probe umask and missing container client. Those are
preparation/controller failures. Their repairs precede the final four controls.
They are not competitor failures or model outcomes.

Twelve native Read observations match their file-bound captured source and the
exact numbered text delivered in actual SDK inputs. This partial audit grants
no source credit to arbitrary shell, grep or MCP output and produces no
complete ContextBench score. A diagnostic initially misinterprets the SDK's
trailing empty EOF marker and optional native ToolResult error flag. Both
assertions and corrections remain retained; the strict final SDK terminal,
answers, times and word-limit outcomes stay unchanged.

## Useful maintenance outcome

A separate deterministic witness runs real provider preparation, Git
fetch/reset/clean and FileLocks. B passes its root check before A publishes,
then creates its workspace below A's checkout. A later failed-build retry
removes B's nested workspace; the peer-directory control survives. Graph
construction and implementation-identity probes are stubbed, so this witness
does not validate a graph backend or establish agent benefit.

The public ARB guide previously overstated protection for overlapping roots.
It now recommends provider-owned peer areas and explains that existing receipt
checks do not reserve unpublished paths. Concurrent nesting and swapped roots
remain unsupported. Tracking issue [#867](https://github.com/sysevol-ai/CodeNib/issues/867)
is restored after GitHub's timeline confirms that a negated closing keyword in
#951's body automatically closed it. Repository guidance now requires direct
open-issue wording and a post-merge issue-state check. No root-role ownership
framework is implemented or promoted by this work.
