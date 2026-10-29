# Independent workflow assessment and product decision, v2

Hold promotion: this pilot does not establish CodeNib's required 20% workflow
benefit. CodeNib accepts 11/15 tasks, below the strongest observed alternative's
13/15, and its graph tools are never selected despite successful setup on all
three sources. Every alternative also has a material explanation error, so
the frozen rule admits no complete eligible baseline and reports
**insufficient evidence**. There is no selected comparative winner.

The [prospective protocol](codegraph_matched_protocol_v2.md) and
[execution record](codegraph_matched_run_v2.md) cover fifteen prompts across
Requests, Flask and pytest, each run with native tools only, Serena,
`colbymchenry/codegraph`, and the current CodeNib source candidate. The original
v1 results and retrospective diagnostic correction retain their original
status. This independent cohort was frozen before its new inference.

## Accepted tasks and complete costs

| Arm | Accepted tasks | Serious-error tasks | Observed workflow total, s | Total with frozen rejection penalties, s | Normalized tokens | Optional MCP attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Native | 13/15 | 1 | 511.724 | 1,135.179 | 2,115,543 | 0 |
| Serena | 12/15 | 1 | 567.999 | 1,529.459 | 2,191,580 | 0 |
| Codegraph | 10/15 | 4 | 526.644 | 1,959.806 | 2,510,976 | 6 |
| CodeNib | 11/15 | 3 | 667.005 | 1,750.160 | 2,571,238 | 0 |

The twelve unchanged native repair grades all pass. Of 48 source explanations,
34 are accepted and fourteen rejected; none is protocol-unassessable. Every
read-only task preserves captured source bytes. Rejected tasks keep their
observed costs and add `max(0, frozen deadline - observed task time)`, exactly
as specified before inference. No failed answer is omitted or assigned zero
time.

The maximum alternative acceptance is thirteen. Native attains it but has a
serious error, making the eligible set empty. Selecting a weaker alternative
would violate the frozen rule. CodeNib also falls below that maximum accepted
count. Neither raw timings nor a penalty-adjusted ordering can establish the
promotion gate without the required quality and eligibility conditions.

Descriptively, CodeNib's unpenalized total is 30.3% above Native and its recorded
tokens are 21.5% higher. The three paired repository time ratios are 1.264,
1.401 and 1.241. Repository bootstrap uses the original seed, 10,000 resamples
and a 95% percentile interval of 1.241–1.401 for this raw ratio. These are
three clusters, not fifteen independent observations, and the comparison has
no established quality parity or population interpretation.

## What the source review found

One unblinded maintainer-agent review applies all frozen criteria to the new
captured answers and exact source versions. Each finding retains its verbatim
answer quote, criterion rationale, source hash and lines. Controller source
excerpts establish correctness, not model-read credit. No v1 judgment is copied
into this cohort and no criterion is revised after observing v2.

- All four pytest flow answers unconditionally say per-item teardown runs.
  `runtestprotocol` has no `finally` around setup/call; configured `Exit` or
  `KeyboardInterrupt` propagation can skip that phase. The pinned-function
  counterexample observes only setup and call before the interrupt escapes.
  This is the same material-error rule applied to new answers.
- Codegraph additionally omits HEAD's exclusion from a 302 method rewrite,
  predicts that digest-auth redirect responses cannot be followed by the
  surrounding session, and overstates Flask's debug/propagation condition.
- CodeNib's Flask answers overstate universal `ensure_sync` wrapping and say a
  `stream_with_context` response is iterated after cleanup. Exact source and a
  hash-checked execution show that the wrapped stream retains the context,
  with teardown delayed until its completion.
- Ordinary rejections include overstated test protection, missed existing
  regression coverage and a contradictory explanation of `excinfo=None`.
  Missing coverage and minor citation/order errors are distinguished from
  material false control-flow claims.

Per-source review records contain all accepted and rejected decisions:
[Requests](codegraph_source_assessment_v2_requests.json),
[Flask](codegraph_source_assessment_v2_flask.json), and
[pytest](codegraph_source_assessment_v2_pytest.json). The
[analysis record](codegraph_matched_assessment_v2.json) contains paired task
outcomes, frozen penalties, complete arm totals, descriptive subgroups and all
repository comparisons. Lookup, flow/impact, repair and post-edit subgroup
costs allocate one fifth of actual workflow overhead to each task; that
amortization is descriptive and supplies no separate promotion test.

## Product scope and next evidence

Keep CodeGraph scoped to source-current typed navigation and bounded indexed
relationships where its provider is ready, with visible explicit rebuild
costs. Hold comparative-efficiency marketing and default-rollout expansion.
These measurements include installation/indexing costs, but CodeNib and
Serena are unused after connecting; their answer outcomes do not measure the
intrinsic efficacy of graph retrieval. Codegraph's six calls do not establish
its general superiority either.

Prioritize an actual user's repeatable source-linked task and observed tool
choice before another efficacy cohort or unrelated language/backend expansion.
Do not force tool calls, repeat these exposed tasks to seek a positive result,
or change the seriousness/eligibility rules. Broader provider correctness,
representative update costs and automatic rebuilding remain open. The source
fixes are unreleased; public 0.2.4 is unchanged.

External repeat use remains **0/5**: there is no recruitment program, consent
or authorized outreach. Maintainer SDK sessions are not external participants.
The bounded hold decision is complete; the broader adoption objective and
[tracking issue #874](https://github.com/sysevol-ai/CodeNib/issues/874) remain
active. Current gates belong to [adoption A0](../adoption_roadmap.md).
