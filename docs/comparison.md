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
| [CodeGraph (Lordymine/codegraph)](https://github.com/Lordymine/codegraph) | Yes | No retrieval model | Typed call graph | Go, TypeScript/JavaScript | Re-index on MCP launch; unchanged input is a no-op |
| [DeepWiki public MCP](https://docs.devin.ai/work-with-devin/deepwiki-mcp) | Hosted service | No user key/authentication for public MCP | Wiki structure, contents and generated answers | Public indexed repositories; no per-language graph matrix in this contract | Service-managed; local file-level update contract not documented |

“CodeGraph” names several independent projects. This table identifies
`Lordymine/codegraph` explicitly; its claims do not describe the whole family.
Serena's LSP navigation and DeepWiki's Wiki structure are different interfaces
from CodeNib's persisted typed graph. Their public contracts do not establish
the same graph schema or coverage.

## Where CodeNib fits

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
