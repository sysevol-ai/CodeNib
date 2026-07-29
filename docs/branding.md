<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib Names And Paths

CodeNib is both the product name and the maintained Python and command-line
namespace. Current installations use the `codenib` package, `codenib` command,
`CODENIB_*` environment variables, and CodeNib-owned state paths.

## Preferred Commands

New installations should use the unified product command:

```bash
codenib wiki /path/to/repo
codenib mcp /path/to/repo
```

Python uses the `codenib` distribution and import package:

```python
from codenib.agent import CodeNibAgentOptions
```

## Stable Inputs

The following identifiers are stable public inputs:

- `CODENIB_*` environment variables;
- `~/.codenib` and `.codenib_qa` state roots;
- the `codenib` package and every existing `codenib-*` command;
- MCP server ID `codenib`, prompt ID `codenib-guide`, tool names, and
  `codenib_context` skill ID;
- manifest, graph, vector, and incremental-cache formats, including the
  `repo_manifest.json`, `graph.pkl`, `incremental_state.json`,
  `chunk_store.pkl`, `embeddings_cache.pkl`, and `qa_registry.json` entry
  filenames;
- canonical repository URL `https://github.com/sysevol-ai/CodeNib`;
- CodeNib Base, CodeNib Synthesis, their Hub IDs, and artifact paths;
- public classes such as `CodeNibAgentOptions`, `CodeNibBaseDataset`, and
  `CodeNibSynthesisDataset`.

The Hub values above are external dataset addresses, not aliases for executable
CodeNib interfaces. Persisted schemas change only when their serialized
structure or declared identity changes.

## Filesystem Roots

CodeNib resolves machine-dependent locations through four environment
variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CODENIB_HOME` | `~/.codenib` | User-owned datasets, checkouts, and state |
| `CODENIB_PREBUILT_DIR` | `$CODENIB_HOME/prebuilt` | Reusable repository artifacts |
| `CODENIB_RESULTS_DIR` | `$CODENIB_HOME/results` | Benchmark and experiment outputs |
| `CODENIB_TEMP_DIR` | `$TMPDIR/codenib` | Disposable indexer and tool work |

User-facing commands keep repository indexes under
`$CODENIB_HOME/repositories/<repo>-<id>/indexes` (default
`~/.codenib/repositories/...`) instead of storing CodeNib artifacts in the
target checkout. Language-aware graph builders may still invoke project
toolchains that prepare dependencies in the checkout. The lower-level compiler
accepts an explicit `cache_dir`; compatibility code can also read older
repository-local `.codenib_cache` manifests.
