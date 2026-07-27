<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib Naming

CodeNib is both the product name and the maintained programmatic namespace on
`main`. The frozen artifact branch preserves earlier experiments; current
installations use the CodeNib package, commands, environment variables, and
state paths directly.

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

The following identifiers remain canonical:

- `CODENIB_*` environment variables;
- `~/.codenib` and `.codenib_qa` state roots, plus the legacy
  `.codenib_cache` layout;
- the `codenib` package and every existing `codenib-*` command;
- MCP server ID `codenib`, prompt ID `codenib-guide`, tool names, and
  `codenib_context` skill ID;
- manifest, graph, vector, and incremental-cache formats, including the
  `repo_manifest.json`, `graph.pkl`, `incremental_state.json`,
  `chunk_store.pkl`, `embeddings_cache.pkl`, and `qa_registry.json` entry
  filenames;
- canonical repository URL `https://github.com/sysevol-ai/CodeNib` (GitHub
  redirects the repository's former URL for historical references);
- CodeNib Base, CodeNib Synthesis, their Hub IDs, and artifact paths;
- public classes such as `CodeNibAgentOptions`, `CodeNibBaseDataset`, and
  `CodeNibSynthesisDataset`.

The Hub values above are immutable external addresses, not aliases for
executable CodeNib interfaces. Schemas change only when their serialized
structure or declared identity changes. The full mapping from former
identifiers to their CodeNib replacements lives in the
[namespace migration record](codenib_namespace_migration.md).

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
accepts an explicit `cache_dir`, and CLI manifest discovery can read the legacy
`<repo>/.codenib_cache` layout.
