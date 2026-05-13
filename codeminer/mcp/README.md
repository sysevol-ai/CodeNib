<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeMiner MCP Server

Model Context Protocol (MCP) server for CodeMiner's semantic search capabilities.

## Installation

```bash
pip install -e ".[dev]"
pip install 'mcp[server]'
```

## Usage

### 1. Build Index First

Before running the MCP server, you need to build indexes for your repository:

```bash
# Index your repository with vector embeddings
python scripts/index_repo.py --repo /path/to/repo --embedding-model text-embedding-3-small
```

This creates `.codeminer_cache/repo_manifest.json` in your repository.

### 2. Start MCP Server

```bash
codeminer-mcp --manifest /path/to/repo/.codeminer_cache/repo_manifest.json
```

Or using Python module:

```bash
python -m codeminer.mcp.server --manifest /path/to/repo/.codeminer_cache/repo_manifest.json
```

### 3. Check Server Status

The server exposes a status resource at `info://status` that shows:
- Repository path and commit
- Supported languages
- Loaded indexes (vector, BM25, graph)
- Index statistics (document counts, model info)

## Available Tools

### `semantic_search`

Search codebase using vector embeddings.

**Parameters:**
- `query` (str): Natural language or code search query
- `top_k` (int): Maximum results to return (default: 10)
- `level` (str): Hierarchy level - "l0" (files), "l1" (top symbols), "l2" (functions/methods). Default: "l2"
- `score_threshold` (float): Minimum similarity score (0.0-1.0)

**Returns:**
- List of code nodes with:
  - `node_id`: Unique identifier
  - `file_path`: Source file path
  - `node_type`: Symbol type (function, class, method, etc.)
  - `content`: Source code
  - `score`: Similarity score (higher = more relevant)
  - `start_line`, `end_line`: Line numbers (1-based)

**Example:**
```python
results = await semantic_search(
    query="function that validates email addresses",
    top_k=5,
    level="l2",
    score_threshold=0.7
)
```

## Architecture

- **ServerContext**: Loads and manages indexes from manifest
- **Phase 1 (Indexing)**: `IndexCompiler` builds indexes and writes manifest
- **Phase 2 (Query)**: MCP server loads manifest and serves search tools
- **Model validation**: Ensures embedding model consistency between indexing and serving

## Development

Run tests:
```bash
# Unit tests (no MCP dependency required)
pytest test/mcp/test_mcp_server.py -v

# Integration tests (requires built indexes)
pytest test/mcp/test_search_semantic_integration.py -v -m integration
```
