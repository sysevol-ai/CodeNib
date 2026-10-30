# Maintainer source-review adoption probe

This prospective diagnostic asks whether the clearer exploration guidance leads
to useful voluntary calls during two real CodeNib source reviews. It has one
internal product condition and no comparison arm. It cannot establish a
whole-workflow gain, independent success confirmation or external repeat use.
The prior [source-first outcome](codegraph_source_first_results.md) stays closed.

## Frozen task and runtime

The [pre-inference packet](codegraph_guidance_adoption_freeze_2026_10_30.json)
seals 7,357 inputs at `2026-10-30T21:44:41.451042+00:00`, before any model
task. Binding SHA-256 is
`3d9e3bac6862f20e483f9919adec476ced598e3bb1ee43130af949ed3c677f54`.
This includes all 4,936 unchanged previous inputs, the exact new controller,
prompts, private rubric, source snapshot, wheel and copied runtime helpers.
The reserved task set is unopened.

The source snapshot contains 1,143 selected files from current main product
commit `5f9573d97f141e1588af30ea95b06ae988f104a1`: the Python package, core,
tests and packaging files. Its archive and independent single-commit Git
snapshot are bound separately. Documentation and prior experiment outcomes
are outside model access, as are the rubric and execution receipts.

| Task | Maintainer question | Strict final-word limit |
| --- | --- | --- |
| Dependency policy | Trace query-only, symbol-seeded and explicit dependency behavior in deferred/prebuilt modes, including warm reuse, source evidence, preparation costs and supported limits. | Fewer than 450 |
| Changed source | Trace stale-source rejection, visible recovery, explicit rebuilding and same-process reload; identify regression coverage and limits to automatic recovery. | Fewer than 550 |

Neither prompt names the optional tool or requests a call. Read, Grep, Glob,
Bash and the existing native tool set remain available. No result from this
probe is an external user's adoption observation.

The exact [admitted main wheel](codegraph_installed_guidance.md) runs in a
credential-free, network-disabled container with two CPUs and 4 GiB memory.
Dependencies come from a pinned prepared image; its retained Python parser
cache is copied explicitly. Setup and failures remain timed, with no fresh
dependency-installation comparison. Isolated-mode Python starts the installed
MCP server from outside the analyzed checkout, preventing self-analysis from
shadowing the installed package.

The main model is `claude-opus-5-5` with adaptive thinking and medium effort;
the requested output cap is 16,384 tokens including thinking and text. The
pinned CLI is 2.1.310 and Agent SDK is 0.2.174. The provider's current
[model specification](https://platform.claude.com/docs/en/models/opus-5-5/overview)
and [migration requirements](https://platform.claude.com/docs/en/models/opus-5-5/migration-guide)
were checked before freezing: tool choice remains automatic, thinking is
adaptive, and unsupported sampling settings are omitted. The prior exact
CLI/SDK outgoing-request control verifies the same configuration; actual model,
initialization, effort, rate and final usage must be checked again.

## Acceptance, costs and stopping

Each task uses a separate native subscription client, with 24 turns, a
900-second task deadline, 120-second connection deadline, 30-second disconnect
deadline and 20-second interrupt/drain grace. The complete workflow cap is
2,400 seconds and setup cap is 900 seconds. The existing authenticated source
receiver and bounded deadline implementation are copied unchanged.

Use the maintainer's included Max route with API/provider overrides removed.
The SDK still draws from subscription limits when signed in, as documented in
the [current plan guidance](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan).
There is no account/provider/paid fallback. The new diagnostic has a $4
cumulative SDK-estimate guard; the existing $500 API authorization is unchanged.
Estimates do not establish invoice charges. Count final cumulative input,
cache-write, cache-read and output tokens once per client across main and all
auxiliary models; thinking is already included in output. Unknown latest usage,
authentication/quota failure or an exhausted guard stops further queries.

Retain every attempted call, no-call result, failure, cap, source mutation and
setup cost. Require successful terminals, unchanged selected source and
source-grounded answers satisfying the frozen semantic rubric and strict word
limits. A single unblinded maintainer reviews both answers only after the whole
model batch terminates. A completed optional call is useful only if its original
source is bound to file/version/line, delivered to the model and supports a
correct final claim. Otherwise retain it as an attempt or unattributed source;
never infer read credit from a requested range or assistant summary.

## Pre-inference controls and open gates

The exact controller prepares the selected source, verifies all 485 installed
Python files and its native extension, advertises the single exploration tool,
delivers all 772 instruction characters and answers a real model-free source
query with dependencies disabled. Source bytes remain unchanged.

The initial preparation's missing HOME exposes a secondary cleanup error.
Both are corrected before freezing; the original attempt and explicit recovery
are retained. A deterministic exception immediately after container creation
verifies cleanup, and the corrected ordinary preparation passes. All three
owned control containers are independently absent. This is controller
admission, with zero model tasks; its failures are separate from product and
prospective model outcomes.

Keep the full correctness/20% complete-time/normalized-usage gates,
representative update coverage and both independent confirmations unchanged.
External enrollment remains 0/5, with no outreach authorized. Current outcomes
and open gates belong to [adoption A0](../adoption_roadmap.md) and
[#874](https://github.com/sysevol-ai/CodeNib/issues/874).
