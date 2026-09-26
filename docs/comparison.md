<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Choosing repository tools for an agent

Use text search for exact strings, symbol tools for definitions and edits,
and a typed graph for relationships across symbols. A hosted Wiki is useful
when you want to browse a public repository without installing anything.
These tools can complement each other; this is a capability comparison, not
a shared performance benchmark.

Checked against the linked upstream documentation on 2026-09-26. “Local”
describes the repository tool; your coding agent may still call a remote
model. “No key” excludes the agent's own subscription or API credentials.

| Tool / route | Local repository processing | Model or key for the tool | Structural context | Languages | Update behavior |
| --- | --- | --- | --- | --- | --- |
| grep / file reads | Yes | No | Text matches and source files | Any searchable text | Reads current files; no graph index |
| [CodeNib CodeGraph](codegraph.md) | Yes | No | Typed SCIP/LSP symbol graph | [14 chunkers; 12 graph-capable language entries](language_capabilities.md) | Reuse unchanged views; rebuild changed views |
| [CodeNib grep → Jev experiment](evaluation/grep_jev.md) | Local search; remote model calls | OpenRouter | Ranked source blocks; a graph is not required | Evaluated on five language groups | Searches the selected source snapshot |
| [Serena](https://github.com/oraios/serena) | Local LSP or IDE backend | No separate retrieval model | Symbol, definition, reference and editing tools | [Upstream language/backend matrix](https://oraios.github.io/serena/01-about/020_programming-languages.html) | Backend maintains project state; indexing/caches are backend-dependent |
| [CodeGraph (colbymchenry/codegraph)](https://github.com/colbymchenry/codegraph) | Yes; telemetry opt-out | No model or API key for indexing/search | Typed AST-derived symbol and relationship graph | [20+ languages and templates; upstream matrix](https://github.com/colbymchenry/codegraph/blob/ba3c21e50d9129d2f5f3843ec3728868ae6d47a1/README.md#supported-languages) | File watcher, incremental changed-file sync and reconnect catch-up |
| [DeepWiki public MCP](https://docs.devin.ai/work-with-devin/deepwiki-mcp) | Hosted service | No user key/authentication for public MCP | Wiki structure, contents and generated answers | Public indexed repositories; no per-language graph matrix in this contract | Service-managed; local file-level update contract not documented |

“CodeGraph” here means `colbymchenry/codegraph`, a separate project from
CodeNib's CodeGraph route. Its row was checked at revision
[`ba3c21e`](https://github.com/colbymchenry/codegraph/tree/ba3c21e50d9129d2f5f3843ec3728868ae6d47a1),
using its [README](https://github.com/colbymchenry/codegraph/blob/ba3c21e50d9129d2f5f3843ec3728868ae6d47a1/README.md),
[typed node/edge definitions](https://github.com/colbymchenry/codegraph/blob/ba3c21e50d9129d2f5f3843ec3728868ae6d47a1/src/types.ts)
and [indexing guide](https://colbymchenry.github.io/codegraph/guides/indexing/).
Language counts use each project's grouping; consult the linked matrices for
coverage and limitations.

CodeGraph processes source locally. Its
[telemetry policy](https://github.com/colbymchenry/codegraph/blob/ba3c21e50d9129d2f5f3843ec3728868ae6d47a1/TELEMETRY.md)
documents usage statistics and a background release check; `DO_NOT_TRACK=1`
disables both. Local processing does not by itself mean zero network requests.

Serena's LSP navigation and DeepWiki's Wiki structure are different interfaces
from CodeNib's persisted typed graph. Their public contracts do not establish
the same graph schema or coverage.

## Where CodeNib fits

Both CodeNib and the compared CodeGraph offer local, model-free graph tools
across multiple languages. CodeNib's choices include SCIP/LSP-derived
relationships, portable context artifacts, static Wiki export and
inspectable method reproductions. CodeGraph documents automatic incremental
file updates; CodeNib's current update boundary is described below.

Choose the shipping CodeGraph route when you want local MCP tools returning
bounded source context plus graph navigation across supported languages.
SCIP/LSP providers may require system toolchains and project dependencies;
model-free does not mean installation-free. Swift and Lua currently support
chunking/retrieval, without a graph backend. Check the
[generated capability matrix](language_capabilities.md) for exact boundaries.

CodeNib currently reuses an unchanged view or rebuilds an affected view.
File- and symbol-level delta repair is not enabled on the product path.
The language registry's incremental-backend entries must not be read as a
claim that every CLI index update performs delta repair.

The grep/Jev result motivates a lighter initial retrieval path, but remains
an experiment until exposed and verified in the product. Its
[71.4% Recall@5](evaluation/grep_jev.md) is not a head-to-head measurement
against Serena, CodeGraph, or DeepWiki.
CodeGraph's own agent token/cost reports use different tasks and measurements;
they cannot be ranked against CodeNib's retrieval Recall@5 result.
