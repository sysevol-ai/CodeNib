<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Agent recovery after a source edit

This maintainer probe checks whether an agent can recover from a stale CodeNib
query by preparing current indexes and retrying within the same MCP connection.
It records preparation and recovery costs on three previously exposed Python
repositories. It does not measure comparative task benefit, voluntary tool use,
external adoption, or cold installation.

The [frozen binding](codegraph_agent_recovery_protocol_v1.json) retains exact
source commits, prompts, executable bytes and the acceptance rules below.
Keep this protocol unchanged after inference; publish outcomes separately.

## Prospective observation contract

Use Requests, Flask and pytest at the same pinned commits as the completed
matched study. Test the source candidate after #890 with warm SCIP 0.6.6 and
basedpyright 1.39.8, serial decoding, BM25 and strict graph construction. Keep
the published 0.2.4 package and every frozen matched-study result unchanged.

Prepare a fresh private state directory for each repository. Measure the
complete `codegraph init` invocation and a second unchanged invocation. The
shell and real product MCP run in one owned, network-disabled arm64 container,
limited to two CPUs, 4 GiB and 256 processes. Mount only its own checkout. Mask
any inherited image workspace volume with an explicitly empty temporary
filesystem, and verify that boundary before setup. Client registration uses a
stand-in inside the container; it does not exercise installation into the
user's native profile. Provider and package installation are already warm.

The immutable product image binds original source commit
`0d846180c8247832a4fe88f61f6c857af904837b`; its changed MCP files equal merged
main `dc1e3ff9c764c44f2c91a7ccf0654f5c935e6511`. Verify their bytes at runtime.
The original and controlled-edit source captures must match the #890 controls.

Before inference, exercise the exact initialization and first dependency query
on every selected source. Make the controlled edit, confirm stale rejection,
rebuild explicitly and verify the new relationship in the original MCP process.
Retain failed preparation/preflight attempts separately from product failures.

Then freeze the executable, prompts, source commits, image, hooks and acceptance
contract before any model query. Run one native Claude Max SDK session per
repository, with two queries in the same session. Request `claude-opus-5-5`,
medium effort, adaptive thinking and a 16,384-token output cap. Retain actual
model, authentication, discovered tools, permission events, model usage and
terminal outcomes. Read, Grep, Glob, Edit, Write, Bash and ToolSearch remain
available; auto-approve this MCP server. Explicitly enable tool search. Keep
ambient settings, skills and plugins excluded, and do not enable API fallback.

Each query has a client-side $1 estimate cap, at most 20 turns and a
240-second limit. The three sessions contain two queries each, giving a nominal
$6 estimate allowance with headroom for SDK cap overshoot, within the
already authorized $500 ceiling; this is not an invoice reconciliation. Stop
on an unconfirmed subscription route or included-only rate event. Count the
final cumulative usage once per session, retaining cache and auxiliary-model
fields. Do not add cumulative per-query estimates together. Actual invoice
charges and HTTP request counts remain unknown.

The first prompt explicitly asks for the immediate outgoing dependencies and
source-linked final return of a named function. The controller then wraps that
return with a newly added identity helper, `_codenib_g1_passthrough`, in the
same file. The second prompt says the checkout changed and asks for the current
CodeNib dependency result and cited return expression. It does not prescribe
the rebuild command. Do not manually rescue a model attempt or change its
acceptance after seeing the result.

| Repository | Changed function | Controlled change |
| --- | --- | --- |
| Requests | `Session.prepare_request` | Wrap the prepared-request return value |
| Flask | `Flask.make_response` | Wrap the completed response return value |
| pytest | `CallInfo.from_call` | Wrap the constructed call-information object |

These identity wrappers preserve the selected return values. They exercise a
real tracked-file change and an added graph relationship, without asking the
model to repair production behavior. They are bounded controls, not a sample
of arbitrary user edits.

## Acceptance fixed before inference

Record each observation separately. A model session completes the bounded
recovery gate only if all of the following hold:

1. Its initial CodeNib dependency call succeeds on the original source.
2. After the edit it observes a stale CodeNib rejection, invokes the product
   CLI to prepare current indexes without human intervention, and retries the
   CodeNib dependency tool successfully.
3. The delivered current graph contains the exact changed-function-to-helper
   relationship; the answer cites the current return expression and helper.
4. The MCP process ID remains the same across both queries and the final
   product status authenticates the edited source.
5. The model preserves all repository bytes and Git state apart from the
   controller's recorded edit, and ends within the declared resource bounds.

A completed terminal response must have `subtype: success`, `is_error: false`,
`terminal_reason: completed`, `stop_reason: end_turn` and no API error status.
No capped, truncated, interrupted, refused or budget-limited response is accepted
as completion. Check these rules prospectively even if partial tool delivery
looks useful. Retain elapsed time and source state on exceptions as well.

Stop the remaining cohort on an authentication/rate confirmation failure,
wrong SDK/model/tool inventory, or a resource-integrity mismatch. Retain
unattempted repositories in the three-source denominator. No manual rescue
or replacement session is allowed. Run Requests, Flask and pytest sequentially.

Native fallback, an unresolved tool error, a missed helper relationship, a
source modification, a reconnect, a timeout or a model/resource mismatch stays
in the denominator. Preserve raw prompts, tool attempts, actual delivered
replies, source-version captures and every terminal result. Retain complete
recovery wall time, including failed queries, planning, rebuilds and retries;
also report the observed CLI duration separately.

Passing demonstrates prompted agent-operated recovery on these sources. The
server itself still requires explicit rebuilds and does not start indexing
from a read. It cannot establish voluntary use, a 20% whole-task improvement,
complete graph accuracy, automatic background indexing, or the five-user
repeat-use gate. [A0](../adoption_roadmap.md) and
[#874](https://github.com/sysevol-ai/CodeNib/issues/874) remain active.

## Configuration references checked before inference

The [model configuration](https://code.claude.com/docs/en/model-config),
[Opus capabilities](https://platform.claude.com/docs/en/models/overview),
[Python SDK](https://code.claude.com/docs/en/agent-sdk/python),
[MCP permissions](https://code.claude.com/docs/en/agent-sdk/mcp),
[tool search](https://code.claude.com/docs/en/agent-sdk/tool-search) and
[output environment variable](https://code.claude.com/docs/en/env-vars)
were checked on 2026-10-28. Requested flags alone do not prove effective
behavior; retain initialization, hook, stream and final usage receipts.
