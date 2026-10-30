# Maintainer adoption outcome

Both internal source-review sessions voluntarily call CodeNib once and receive
source supporting a specific correct final claim. Neither complete answer
passes the [prospectively frozen acceptance rules](codegraph_guidance_adoption_freeze.md).
This establishes bounded use in this diagnostic and keeps promotion on hold;
it does not establish comparative benefit or a causal effect of the guidance.

## Task acceptance

Both tasks terminate successfully through the pinned SDK. The original final
text is counted with `str.split()` against its exclusive word limit. The source
rubric is applied only after the whole batch terminates, by one unblinded
maintainer-agent with no independent adjudicator.

| Source review | Voluntary CodeNib calls | Final words | Required words | Source-review finding | Fully accepted |
| --- | --- | --- | --- | --- | --- |
| Dependency preparation | 1 | 509 | Fewer than 450 | Core policy is accurate, including symbols plus explicit false; the required static-reference/invocation boundary is omitted. | No |
| Stale-source recovery | 1 | 567 | Fewer than 550 | Correctly traces rejection/rebuild/reload, but falsely extends the indexed-query wrapper to every MCP tool. | No |

The second answer states that every MCP tool passes through
`_indexed_tool_call`. `get_manifest` instead dispatches to `_manifest_response`
and reports source verification through its status path. That distinction
matters when describing which operations reject stale source. Exact submitted
answers, original source versions/excerpts, the frozen rubric and separate
word-limit/semantic findings are retained in the
[outcome packet](codegraph_guidance_adoption_results_2026_10_30.json).
No task, criterion, model terminal or old cohort score changes.

## Actual tool delivery and its limits

The two sessions also use thirteen native Read calls and seven Grep calls.
Both optional requests are ordinary query-only requests; no call is forced.
The first delivered observation supports the correct claim that the budget
limits returned context rather than requesting preparation. The second
supports the correct description of recording `source_error` and raising the
indexed-query error. These are bounded supporting observations: native tools
also supply evidence, and neither final answer is fully accepted.

The audit checks both captured original MCP replies and the corresponding
tool results in the SDK stream. The CLI minifies the outer JSON, so raw outer
strings differ; every decoded field and source body matches. Each complete
visible source line is mapped to its file and independently captured original
version. Omission markers and projection caps retain their meaning; a reported
line range does not imply that every line was delivered. No search is rerun,
native source credit is left unattributed, and no ContextBench score is claimed.
The provider's native HTTP requests are not intercepted.

The product reports `commit_verified=false` and `checkout_state=not-attested`.
Those flags are preserved. The separate sealed source archive and unchanged
before/after file hashes bind the original versions for this audit; they do
not change the product's Git-attestation claim.

## Observed resource use

| Measure | Two-task diagnostic |
| --- | --- |
| Complete observed workflow | 78.950 seconds |
| Included setup | 3.017 seconds; dependencies/image and retained parser cache prepared |
| Normalized cumulative main/auxiliary tokens | 394,316 |
| SDK estimate | $0.7113487 |
| Complete accepted tasks | 0/2 |

These are two clients in one internal condition. The actual CLI is 2.1.310;
both main assistants are Opus 5.5 at observed medium effort, with Haiku 5.5
also present in both final usage ledgers. Main and auxiliary counters are
counted once per final client. Initialization has `apiKeySource=none`, included
rate checks pass, and no API/provider/account fallback occurs. SDK estimates
are not invoices; invoice charges and complete HTTP request counts remain
unknown. There is no comparison-arm time or cost result.

## Integrity and next gate

The packet reconstructs 29 original files from 22 distinct payloads. An
evidence-only line-pointer correction preserves its initial artifact and
changes no judgment. All 7,357 sealed inputs, including the 4,936 prior inputs,
remain unchanged. The three control containers and actual model container
are independently absent. No completed model cohort is rerun, and the
reserved task set remains unopened.

The next engineering hypothesis is to reduce unrelated returned context while
retaining the decision boundary and complete source needed for the question.
Both observed calls return bounded, partially projected excerpts, followed by
native inspection. That motivates a separate control; it does not show that
retrieval caused either answer failure or that a smaller response will improve
the workflow. Admit any new model test under a new prospective protocol.

Keep the full correctness/20% complete-time/normalized-usage gates,
representative source updates and both independent success confirmations open.
External enrollment remains 0/5, with no outreach authorized. The old matched
outcomes and hold decision stay unchanged in [adoption A0](../adoption_roadmap.md)
and [#874](https://github.com/sysevol-ai/CodeNib/issues/874). Public package
0.2.4 is unchanged.
