# CodeGraph natural-use development workflow

This exposed-source development round does not establish a CodeNib benefit.
CodeNib finishes faster than Serena, but slower than native tools and Codegraph,
uses more model tokens, and has lower source-answer acceptance. Neither CodeNib
nor Serena is voluntarily called. Hold promotion and preserve the complete run.

This is one known Flask checkout with a synthetic request-hook regression,
followed through diagnosis, repair and review. The three tasks share source and
conversation history; they are not independent repository samples. This round
counts as neither of the two required independent confirmations and supplies
no external-user observation.

## Results

Every model query reaches the originally required successful terminal. All four
captured patches make the same one-line production repair, add their own tests,
and pass all six frozen private native cases. Source review accepts five of the
eight explanations. Each arm's accepted-task count below includes its repair.

| Arm | Accepted tasks | Serious-error tasks | Complete workflow | Normalized tokens | Voluntary MCP attempts / SDK-visible replies | SDK estimate |
| --- | --- | --- | --- | --- | --- | --- |
| Native | 2/3 | 1 | 88.808 s | 246,098 | 0 / 0 | $0.3748748 |
| Serena | 3/3 | 0 | 152.147 s | 290,840 | 0 / 0 | $0.4463088 |
| Codegraph | 3/3 | 0 | 79.171 s | 220,359 | 4 / 4 | $0.5084563 |
| CodeNib | 1/3 | 2 | 108.891 s | 367,985 | 0 / 0 | $0.5626022 |

These are descriptive observations. CodeNib's complete time is 28.4% lower
than Serena's, 22.6% higher than Native's and 37.5% higher than Codegraph's.
Its normalized usage is respectively 26.5%, 49.5% and 67.0% higher. CodeNib's
lower acceptance and absent product calls prevent a graph-benefit interpretation;
do not select the Serena time comparison as a promotion result.

Installation/initialization takes 5.213 seconds for CodeNib, 3.973 for Serena
and 6.861 for Codegraph. CodeNib retains only a fresh BM25 index at the end:
the model never requests its graph, so provider/graph preparation remains
unused. The earlier operator admission actually installed providers and built
the graph; those forced preflight calls are separate from these voluntary-use
counts. Avoiding unused work is functioning, but source-delivery improvements
were not exercised by this agent cohort.

The eight native Max sessions record 1,125,282 normalized main/auxiliary tokens
and a $1.8922421 SDK estimate, below the planned $12 round estimate. Count each
session's final cumulative counters once. The cumulative estimate including
preceding recorded probes/cohorts is $22.4119693. These amounts are not invoices
or cash savings; complete HTTP request counts and invoice charges remain
unknown. No API-key fallback is used, and the original $500 paid-API ceiling
is unchanged.

## Source assessment and native-test limits

One unblinded maintainer-agent review applies all 32 frozen explanation
criterion instances: 29 pass and three fail. There is no independent external
adjudication. The [assessment and raw evidence](codegraph_development_execution_v1_2026_10_30.json)
retain exact answers, source hashes, criterion decisions, patches and terminal
receipts.

| Rejected answer | Finding | Source and executable evidence |
| --- | --- | --- |
| CodeNib diagnosis | The central loop diagnosis is correct, but registration names a nonexistent `Blueprint.app_before_request`; an additional template-context claim gives the wrong namespace order and cites an error-handler implementation. | `Blueprint.before_app_request` is defined at `blueprints.py:546`; `app.py:1245` belongs to `_find_error_handler`. Template processors use reversed blueprint names at `app.py:741`. An attribute check and app/parent/child processor observation reproduce both contradictions. |
| CodeNib review | It states that an unhandled exception reaches teardown without the source's error-clearing branch. | `app.py:2218–2221` can set `error=None` before context pop. An unhandled `ValueError` with propagation off and `should_ignore_error=True` produces a 500 response, runs `after_request`, and sends `None` to teardown. |
| Native review | It says testing/debug mode re-raises without accounting for an explicit propagation override, and omits the untrapped HTTP-exception path when describing handler-less exceptions. | `app.py:1393–1396` uses testing/debug only when the configuration is `None`. With testing true and `PROPAGATE_EXCEPTIONS=False`, the callback error produces a finalized 500 without raising. An unhandled `abort(403)` returns 403 with teardown `None`. |

Those executable checks corroborate source judgments after model execution;
they do not add cases to the frozen six-test repair grader. The original
native denominator remains six per patch, with no collection error or
model/native test-path conflict. Diagnosis and review preserve source bytes
in all eight observations.

The models' wider local test runs remain red: Codegraph observes three failures
in its selected subset, while the other arms observe eight in the full suite.
A separate canonical-reference diagnostic also fails the same eight test IDs
(466 passed, ten skipped): three session-cookie assertions and five dependency
deprecation errors. Retain these original model outcomes; a six-case private
pass does not mean the whole suite passes.

Codegraph and CodeNib exceed the requested review length under whitespace
counting (656 and 623 words). The frozen source rubric has no numeric counting
rule, so these instruction deviations remain explicit without introducing a
new acceptance rule after inference. Minor citation drift and broad wording
around finalization remain interpretation limits alongside the exact findings.

## Prospective binding and accounting

The [protocol](codegraph_development_protocol_v1_2026_10_30.json) is sealed at
2026-10-30 09:00:56 UTC, before first model inference. It binds 990 files:
tasks, private rubric/grader/reference hashes, exact driver and native-tool
scope, candidate wheel, SDK/CLI code, source files and four operator admissions.
Private grading contents remain outside all model inputs and are published
only with these completed results. The earlier `draft` rubric filename is
fixed by that seal; it is not permission to alter criteria after execution.

The fixed arm order is Codegraph, CodeNib, Serena, Native. All use Claude Code
2.1.310, Agent SDK 0.2.174, `claude-opus-5-5`, medium effort, adaptive thinking
and the same upfront-tool policy. All retain the six required native tools;
optional servers report connected. Initialization inventories and four visible
Codegraph replies do not establish complete downstream HTTP schema visibility.
The SDK also records auxiliary `claude-haiku-5-5` usage; it is included in the
normalized totals, not mislabeled as additional main-assistant queries.

The model phase runs from 09:01 to 09:09 UTC. The complete clock starts before
product installation and includes session startup/disconnect, actual queries,
model-chosen test work and any first-use/update work. Common project preparation
and later evaluator grading are separate. No task is retried after an outcome
is inspected. Shell source output is not converted to a ContextBench read score.

The frozen source candidate is `45c4a363`, whose tree is identical to the
subsequently merged source-delivery fix
[#913](https://github.com/sysevol-ai/CodeNib/pull/913), `9a84ba56`. Its 20
executed PR checks pass with eight verified conditional skips. Main's release
verifier passes all 20 executed jobs, including installation and Wiki/MCP
services, with five tag-only skips; unit and integration pass. Conditional
serial/graph/slow skips do not establish full graph-chain acceptance. These
source fixes do not change the public 0.2.4 package.

Four evidence parts independently reconstruct all 143 archived execution,
native-grading and audit files. All 990 sealed inputs, 390 preceding JSON
artifacts and 34 original v2 inputs retain their hashes. All eight model,
four grading and two retrospective-audit containers are absent. The single
assessment-controller error omitted Markdown delimiters in a literal quote
assertion; correcting quoting changes no task, model run, native grade or
source judgment, and the failed script is retained.

## Next gate

Keep the full objective active. Improve the task fit and tool entry based on a
source-linked cross-file consumer, then observe useful voluntary calls before
attempting two independently frozen confirmation sets. Preserve native exact
search and every setup/failure/update cost. Do not rerun these exposed tasks
to seek a favorable result. Representative update coverage and five-user
consent/seven-day reuse remain open at 0/5; no outreach is authorized.

Tracking: [#874](https://github.com/sysevol-ai/CodeNib/issues/874).
