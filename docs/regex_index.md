<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Regex Node Index

An in-memory index for fast regex-based searching across CodeGraph nodes (similar to grep).

## Features

- **In-Memory Storage**: Fast access using Python list structure
- **Regex Matching**: Powerful regular expression search capabilities
- **Glob Filtering**: File path filtering with glob pattern support

## Overview

The `RegexNodeIndex` provides grep-like functionality for searching code content within a CodeGraph. It stores all nodes in memory with their content and supports:

1. **Regex search** - Pattern matching using Python's `re` module
2. **Plain string search** - Fast substring matching
3. **File filtering** - Glob-based file path filtering
4. **Type filtering** - Search within specific node types (function, class, file, etc.)
5. **Case sensitivity control** - Optional case-insensitive matching

## Quick Start

### Basic Usage

```python
from codeminer import CodeGraph, RegexNodeIndex

# Load existing CodeGraph
code_graph = CodeGraph.load_graph("~/.codeminer/xxx/graph.pkl")

# Build index from CodeGraph
idx = RegexNodeIndex(code_graph=code_graph)

# Search examples

# 1. Find function definitions in Python files
results = idx.search(r'def\s+\w+\(', file_glob='*.py')
for node in results:
    print(f"{node.file}:{node.start_line} - {node.node_name}")

# 2. Simple string search (case-insensitive)
results = idx.search('calculator', use_regex=False)

# 3. Search within specific node types
results = idx.search('class', node_type='file')

# 4. Combined filtering
results = idx.search(
    pattern=r'TODO|FIXME',
    file_glob='src/**/*.py',
    case_sensitive=False
)
```

### search() Method

```python
def search(
    pattern: str,
    file_glob: Optional[str] = None,
    node_type: Optional[str] = None,
    case_sensitive: bool = False,
    use_regex: bool = True
) -> List[NodeInfo]
```

**Parameters:**

- **`pattern`** (str, required): Search pattern (regex or plain string)
  - If `use_regex=True`: Treated as regular expression
  - If `use_regex=False`: Treated as literal string

- **`file_glob`** (str, optional): Glob pattern to filter by file path
  - Examples: `*.py`, `src/*.js`, `**/test_*.py`, `*calculator*`
  - Uses `fnmatch` for glob matching

- **`node_type`** (str, optional): Filter by node type
  - Available types: `file`, `function`, `method`, `class`, `field`, `directory`, `symbol`
  - Exact match only

- **`case_sensitive`** (bool, default=False): Whether search is case-sensitive
  - `False`: Case-insensitive matching (default)
  - `True`: Exact case matching

- **`use_regex`** (bool, default=True): Whether to use regex matching
  - `True`: Pattern treated as regular expression
  - `False`: Pattern treated as plain string (faster)

**Returns:**
- `List[NodeInfo]`: List of matching nodes
