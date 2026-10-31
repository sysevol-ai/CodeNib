<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Internal source-update maintenance

CodeNib constructs a complete selected Python graph of its own checkout and
keeps one MCP connection usable through a documentation/source edit and explicit
rebuilding. This internal functional control exposes a substantial first-graph
and post-edit preparation cost. It does not measure comparative coding benefit.

The [evidence packet](codegraph_internal_maintenance_2026_10_30.json) retains
35 text records in one bounded part. Verify the part's hash, decode each
record's `gzip_base64`, verify `gzip_sha256`, decompress and check its original
byte count and `sha256`. Original SCIP and graph binaries remain local with
recorded hashes. No completed paid study is rerun or rescored.

## Maintenance outcome

The actual task corrects a stale statement in `codenib/mcp/README.md`: query
calls can prepare a graph when relationships are explicitly requested in
on-demand mode. Reloading a published generation does not itself compile,
and changed source still requires explicit rebuilding. The adjacent loader
docstring makes that distinction; the exploration reference also explains
incoming and outgoing direction values. Runtime behavior and parameter enums
do not change.

A source-only query supplies the implementation evidence. Subsequent graph
queries return bounded indexed references around the preparation/reload
functions. Both graph builds select all 1,172 Python documents under the
existing source policy, with 33,178 nodes and 159,676 edges, partial indexing
disabled and no added exclusions. Construction and these source observations
do not prove exhaustive reference accuracy or runtime coverage.

After applying the reviewed README/docstring patch to the disposable analyzed
checkout, a query rejects the stale index. Explicit BM25 rebuilding restores
source-only queries in the same MCP connection and reports graph navigation
unavailable. A later relationship request prepares a new complete graph against
the changed source. The MCP process remains PID 913170 throughout; source
fingerprints change, and only the two intended files remain modified.
All 2,387 checked-out files are checked against original Git blob identities
or the reviewed patch, with no extra files left behind.

All 26 returned contexts match their captured source versions: 15 contain live
source and 11 resolve to an earlier exact delivery. Successful session-call
numbers differ from controller-attempt numbers because errors do not commit
delivery history; the packet retains that mapping. Both wrong direction
arguments (`callers`, then `in`) and the expected stale rejection remain in the
eight-attempt ledger. The correct direction is the advertised `impact`.

## Observed costs

| Operation | Seconds |
| --- | ---: |
| Setup, including independent source checkout and wheel verification | 6.632 |
| Initial BM25 command, included in setup | 3.443 |
| Initial source-only query | 0.787 |
| First relationship query, including graph preparation | 210.506 |
| Later relationship query on the prepared graph | 0.614 |
| Explicit BM25 rebuild after editing | 3.360 |
| Source-only query after rebuilding | 1.491 |
| Relationship query after editing, including graph preparation | 219.915 |

Dependencies and SCIP/basedpyright providers are already installed. The product
container has two CPUs, 4 GiB, 256 PIDs and no network. Its installed #933 CI
wheel matches all 485 Python modules and one native extension from outside the
analyzed checkout. The source is an independent checkout of `943c02c1`; the
product runtime is never overlaid with checkout code. Whole-controller time
includes operator reading/waiting and artifact export and is not a task-speed
endpoint. These are different queries and descriptive component observations,
not a controlled comparison or an advantage claim.

SCIP emits an unsupported-Python-3.13 warning before completing all selected
documents. The controller forwards stderr to its terminal rather than retaining
a complete byte stream; original parsed MCP replies and compiler artifacts are
retained. A patch-equality diagnostic initially fails because full and shallow
Git repositories abbreviate object IDs differently. Full-index patches and
exact changed-file bytes agree; that diagnostic is not a source mutation.

The owned container is independently absent. Forty-three targeted reload,
deferred-preparation and server checks pass, as do the strict docs build,
public boundary, support matrix and pinned hooks.

## Decision

Hold promotion and the unchanged whole-workflow quality/time/usage gates.
Internal operator-selected calls are not agent adoption, independent success
or external repeat use. External enrollment remains 0/5 and successful
independent confirmations remain 0/2.

First-relationship preparation and graph rebuilding after edits are specific
cost targets. Python product navigation is currently graph-backed; the existing
native provider path is clangd-only and lacks an authenticated selected-file
proof. Do not bypass source-policy guards or promote an evaluation-only live
Python provider to make a component faster. A real coding task, useful voluntary
use and a prospectively frozen matched protocol remain necessary before another
efficacy claim. Earlier no-tool cohort latency is not attributed to this run.
