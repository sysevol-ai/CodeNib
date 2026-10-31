# Preserve LSP replies and distinguish cold readiness

The shared language-server client could lose replies and progress messages
that arrived together, or discard a fragmented message after a read timeout.
The transport repair preserves those bytes and late replies. It does not by
itself repair the previously observed cold cross-file reference omission.

The [terminal packet](codegraph_lsp_stdio_2026_10_31.json) retains 165 exact
text files in seven bounded parts. Large original full-source wire output and
the interrupted test-tier logs remain local with their byte lengths and
SHA-256 identities; no query is replayed to replace an original observation.

## Product repair and verification

`LSPClient` now keeps unread frames and partial bytes across calls, processes
already buffered messages before waiting on the pipe, and retains replies
encountered during notification or pending-request drains. The existing
100-response limit remains. A new server clears the preceding transport state.
This shared client already serves Python compiler-identity normalization,
incremental graph patching and LSP indexing; no new provider or product route
is added. Source guards, graph/builder versions and storage boundaries remain.

Real POSIX-pipe regressions cover coalesced replies, split headers and Unicode
bodies across a timeout, buffered progress, late reference replies, pending
drains and a fresh server. All seven fail against the unchanged base and pass
after the repair. The related unit tier passes 213 cases; the full local unit
tier passes 9,016, with 24 skips, 221 heavier cases deselected and the same 46
existing warnings. Pinned format/lint hooks pass.

Retain the setup outcomes. The first regression fixture omitted `PIPE` from
its fake subprocess module; after fixing only that fixture, all seven product
failures reproduce before the product edit. An unfiltered targeted command
enters serial integration and is interrupted; its exact owned process ends,
with no remaining child in the task namespace. The first full unit run under
host umask `002` fails an existing `0644` file-mode assertion, also reproduced
on unchanged base. With process-local umask `022`, the full tier passes without
changing that assertion or the storage implementation.

## Original server replies and explicit file preparation

Four prospectively fixed, sequential sessions use basedpyright 1.39.8 in the
same prepared offline Docker image, with two CPUs, 4 GiB and 256 PIDs. An
observing wrapper passes original pipe bytes through unchanged and captures
their chunks; it can affect scheduling. Installed identity checks admit all
485 Python files and the native extension outside a source checkout. The
candidate developer assembly changes only `lsp_client.py` from the previously
admitted wheel and regenerates its RECORD; it is not a source-built, CI-produced
or published candidate.

| Condition | First reference positions, expected 3 | Separate observation |
| --- | --- | --- |
| Unchanged client, cold fixture | 1 | After bounded idle: 3 |
| Repaired client, cold fixture | 1 | After bounded idle: 3 |
| Repaired client, every selected Python file prepared | 3 | No idle or positioned warmup query |

Both first cold **server replies themselves** contain only the declaration.
Offline framing of the captured original stream verifies that these replies
equal what the client delivered. The observed `Found 2 source files` message
arrives later in each case. This distinguishes the omission from a client
delivery failure; neither first result is replaced by the later successful
query.

The pinned provider implementation schedules file enumeration separately from
workspace settings initialization, and references visit the current program's
file list. Its [enumeration scheduling](https://github.com/DetachHead/basedpyright/blob/v1.39.8/packages/pyright-internal/src/analyzer/service.ts#L679)
and [reference iteration](https://github.com/DetachHead/basedpyright/blob/v1.39.8/packages/pyright-internal/src/languageService/referencesProvider.ts#L267)
support an enumeration-readiness explanation. That is an inference from source
and observed ordering, not an internal server trace or an idle-completeness
guarantee. No diagnostic-mode configuration is changed.

The prepared condition opens **every** selected Python file and awaits each
document-symbol reply before its first positioned query. It injects no selected
caller or expected result. The generated three-file fixture includes both
Python files. The full-source condition retains all 2,390 selected files,
including all 1,172 Python files, tests and configuration, from pinned CodeNib
source `8639802df6a6d3ba6f118c7a1a1aba535404bab4`. All 1,172 actual did-open
texts exactly match their captured source bytes, and preparation completes
without an error or skipped selected file.

All six full-source definition/reference sets for `_prepare_graph_for_call`,
`extract_symbol` and `extract_scip_blocks` match the saved same-source compiler
positions, with reference counts 2/26/29 and one definition each. There is no
missing, extra or invalid location. This checks three fixed symbols, not the
complete graph, multi-hop reference semantics or arbitrary-repository coverage.

From just before source capture to the first guarded full-source reference
result takes **7.142s** in one observation. The interval includes source copy,
container creation/start, offline wheel reinstall, installed import/identity
checks, LSP initialize, all selected symbol operations and the guarded query.
The selected-file preparation itself takes 4.086s. The existing checkout and
prepared image/software predate that interval. This is not a timing comparator
with full SCIP preparation, Serena or a complete coding task.

A separate installed post-SCIP replay exercises the real identity normalizer
and subsequent graph construction on unchanged saved raw compiler output.
It preserves the exact normalized-text digest, query-surface digest, all
786,078 ordered occurrences, the quality report and artifact receipts, with
33,178 nodes and 159,676 edges. This verifies an existing product consumer;
it reruns no compiler generation, MCP coding workflow or model experiment.

## Preserved source and open product gates

Retained original and snapshot authorities bracket each published positional
observation; only the verified snapshot is project-mounted read-only, with the
original source absent in the server container. Both sources reverify after
each session. All eleven observed position replies equal the corresponding
captured original server results; all four exact owned containers are
independently absent. All 10,282 provider-manifest entries reverify. These are
source-only observations, not model-input source credit.
The separate installed-replay container is also independently absent, for five
owned containers across this iteration.

Ship the verified shared-client repair after review. Keep live positioned-query
routing experimental until portable source isolation and representative
project/configuration/dependency closure are established. Selected links and
arbitrary repository/language/platform coverage remain unsupported by this
experiment; the source-selection guard is not relaxed. The existing
[source-update and Unicode controls](codegraph_native_source_queries.md) and
all frozen coding outcomes stay intact.

No new model question or external participant is added. Whole-task quality,
at least 20% complete-time improvement, normalized-token limits and voluntary
use remain open; independent successes stay 0/2 and external enrollment/reuse
0/5. The reserved confirmation material is not opened by this diagnostic.
