<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib Naming and Compatibility

CodeNib is the product name. The CodeMiner compatibility namespace remains in
place for existing installations, clients, caches, and published artifacts.
The display-name migration does not require users to rebuild indexes or change
imports, configuration, or artifact URLs.

## Preferred Commands

New installations may use the product-facing commands:

```bash
codenib-mcp /path/to/repo/.codeminer_cache/repo_manifest.json
codenib-web
```

`codeminer-mcp` and `codeminer-web` remain supported aliases backed by the same
Python functions. The other existing `codeminer-*` evaluation and artifact
commands are unchanged.

Python continues to use the `codeminer` distribution and import package.
Agent callers may use either spelling below; both names refer to the same class
object:

```python
from codeminer.agent import CodeMinerAgentOptions, CodeNibAgentOptions
```

## Stable Compatibility Inputs

The following identifiers remain canonical:

- `CODEMINER_*` environment variables;
- `~/.codeminer`, `.codeminer_cache`, and `.codeminer_qa` state roots;
- the `codeminer` package and every existing `codeminer-*` command;
- MCP server ID `codeminer`, prompt ID `codeminer-guide`, tool names, and
  `codeminer_context` skill ID;
- manifest, graph, vector, and incremental-cache formats, including the
  `repo_manifest.json`, `graph.pkl`, `incremental_state.json`,
  `chunk_store.pkl`, `embeddings_cache.pkl`, and `qa_registry.json` entry
  filenames;
- repository URL `https://github.com/sysevol-ai/CodeMiner`;
- CodeMiner Base, CodeMiner Synthesis, their Hub IDs, and artifact paths;
- public classes such as `CodeMinerAgentOptions`, `CodeMinerBaseDataset`, and
  `CodeMinerSynthesisDataset`.

CodeNib branding alone does not bump a schema or rewrite an artifact. A future
change to any stable identifier requires an explicit migration decision,
dual-read or alias behavior where applicable, and compatibility tests.
