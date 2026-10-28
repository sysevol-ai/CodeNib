# CodeGraph matched pilot execution

All twelve frozen workflows completed their five model tasks. The original native
repair grader resolves 12/12 captured repairs. Source-rubric acceptance for the
48 explanation tasks remains open, so the complete benefit gate is unassessed.

The same native Claude agent is compared alone, with Serena, with
[`colbymchenry/codegraph`](https://github.com/colbymchenry/codegraph), and with
the unreleased CodeNib source candidate across three historically exposed Python
source snapshots.

The [execution summary](codegraph_matched_execution_v1.json) retains every
workflow, native outcome and pending source task. The
[controller archive](codegraph_matched_controller_v1.json) retains exact
post-processing code and failed setup output. The
[pre-inference archive](codegraph_execution_2026_10_27.json) binds the executable,
candidate wheel, CLI and runtime before the first query. The
[post-run audit](codegraph_matched_validation_v1.json) verifies original native
test denominators, adapter identities, limits and owned-container cleanup.

## Observed execution

Times include product installation/indexing, both SDK sessions and all five
tasks. Common machine/source/project-dependency preparation and later grading
are excluded under the [frozen policy](codegraph_matched_policy_v3.json). These
are observed, unpenalized times; rejected tasks still require the frozen timeout
penalty after source review.

| Source case | Native | Serena | Codegraph | CodeNib |
| --- | ---: | ---: | ---: | ---: |
| psf__requests-1921 | [149.6 s](codegraph_matched_workflow_v1_03.json) | [206.1 s](codegraph_matched_workflow_v1_00.json) | [176.0 s](codegraph_matched_workflow_v1_02.json) | [212.3 s](codegraph_matched_workflow_v1_01.json) |
| pallets__flask-5014 | [176.2 s](codegraph_matched_workflow_v1_06.json) | [190.3 s](codegraph_matched_workflow_v1_04.json) | [156.9 s](codegraph_matched_workflow_v1_05.json) | [229.0 s](codegraph_matched_workflow_v1_07.json) |
| pytest-dev__pytest-5809 | [132.1 s](codegraph_matched_workflow_v1_09.json) | [182.8 s](codegraph_matched_workflow_v1_11.json) | [185.6 s](codegraph_matched_workflow_v1_10.json) | [177.6 s](codegraph_matched_workflow_v1_08.json) |

## Tool availability and use

Optional MCP attempts count unique captured tool-use IDs. An unused tool stays
in its assigned arm. Native read/search/edit/shell tools remained available
throughout. Actual SDK initialization confirms connected optional tools on
Requests/Flask and for Codegraph on pytest.

| Source case | Serena attempts | Codegraph attempts | CodeNib attempts |
| --- | ---: | ---: | ---: |
| psf__requests-1921 | 0 | 3 | 0 |
| pallets__flask-5014 | 0 | 2 | 0 |
| pytest-dev__pytest-5809 | 0 (unavailable) | 4 | 0 (unavailable) |

CodeNib initializes graphs for Requests and Flask. Its pytest initialization
fails on the retained containment/definition-identity collision (#871), after
26.7 seconds of setup. Serena's pytest auto-configuration detects Pascal fixture
files and asks whether to enable that additional language server. The frozen
headless invocation receives EOF and stops after 3.5 seconds of setup. This is
an unattended configuration failure, without a symbolic query on pytest.

Both failed setups continue with native tools under the frozen policy. Their
failures and full elapsed costs remain in the cohort. The run does not
demonstrate CodeNib graph recovery/automatic refresh or Serena's pytest indexing
capability with a completed configuration.

## Original native repair outcomes

The unchanged selected SWE-bench Verified 4.1.0 tests, parser and grading run on
the exact captured repair patch and pinned arm64 control image. Native grading
begins after the model batch ends. Reference patches and grading feedback never
enter model inputs.

| Source case | Native | Serena | Codegraph | CodeNib |
| --- | --- | --- | --- | --- |
| psf__requests-1921 | pass | pass | pass | pass |
| pallets__flask-5014 | pass | pass | pass | pass |
| pytest-dev__pytest-5809 | pass | pass | pass | pass |

## Accounting and remaining gates

All 24 sessions report the actual `claude-opus-5-5` assistant with native Claude
Max authentication and no API-key source. No paid API fallback is enabled.

Final cumulative usage is counted once per session across main and auxiliary
models, including cache creation/read and output. The cohort records 8,959,314
tokens and $9.880642 in SDK estimates. These estimates are separate from
invoices; per-task cash charges and complete HTTP request counts remain unknown.

48/48 read-only tasks preserve the recorded source bytes. Applying each frozen
source rubric remains necessary before accepted-task counts, the strongest
eligible alternative, timeout penalties, cluster uncertainty or the 20%
promotion gate can be reported.

This cohort has one attempt per task and supports a bounded pilot decision after
acceptance review. Broader graph correctness (#871), manual rebuild/reconnect
costs and five external users with seven-day repeat-use observations remain open.
No external participant is enrolled.

The workflow fixes and this candidate are on source main; the public PyPI 0.2.4
package is unchanged. Adoption A0 remains active under
[#874](https://github.com/sysevol-ai/CodeNib/issues/874).
