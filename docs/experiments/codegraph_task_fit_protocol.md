# Evaluate voluntary exploration on a real source-identity repair

This development round asks whether task-oriented tool wording leads an agent
to use CodeNib's indexed exploration and produce useful source-grounded work.
One known CodeNib bug supplies a diagnosis, a repair and a review. It cannot
establish either independent confirmation set or a general benefit claim.
The [execution seal](codegraph_task_fit_protocol_2026_10_30.json) retains the
exact input hashes and final operator admissions before the first model call.

The main backend includes merged [#915](https://github.com/sysevol-ai/CodeNib/pull/915).
The candidate changes only the exploration tool description and its server
instructions. Both CodeNib conditions use the same repaired backend; native
tools stay available and no product call is required. The earlier negative
Flask round and both frozen cohorts retain their original outcomes.

## Conditions and task

The fixed execution order is Codegraph, candidate CodeNib, Serena, unchanged
CodeNib wording, then native tools. Codegraph is pinned to 1.6.2 and Serena to
2.1.0. Both internal CodeNib wheels retain the 0.2.4 package label and are
source-bound development artifacts, not a new public release. Their 485
Python files differ only in the two metadata files; all native bytes agree.
The controller checks the actual installed Python/native bytes per condition.

The target is a single-commit archive of CodeNib before the source-seed fix in
[#902](https://github.com/sysevol-ai/CodeNib/pull/902). Ranked property snippets
can identify the right source while graph expansion misses its physical
definition. All conditions receive the same three prompts, source and prepared
project dependencies. There is no private reference, grader or grading feedback
in model inputs. This is an exposed self-repository case; installed product
implementations and existing project knowledge limit generalization.

Each workflow has one fresh diagnosis session, then a separate return session
for repair and review. Diagnosis and review are read-only and capped at 650
whitespace-separated words. The selected native repair control has 26 cases:
the original source fails six and passes twenty; the reference passes all 26,
with the same selected IDs collected and executed. Private test/reference
contents remain unpublished until every model workflow is terminal.

## Execution and costs

The agent uses Claude Code 2.1.310 through Agent SDK 0.2.174, with Opus 5.5,
medium effort, adaptive thinking, automatic tool choice, 24 turns and a requested
16,384-token output limit. Native read/search/edit tools remain available.
Upfront MCP loading is requested consistently, and actual initialization and
delivered tool results are recorded. Producer metadata and requested flags do
not establish complete downstream HTTP schema visibility.

The selected model's adaptive thinking is always enabled and forced tool use
is unsupported. The output request is below its documented maximum.
[Model reference](https://platform.claude.com/docs/en/models/opus-5-5/overview).
`MCP_TIMEOUT` governs startup; stdio calls have no HTTP per-request timer. The
shared task/workflow deadlines still apply to a long first graph build.
[MCP reference](https://code.claude.com/docs/en/mcp).

All workflows use their own credential-free container, source checkout and
product environment under two CPUs, 4 GiB and 256 PIDs. Network access permits
provider installation; acquiring external repository answers or grading
material violates the source-only task policy. No shared home, Docker socket,
other condition or private grading tree is mounted. The host runs the signed-in
native SDK with subscription authentication and no automatic API fallback.
[Plan usage reference](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan).

The primary clock starts before optional product installation and stops after
the final SDK disconnect. Include provider preparation, first graph queries,
rebuilds, retries and failures. Common project preparation and later evaluator
grading are separate. The 247.965-second first graph control in #915 is a
retained cost, not a speed result. Operator admission calls do not count as
voluntary use, and their prepared graphs are not reused by model workflows.

Task limits are 300 seconds for diagnosis, 540 for repair and 300 for review;
setup is bounded at 900 seconds and the workflow at 3,000. Retain actual time
and a separate deadline penalty for failed or unattempted work. A failed SDK
terminal stops that workflow; quota, billing or missing-usage failures stop
starting further workflows. No model rerun replaces a failed observation.

The authorized paid-API ceiling remains $500, with native subscription usage
preferred. A prospective $3 estimated-usage guard applies between each
workflow's queries; stop starting workflows at $20 observed round estimates.
The plan is $15 estimated usage. Query-local client estimates can overshoot, so
these are not hard invoice caps. Sum final cumulative main and auxiliary token
counts once per session, including cache and output; retain incomplete values
as lower bounds. Estimates and SDK turns do not establish invoice charges or
complete HTTP request counts.
[SDK reference](https://code.claude.com/docs/en/agent-sdk/python).

## Admission and interpretation

Both original CodeNib preparations failed at an instance-field write before
inference. A controller audit also found that the intended old-wording control
installed the candidate wheel. Keep those failures; they do not compare
metadata. The next controller selects each condition's own artifact, verifies
all installed code, and retains available compiler artifacts on failed
preparations. Corrected development preparations pass on all five conditions.
The final driver/policy receives another exact admission before its separate
execution seal; no model starts without that seal.

Both explanations need every fixed source criterion, successful terminal,
unchanged source and the word limit; material contradictions remain serious
errors. Repair acceptance requires all 26 native cases. One unblinded
maintainer-agent source review is available; independent adjudication is not.
Begin native grading only after all model workflows are terminal. Distinguish
collection failures, grading infrastructure errors, model/native test conflicts
and behavioral failures, and retain every selected outcome.

Use these observations to choose the next engineering change. One exposed
repository and its three related tasks are not independent samples. Even a
faster accepted development result does not satisfy the full program's two
independent confirmation sets, quality parity, 20% whole-task improvement or
normalized-usage guard. External enrollment remains 0/5; recruitment and
seven-day reuse remain open, with no outreach authorized. Track the full
objective in [#874](https://github.com/sysevol-ai/CodeNib/issues/874).
