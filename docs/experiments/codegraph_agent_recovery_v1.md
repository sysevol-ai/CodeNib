<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Prompted agents recover after an indexed source edit

All three maintainer-operated sessions recovered from a stale CodeNib query:
the agent received the recovery instruction, rebuilt through the product CLI,
and queried the changed relationship through the original MCP process. This
verifies the bounded recovery path after #890. It does not establish voluntary
use or a comparative whole-task benefit; [A0](../adoption_roadmap.md) and
[#874](https://github.com/sysevol-ai/CodeNib/issues/874) remain active.

The [execution record](codegraph_agent_recovery_2026_10_28.json) retains all
three sessions, six prompts and nine delivered MCP outcomes: six successful
dependency results and three expected stale-query rejections. All selected
source bytes and Git state match the controller's edit after each model query.
All three containers are absent, and no host process remains in their owned
workspaces.

## Frozen scope and outcome

The [protocol](codegraph_agent_recovery_protocol_v1.md) and
[executable binding](codegraph_agent_recovery_protocol_v1.json) were committed
as `ee2b1b7a363e976165fe9418a56686be00b0770c` before the first query. They pin
the three previously exposed source commits, exact prompts, immutable product
image, hooks, model/client settings, source captures and acceptance rules.
No attempt was replaced or manually rescued. No acceptance rule changed.

The first prompt explicitly requests the connected CodeNib dependency tool.
Between prompts the controller changes one tracked return expression and adds
an identity helper in the same file. The second prompt says the checkout has
changed and asks for current CodeNib dependencies and cited return behavior;
it supplies no rebuild command. Ordinary native read, search, edit and shell
tools remain available. Provider and package installation are already warm,
and client-registration CLIs are isolated stand-ins.

Each session met the frozen recovery criteria:

1. Its original dependency query succeeded on the pinned source.
2. The actual model input then contained a stale-index error and the rebuild
   instruction; the agent ran `codegraph init` and retried successfully.
3. The delivered new graph contained the exact changed-function-to-helper
   reference. Current source observations and final citations covered the
   changed return and helper.
4. One MCP process remained alive across both prompts. Final product status
   authenticated the changed source and current indexes.
5. Source/Git state stayed equal to the controller's edit. Both model turns
   ended with the required completed terminal state and within resource bounds.

The three acceptance counts cover those recovery criteria. They do not grade
the complete answer or establish complete graph accuracy. In particular, the
Flask answer calls a table of static references "direct calls" even though it
contains fields and the `ResponseReturnValue` annotation. The pytest answer
explicitly observes missing edges for external and dynamic calls. Keep these
interpretation limits visible before a future benefit cohort; recovering an
index does not make every returned reference a runtime call.

## Complete request and rebuild-command costs

| Repository | Fresh-state initialization | Unchanged initialization | Original query | Query after edit | Rebuild-containing shell command |
| --- | ---: | ---: | ---: | ---: | ---: |
| Requests | 10.380 s | 0.813 s | 18.918 s | 31.910 s | 10.051 s |
| Flask | 9.827 s | 0.889 s | 22.106 s | 36.606 s | 9.545 s |
| pytest | 23.744 s | 1.073 s | 20.498 s | 43.647 s | 23.830 s |

The query-after-edit column includes the failed query, agent inspection and
planning, the rebuild, retries, source reading and final answer. Flask also
requests CLI help and a dry run; both remain in its complete request time.
The last column is the SDK-reported native Bash duration for the command that
contains the rebuild, including its pipes or Git-status check. It is not an
isolated compiler measurement. Raw pre/post intervals and exact commands are
retained alongside those reported durations.

These are sequential developer observations on three controlled Python edits,
with warm SCIP 0.6.6, basedpyright 1.39.8, serial graph decoding and BM25. They
are not cold installation measurements, matched native/peer comparisons or a
representative estimate of arbitrary update costs. The initial query and
changed-source query have different context and work, so their time difference
is not a causal overhead estimate.

## Observed model usage and boundaries

All three sessions use native Claude Max authentication, `apiKeySource: none`
and the observed `claude-opus-5-5` assistant. Tool hooks confirm medium effort;
adaptive thinking and automatic tool choice follow the frozen configuration.
The same nineteen tools are discovered in each session: seven native tools
and twelve CodeNib tools. Included-only rate events are retained, without a
paid/API fallback.

Final cumulative main and auxiliary-model counters are counted once per
session, including input, cache creation, cache reads and output. The total is
464,526 normalized tokens and **$0.6462027 in SDK estimates**. The reported
auxiliary model is `claude-haiku-5-5`; thinking counters are already part of
output and are not added again. Actual invoice charges and complete HTTP
request counts remain unknown. The estimate is within the nominal $6 probe
allowance; the user-authorized API ceiling remains $500.

The product image binds source commit `0d846180`; runtime checks confirm its
changed MCP files equal merged main `dc1e3ff9`. The source fixes remain
unreleased, and the public PyPI 0.2.4 package is unchanged. The original 22
matched-study files remain byte-for-byte unchanged, including their failed
setups, native fallbacks, timing, acceptance and insufficient-evidence decision.

## Remaining product decision

Keep promotion held. Prompting an agent to use CodeNib does not demonstrate
unprompted use, repeat use or a 20% complete-task improvement. Rebuilding is
still an explicit agent CLI operation; reads do not start background indexing.
Broader provider correctness and clear interpretation of static relationships
remain necessary before the next prospective matched cohort.

Next, resolve the recorded unattended peer setup and source-rubric issues,
freeze a corrected full benefit protocol, and retain all setup, update and
failure costs. External recruitment is unavailable and no participant is
enrolled; the five-user, seven-day repeat-use gate remains open. This recovery
result does not complete the full objective.
