# Native SDK route for the CodeGraph pilot

The pilot will prefer the user's existing Codex and Claude Code account
logins, with the compared tool connected over MCP. The user confirmed a
**$500 API budget ceiling** on 2026-10-27. Subscription allowance use and API
charges are separate accounting categories.

This record advances [A0](../adoption_roadmap.md) and
[#874](https://github.com/sysevol-ai/CodeNib/issues/874). The full task protocol
and external repeat-use trial remain open; this is not a benefit result.

## Verified connectivity

The [retained receipts](codegraph_sdk_2026_10_27.json) contain the executed
harnesses, versions, source hashes, indexed fixture and SDK-observed calls.
Both probes requested one dependency query on synthetic source and received
the correct callee, `sdk_probe_calculate`.

| Client | SDK / runtime used | Observed authentication | Actual result |
| --- | --- | --- | --- |
| Codex | `openai-codex 0.163.0`, explicitly using local Codex CLI `0.165.0` | SDK account reports `chatgpt`; API-key environment variables removed in the child | One completed CodeNib MCP call and correct final answer |
| Claude | `claude-agent-sdk 0.2.174`, explicitly using local Claude Code `2.1.310` | Existing claude.ai Max login; SDK init reports `apiKeySource: none` | Connected MCP server, one tool-use block and correct final answer |

The harness neither copies nor logs credential values. It does not change
`HOME`, `CODEX_HOME`, native client registrations or the user's configuration.
SDK dependencies live in an isolated temporary environment, not product
dependencies. The actual CodeNib server uses the source candidate identified
in the receipts; package metadata `0.2.4` is not a published-release control.

These are forced-tool connectivity probes. Claude's probe disables native
tools; Codex exposes only the selected CodeNib operation. Their model defaults,
tool inventories and context sizes differ. Do not compare their timings,
tokens or reported dollars as a tool or client performance result, and do not
count either probe as an external user's adoption.

Claude init identifies `claude-opus-5-5`. Codex uses the configured default,
`gpt-6.1-sol`, but the probe did not capture a separate effective-model receipt.
Matched runs must explicitly pin and verify model and reasoning settings.

## Matched-run contract

Use one SDK/model cohort for all four arms: native tools, Serena,
`colbymchenry/codegraph`, and CodeNib. A second client is a separate cohort,
not a way to compare CodeNib on one model with another tool on a different
model. Keep the same source, task, acceptance criteria and resource bounds
inside each cohort.

Native read, search, edit and shell tools remain available to every arm. Do
not carry the probe's forced MCP invocation or disabled native tools into the
pilot. Isolate tool configurations and checkouts, prevent ambient plugins
from leaking into control arms, and record the effective discovered tools.
Retain setup, indexing, updates, failures, retries and explicit unavailable
results. The [pilot contract](codegraph_net_benefit.md) still governs task
admission and product decisions.

Select **complete task wall time** as the primary endpoint before any matched
task runs. A subscription probe does not establish comparable per-task cash
charges. The secondary endpoint remains recorded model usage, with the
existing no-more-than-10% regression gate. Freeze the per-client token
normalization in the run manifest; retain input, cache-read, cache-write,
output and reasoning fields without double counting overlapping counters.
SDK turns and tool calls do not establish HTTP request counts or unobserved
auxiliary-model usage.

## Budget and remaining gates

The $500 ceiling is an authorization limit, not a spend target. Prefer the
existing account route, record allowance/rate-limit outcomes and account for
any billed fallback within that ceiling. A cohort must keep its declared
authentication and model configuration; quota exhaustion is an outcome, not
permission to silently change providers or discard attempts.

Keep SDK-reported cost estimates separate from actual invoices. Claude's
probe reports `$0.048072`; no API invoice was reconciled for either probe.
The receipts therefore leave `api_invoice_cost_usd` unknown. Subscription
access is neither unlimited capacity nor evidence of zero resource cost.

Still open: representative correctness and #871 scope, a 10-20 task cohort
across at least three repositories, exact peer versions/modes, explicit model
and reasoning settings, per-task limits and effective usage accounting.
There is currently no external recruitment program and no participant is
enrolled. The five-user, seven-day observation gate remains required; SDK
connectivity and maintainer-operated tasks do not replace it.

## Official references checked

- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) and
  [authentication](https://learn.chatgpt.com/docs/auth).
- [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview),
  [local plugins](https://code.claude.com/docs/en/agent-sdk/plugins), and
  [Claude Code authentication](https://code.claude.com/docs/en/authentication).

The verified route is a local maintainer experiment using existing sign-in.
It does not establish a customer-facing subscription-authentication offering.
