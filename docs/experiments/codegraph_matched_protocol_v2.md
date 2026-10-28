# Prospective corrected four-arm pilot, v2

This is an independent future cohort on the same three historically exposed
repositories. It tests the current CodeNib source candidate with corrected,
source-checked acceptance and verified unattended setup. The original v1
cohort retains every task, failure, timing, assessment and insufficient-evidence
decision. No unseen-task or population-wide claim follows from either cohort.

The complete [execution policy](codegraph_matched_policy_v4.json),
[tasks](codegraph_matched_tasks_v2.json) and
[executable/runtime binding](codegraph_matched_binding_v2.json) freeze at
**2026-10-28 14:04:01 UTC**, before any v2 query. The
[freeze receipt](codegraph_matched_binding_v2_freeze.json) authenticates that
binding. Mutable execution outcomes belong in a separate record, not in these
frozen inputs.

## What changes and what stays fixed

There are fifteen task prompts across Requests, Flask and pytest, each run with
native tools only, Serena, `colbymchenry/codegraph`, and CodeNib. Every repository
contains lookup, flow, impact, repair and controlled post-edit work. Prompts,
membership, arm order, source additions, deadlines, primary/secondary formulas
and native repair grading are unchanged from v1.

One criterion changes before new inference: pytest's direct collection factory
call belongs to `pytest_make_collect_report`, while `collect_one_node` dispatches
the report hook and consumes its result. The
[source review](codegraph_matched_source_review_v2.json) binds all forty
explanation criteria to forty-eight actual source definitions/tests. Source
examples support review and are not an exclusive list of acceptable citations.
Reference patches, these review notes and grading feedback remain outside all
model inputs. The prior retrospective correction is not relabeled prospective.

The CodeNib candidate is the independently built `0.2.4` source wheel bound to
`733343e8`; it is not the public PyPI wheel. It includes identity recovery,
same-connection reload, visible recovery errors and explicit static-reference
semantics. Its published release is unchanged. Serena explicitly selects
Python at creation and prepares cached Pyright; the isolated runtime uses
offline uv resolution. Codegraph keeps its default daemon behavior.

## Agent, tools and limits

Every arm uses native Claude Max authentication, Python SDK `0.2.174`, CLI
`2.1.310` and exact main assistant `claude-opus-5-5`. Medium effort is requested
and recorded from hooks where exposed. Adaptive thinking remains enabled;
the SDK requests a 16,384-token output ceiling and 48 turns per query.
Current provider constraints are checked before this freeze against the
[model specification](https://platform.claude.com/docs/en/models/opus-5-5/overview),
[tool-choice rules](https://platform.claude.com/docs/en/models/opus-5-5/migration-guide)
and [SDK reference](https://code.claude.com/docs/en/agent-sdk/python).

Native Read, Grep, Glob, Edit, Write, Bash and ToolSearch remain available.
Optional tool selection stays automatic; no task requires a product call.
Actual server instructions and available tools remain part of the selected
product. Account settings, skills and plugins are disabled in the study SDK
configuration. Each arm has its own checkout, container and client profile;
the native Bash/MCP runtime has networking disabled and no host credential,
Docker socket, another arm or grading material mounted.

A cold lookup starts in one new SDK session. Flow, impact, repair and post-edit
then share a separate return session, preserving its MCP connection across the
identical controlled source addition. Native tools can handle ordinary tool
failures or choose a rebuild; their work counts. Setup failures retain elapsed
cost and native fallback. Do not substitute grep/Jev for the graph product or
infer graph benefit from a successful native fallback.

SDK completion must have `subtype=success`, `is_error=false`,
`terminal_reason=completed`, `stop_reason=end_turn` and no API error status.
Error, refusal, output/turn/budget-limit and timeout terminals fail acceptance
even when partial text or a patch exists. Keep the failed task and unattempted
remainder; do not restart a task or replace an unsuccessful workflow.

## Benefit and accounting gates

The primary endpoint remains complete five-task workflow wall time summed over
the three paired repositories. Start before arm-specific installation and
initialization; stop after the final SDK close. Count installation, session
startup, native/MCP work, errors, retries and updates. Separately record common
machine/source/project preparation and later controller grading. For rejected,
timed-out or unattempted tasks, add the remaining frozen task deadline as the
same failure penalty used in v1.

Among alternatives with the maximum accepted-task count and no serious
correctness error, choose the lowest complete penalized cohort time. CodeNib
must at least match that accepted count, add no serious error on a paired task and
improve the complete total by at least 20%. No eligible complete alternative
means insufficient evidence. Keep every paired outcome and the original
repository-cluster uncertainty calculation; fifteen tasks are not fifteen
independent samples.

The secondary gate remains normalized model token volume, with no more than
10% regression. Count each session's final cumulative main and auxiliary
input, cache creation/read and output counters once. Do not add thinking twice
or sum cumulative phase results. Missing final usage stays unknown/lower-bound
and stops the next workflow. SDK estimates remain separate from unknown
invoice charges, as specified by the
[cost documentation](https://code.claude.com/docs/en/agent-sdk/cost-tracking).
SDK turns are not complete HTTP request counts. Requested ranges, summaries
and elided output do not establish source-read credit or a ContextBench score.

The user-authorized API ceiling remains $500. This cohort uses the native Max
route with included-only rate checks, no supplied API key and no paid fallback.
Each workflow has a $20 SDK estimate cap shared across its two sessions; the
planned cohort estimate cap is $240. Earlier recorded estimates total
$10.5268443 and are not invoice charges. Stop starting workflows on quota,
authentication/billing changes, missing usage or an unexpected configured
model/runtime identity; retain the incomplete cohort instead of changing route.

## Admission and grading

The [admission archive](codegraph_matched_v2_admission_2026_10_28.json) retains
all sixteen development preparations, including two controller failures and
the final twelve successful exact-driver preparations. Each selected product
installs, its project/product/provider identity matches the prior admission,
and its first lookup returns current source. Shared native editing and cleanup
pass. All thirty-one owned preparation/help containers are confirmed absent.
The earlier source-addition controls and their pending/recovery costs remain
separate evidence from these first-query checks.

Driver checks cover committed repair diffs, source preservation, excluded
product caches, cumulative main/auxiliary usage, missing latest-query usage,
fourteen invalid terminal cases and returned quota errors. The adapter and six
original native empty/reference controls are unchanged. Each empty control
collects the intended selected tests and fails where its reference passes.
Native repair grading uses the original SWE-bench Verified `4.1.0` tests,
parser, denominators and pinned arm64 images after the model batch is sealed.
Keep collection errors, patch conflicts and behavioral failures distinct.

One maintainer-agent performs source review using the pinned source, actual
delivered observations and frozen criteria; independent external adjudication
is not available. Record unsupported material caller/order/propagation claims
as serious errors, and missing coverage without a false claim as ordinary
acceptance failures. Do not amend this rubric after seeing v2 failures.

This freeze does not establish voluntary use, a benefit or external repeat
use. Broader provider/workflow coverage, automatic rebuilding and five
consenting users' seven-day observations remain separate gates under
[#874](https://github.com/sysevol-ai/CodeNib/issues/874).
