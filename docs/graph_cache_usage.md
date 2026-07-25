<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Graph Cache Mechanism

The SCIP indexer now includes a comprehensive caching system with configurable skip levels to optimize repeated indexing operations.

## Overview

The pipeline has three main stages:
1. **Index Generation** (`index.scip`) - SCIP indexing of the repository
2. **Index Decoding** (`index.decoded`) - Protobuf decoding to readable format
3. **Graph Construction** (`graph.pkl`) - Building and serializing the CodeGraph using pickle

## Cache Levels

Use the `skip_level` parameter to control which stages are cached:

### `skip_level=None` (Default)
Run full pipeline from scratch. Regenerates everything.

```python
from codenib.ls_router import LSIndexer

indexer = LSIndexer(project_root="/path/to/repo")
graph = indexer.run_pipeline(skip_level=None)  # Full pipeline
```

### `skip_level='raw'`
Check if `index.scip` exists:
- **If found**: Skip generation, proceed to decode + process
- **If not found**: Run full pipeline

```python
graph = indexer.run_pipeline(skip_level='raw')
```

**Use case**: Code hasn't changed, but you want to regenerate the graph with different processing logic.

### `skip_level='decode'`
Check if `index.decoded` exists:
- **If found**: Skip generation + decode, proceed to process
- **If not found**: Run from generation stage

```python
graph = indexer.run_pipeline(skip_level='decode')
```

**Use case**: Both code and decode format unchanged, but graph construction logic updated.

### `skip_level='graph'` (Fastest)
Check if `graph.pkl` exists:
- **If found**: Load graph from disk using pickle and return immediately
- **If not found**: Run full pipeline

```python
graph = indexer.run_pipeline(skip_level='graph')
```

**Use case**: Reusing exact same graph for multiple operations (e.g., batch processing SWE-bench instances).

**Performance**: Pickle is ~10-100x faster than JSON for large graphs.

## Output Directory Structure

By default, cache files are stored in `$TMPDIR/codenib/<project_name>/`
(usually `/tmp/codenib/<project_name>/`). The root comes from
`temp_state_dir()` in `codenib.paths` and can be relocated by setting the
`CODENIB_TEMP_DIR` environment variable (see [branding](branding.md)):

```
/tmp/codenib/my_project/
├── index.scip        # SCIP binary index
├── index.decoded     # Decoded protobuf text
└── graph.pkl         # Serialized CodeGraph (pickle format)
```

Custom output directory:

```python
indexer = LSIndexer(
    project_root="/path/to/repo",
    output_dir="/path/to/cache"  # Custom cache location
)
```

## Instance-Based Caching

For SWE-bench or similar use cases with instance IDs:

```python
# Use instance_id as output directory name
instance_id = "django__django-12345"
output_dir = f"/cache/{instance_id}"

indexer = LSIndexer(
    project_root=repo_path,
    output_dir=output_dir
)

# First run: generates all files
graph = indexer.run_pipeline(skip_level='graph')

# Subsequent runs: loads from cache instantly
graph = indexer.run_pipeline(skip_level='graph')  # Fast!
```

## CLI Usage

> **Note:** ls_router currently has no CLI entry point (no `__main__` / argparse). Use the Python API instead:

```python
from codenib.ls_router import LSIndexer

# Full pipeline
graph = LSIndexer(project_root="/path/to/repo").run_pipeline(skip_level=None)

# Use graph cache
graph = LSIndexer(project_root="/path/to/repo").run_pipeline(skip_level='graph')

# Custom output directory
indexer = LSIndexer(
    project_root="/path/to/repo",
    output_dir="/cache/instance-123",
)
graph = indexer.run_pipeline(skip_level='graph')
```

## Performance Comparison

Typical timings for a medium-sized project:

| Skip Level | Time     | What Runs                           |
|------------|----------|-------------------------------------|
| None       | ~60s     | Full: generate + decode + process   |
| `raw`      | ~30s     | Decode + process only               |
| `decode`   | ~10s     | Process only                        |
| `graph`    | ~0.5s    | Load from disk                      |

## Example: Batch Processing with Cache

```python
from codenib.ls_router import LSIndexer

# Process multiple instances efficiently
instances = [
    {"id": "inst-1", "repo": "/repos/repo1"},
    {"id": "inst-2", "repo": "/repos/repo2"},
]

for instance in instances:
    cache_dir = f"/cache/{instance['id']}"

    indexer = LSIndexer(
        project_root=instance['repo'],
        output_dir=cache_dir
    )

    # First run: creates cache
    # Subsequent runs: loads from cache
    graph = indexer.run_pipeline(skip_level='graph')

    # Use graph...
    print(f"Instance {instance['id']}: {len(graph.graph.vs)} nodes")
```

## Graph Save/Load API

You can also manually save and load graphs using pickle:

```python
from codenib.graph.code_graph import CodeGraph

# Save (uses pickle for fast serialization)
graph.save_graph("/path/to/graph.pkl")

# Load (uses pickle for fast deserialization)
loaded_graph = CodeGraph.load_graph("/path/to/graph.pkl")
```

**Note**: Pickle is binary format and much faster than JSON for large graphs.

## Notes

- Graph files include metadata (project_root) for proper reconstruction
- Cache files are independent - you can delete any stage to force regeneration from that point
- `skip_level=None` runs the full pipeline from scratch
- Empty/corrupted cache files are detected and pipeline falls back to generation
