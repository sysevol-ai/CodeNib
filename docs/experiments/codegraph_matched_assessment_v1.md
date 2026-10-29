# CodeGraph matched pilot assessment

Hold promotion: this three-repository pilot does not establish a 20% net-benefit
gain. The frozen rule admits no complete eligible alternative because each
alternative contains material source-explanation errors. One original criterion
also has an unresolved source-wording defect affecting four observations.

The study compares one native Claude agent alone, with Serena, with
`colbymchenry/codegraph`, and with the unreleased CodeNib source candidate. It
contains fifteen task prompts on three historically exposed Python snapshots,
with one attempt per task in each arm. The
[complete assessment](codegraph_matched_assessment_v1.json) preserves all sixty
observations. The [original execution record](codegraph_matched_run_v1.md)
retains setup failures, tool availability, source versions and native outcomes.

## Acceptance under the original policy

All twelve captured repairs pass the selected original native grading. All 48
read-only explanations preserve source bytes. One unblinded maintainer-agent
review checks every frozen source criterion against the captured answer and
its exact source version. The review finds 29 accepted explanations, 15 rejected
explanations and four unassessable observations. It has no independent external
adjudication.

| Arm | Known accepted / 15 | Known rejected | Protocol-unassessable | Tasks with material errors |
| --- | ---: | ---: | ---: | ---: |
| Native | 12 | 2 | 1 | 2 |
| Serena | 10 | 4 | 1 | 2 |
| Codegraph | 9 | 5 | 1 | 4 |
| CodeNib | 10 | 4 | 1 | 3 |

The accepted counts include three repair tasks per arm. Each unassessable task
remains in its original denominator. The corresponding acceptance bounds are
12–13 for Native, 10–11 for Serena, 9–10 for Codegraph and 10–11 for CodeNib.
These are interpretation bounds, not confidence intervals.

The original pytest impact item confuses a direct factory caller with the
upstream collection entry. The [separate protocol correction](codegraph_source_protocol_v2.md)
preserves that ambiguity and provides a retrospective diagnostic only. With that
single corrected item, diagnostic accepted counts are Native 13, Serena 11,
Codegraph 10 and CodeNib 11. No original score is silently replaced.

## What failed source review

The evidence is available by repository:
[Requests](codegraph_source_assessment_v1_requests.json),
[Flask](codegraph_source_assessment_v1_flask.json) and
[pytest](codegraph_source_assessment_v1_pytest.json). Each observation records
its original criteria, decision, exact answer quotation for a finding, source
file/version/range and captured tool-use references.

Material findings include an unconditional 302/303 method-rewrite claim that
omits the HEAD exception, incorrect Flask exception-propagation conditions, a
false requirement for a completed WSGI response before teardown, and an
unconditional pytest teardown guarantee despite configured re-raising.

The [review method and audit archive](codegraph_source_review_method_v1.json)
retains exact-source checks. Real pinned Flask execution confirms that response
conversion errors propagate even with `from_error_handler=True`, explicit
`PROPAGATE_EXCEPTIONS=False` suppresses re-raising with debug enabled, and
teardown runs when an exception prevents the response WSGI call. Pinned pytest
control-flow functions with explicit boundary doubles confirm interruption can
bypass the per-item teardown phase and an xfail report suppresses the interactive
hook. The signal-mutation probe is marked unavailable because Blinker is absent;
it is not used as a failure gate.

These findings concern assistant explanations. They do not establish that a
CodeNib graph response supplied a wrong fact: CodeNib has no observed MCP
attempt in this run. Minor citation/name offsets and qualified normal-path
summaries are recorded separately. Four explicit overstatements of what selected
tests protect fail the required test-description criterion without receiving a
material-runtime-error label. Later answers that correct an earlier phase do not
rewrite that earlier captured answer.

## Timing and usage

The primary endpoint includes product setup/indexing, both SDK sessions and all
five tasks. Rejected tasks retain elapsed time plus the frozen remaining-deadline
penalty. The original item ambiguity prevents a unique complete primary total;
the assessment records interpretation bounds without turning missing acceptance
into zero.

| Arm | Observed total | Original penalized bounds | Recorded normalized tokens |
| --- | ---: | ---: | ---: |
| Native | 457.9 s | 1,095.3–1,411.5 s | 2,227,905 |
| Serena | 579.2 s | 1,834.4–2,150.1 s | 2,194,470 |
| Codegraph | 518.4 s | 1,938.7–2,244.2 s | 2,337,058 |
| CodeNib | 618.9 s | 1,880.0–2,190.8 s | 2,199,881 |

Raw CodeNib time is 1.352 times Native, 1.069 times Serena and 1.194 times
Codegraph. These are descriptive observations with no quality-parity assumption
or selected eligible winner. The repository-cluster bootstrap uses the frozen
seed and 10,000 paired resamples of the three repositories. Its exploratory
95% percentile intervals for those raw ratios are 1.300–1.419, 0.972–1.204 and
0.957–1.460 respectively. Three repository clusters cannot establish population
non-inferiority or general tool efficacy.

Even the fully permissive assumption that every source answer is accepted leaves
the observed CodeNib total above Native on each repository. That sensitivity
observation does not alter the frozen source decisions or repair grading.

The [prespecified descriptive groups](codegraph_matched_subgroups_v1.json) split
non-phase workflow overhead equally across the five tasks. Their observed
allocated costs are:

| Group | Native | Serena | Codegraph | CodeNib |
| --- | ---: | ---: | ---: | ---: |
| Lookup | 59.0 s | 82.6 s | 67.7 s | 77.1 s |
| Flow and impact | 262.8 s | 302.1 s | 295.5 s | 320.1 s |
| Repair | 72.2 s | 98.3 s | 77.4 s | 125.8 s |
| Post-edit | 63.9 s | 96.2 s | 77.9 s | 96.0 s |

This is the frozen five-task amortization assumption, without a causal claim
about a graph tool that the agent did not use.

Final cumulative main/auxiliary model usage is counted once per session,
including cache creation/read and output. Original SDK estimates total $9.880642;
invoice charges and complete HTTP request counts remain unknown. Source review
adds no SDK inference. Its controller time is excluded under the original policy.

## Product action and remaining work

The frozen outcome is insufficient evidence, with promotion held. Keep CodeGraph
positioned as source-current typed navigation and bounded call/impact evidence
where its provider is ready. Do not add a net-efficiency claim or promote a new
onboarding default from this cohort. Defer expansion into additional languages
or backends unless it removes a recorded current blocker.

Before another benefit trial, fix the #871 identity-recovery blocker and reduce
the rebuild/reconnect friction. Verify tool discovery and actual use: Requests
and Flask connect CodeNib and Serena successfully but the model chooses neither;
Codegraph receives nine MCP attempts. On pytest, CodeNib graph construction fails
and Serena's headless configuration stops on its Pascal-server prompt. Both
native fallbacks and all costs remain retained; the latter is not evidence of
Serena's symbolic indexing capability with completed configuration.

Five external users with seven-day observations remain required and unenrolled.
The overall adoption A0 goal and [#874](https://github.com/sysevol-ai/CodeNib/issues/874)
remain active. The public PyPI 0.2.4 package is unchanged, and this assessment
makes no complete ContextBench source-read score or unseen-task claim.
