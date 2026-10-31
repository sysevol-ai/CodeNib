<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
SPDX-License-Identifier: Apache-2.0
-->

# SDK tool-loading audit

A fresh SDK connection loads CodeNib's exploration tool and accounts for its
server instructions. This model-free check does not support a missing or
deferred tool diagnosis in this connection. Why the agents in the completed
coding studies did not use CodeNib remains unknown.

The [lossless evidence packet](codegraph_sdk_context_audit_2026_10_30.json)
contains thirty UTF-8 records, including both attempts, the transport guard,
original metadata responses, installed-file identities and source checks.
Decode each record's `gzip_base64`, verify `gzip_sha256`, decompress, then
verify its byte length and `sha256`.

## What the client reports

Agent SDK 0.2.174 and CLI 2.1.310 connect to an installed CI wheel from #933.
The product connection uses the closed diagnostic's native-tool list, allowed
MCP namespace, empty settings/skills/plugins, model configuration, system-prompt
preset and appended scope. The wheel's 485 Python modules and one native
extension match exactly from outside the analyzed checkout. The fresh copied
Pylint source has only BM25 prepared; no graph or provider is prepared.

`get_mcp_status()` reports server `study` connected with `explore_context`.
`get_context_usage()` reports `mcp__study__explore_context` with `isLoaded=true`
and a separate server-instruction entry. Neither session loads a memory file.

| CLI-reported context estimate | CodeNib session | Native-only session |
| --- | ---: | ---: |
| System prompt | 1,960 | 1,960 |
| Native tools | 5,726 | 5,726 |
| MCP tool | 782 | 0 |
| MCP server instructions | 260 | 0 |
| Total used | 8,728 | 7,686 |

These are offline CLI estimates. A rejecting proxy denies all 54 observed
CONNECT attempts to `api.anthropic.com:443` before TLS; remote token counting
does not succeed through that proxy. The official
[SDK reference](https://code.claude.com/docs/en/agent-sdk/python#contextusageresponse)
explains that context accounting makes token-count requests outside the message
stream, and token counting is unbilled on the Anthropic API. The observed
CONNECT count is not a complete HTTP or model-request count. The proxy is not
a host network sandbox. No token savings, invoice amount or task speedup is
measured here.

The transport permits only `initialize`, `mcp_status` and `get_context_usage`.
Each session sends exactly those three control requests and receives three
control responses. There is no user submission, assistant/result event or
tool execution; both context replies contain zero message tokens and null
`apiUsage`. No model question is sent. The inspection omits execution hooks
and the source-result proxy, and adds HTTP rejection and telemetry suppression;
these differences are explicit rather than attributed to the earlier study.

The 1,598 copied source files remain byte-identical. All 24,225 inputs in the
closed diagnostic's seal are reverified unchanged. Both owned containers are
independently absent. The first attempt fails before SDK startup because its
command redundantly supplies `sleep` to the image's `/bin/sleep` entrypoint.
Its cleanup removes the container, then incorrectly expects a case-sensitive
missing-object message. Both errors are fixed in the second attempt; original
failure receipts remain in the packet.

## What changes next

Historical HTTP schema exposure and model-choice causation remain unknown.
This audit does not justify another tool-loading rewrite or another paid
exact-name lookup. A new efficacy protocol needs a real source-linked
maintenance task, a verifiable edit/test outcome and useful voluntary tool use.
Retain the prior results, complete setup/update/failure costs, quality gates,
two independent success confirmations and external-repeat-use requirement.

Current Serena documentation describes navigation, symbolic editing and
backend-dependent refactoring. Cross-file rename and symbol editing cover
operations beyond CodeNib's read-only source/navigation interface; several
other refactorings require its JetBrains backend. Its
[evaluation methodology](https://oraios.github.io/serena/04-evaluation/010_methodology.html)
uses agent-selected examples and self-assessment. Its
[evaluation prompt](https://oraios.github.io/serena/04-evaluation/020_prompts/010_evaluation-prompt.html)
assumes correct use and excludes misuse failures. This is workload guidance,
not an independently graded comparison with CodeNib. Keep failures in our
prospective full-workflow protocol. No competitor runtime, completed score or
unopened confirmation set is changed; the review alone does not authorize
adding a refactoring surface.

The product decision remains hold promotion. Independent successes remain
zero and external enrollment remains 0/5. Internal maintenance use must be
labelled internal and cannot satisfy external consent or repeat use.
