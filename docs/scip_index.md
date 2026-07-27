<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

## Use SCIP to get the index

[SCIP](https://github.com/sourcegraph/scip/tree/main) is a code intelligence protocol for index, which has powerful support for multiple languages, e.g. python, C++, etc.
We copy the scip.proto to the local directory for convenience.

### Multilingual Prerequisites

Besides `scip-python`, multilingual indexing requires extra tooling:

- TypeScript/JavaScript (`scip-typescript`):
  - Node.js 18 or 20
  - `npm`
  - `make scip-typescript-tool node-workspace-tools` for local `scip-typescript`,
    `yarn`, and `pnpm` wrappers
- C/C++ (`clangd`):
  - `make active-system-deps-ubuntu clangd-tool` on Ubuntu
  - `cmake` for CMake-based projects
  - `bear` for Make/Autotools projects (e.g., repositories without CMake compile DB)
  - Requires `compile_commands.json` (auto-generated from CMake or `bear -- make` if missing)
- Rust (`rust-analyzer`):
  - `make rust-tool`
- Go (`scip-go`):
  - `make scip-go-tool`
  - Project must contain `go.mod`

Example installs:
```bash
make bootstrap-ubuntu
make active-scip-env
```

### Setup scip-python (Custom Fork)

We use a custom fork of scip-python with exclude-config support, located in `third_party/scip-python`.

#### Installation Steps

1. **Initialize the submodule** (if not already done):
   ```bash
   git submodule update --init --recursive
   ```

2. **Install dependencies and build**:
   ```bash
   cd third_party/scip-python
   npm install
   cd packages/pyright-scip
   npm install
   npm run build
   npm link
   ```

3. **Link the package globally** (so you can use `scip-python` command):
   ```bash
   npm link scip-python
   ```

#### Usage

```bash
scip-python index . --project-name=$MY_PROJECT --target-only=src/subdir
```

Related links:
- [Our scip-python fork](https://github.com/fishmingyu/scip-python/tree/exclude-config)
- [Original scip-python](https://github.com/sourcegraph/scip-python)

### Using the LSIndexer

The `LSIndexer` class provides a Python interface for working with SCIP
indices. CodeNib decodes `index.scip` with the packaged protobuf descriptor, so
users do not need a separate `protoc` executable.

#### Python Provider Resolution

For Python, CodeNib first resolves `scip-python` from `PATH`. The repository
graph intentionally omits the caller's external package inventory, which keeps
indexing reproducible and avoids scanning a large global Python environment.
The managed `scip-env` Conda route remains a compatibility fallback for
development environments where `scip-python` is not directly available.

Inspect the exact requirements for a repository before building:

```bash
codenib doctor /path/to/repository --require graph
```

#### Basic Usage

```python
from codenib.ls_router import LSIndexer

# Create an indexer for a project
# By default, output goes to $CODENIB_TEMP_DIR/<project_name>/
# (default: /tmp/codenib/<project_name>/)
indexer = LSIndexer("/path/to/project")

# Or specify a custom output directory
indexer = LSIndexer("/path/to/project", output_dir="/custom/output/path")

# Generate an index (uses scip-python on PATH, then managed Conda as fallback)
indexer.generate_index(project_name="MyProject", target_dir="src")

# Decode the index.scip file to index.decoded
indexer.decode_index()

# Process the decoded index and save results
result = indexer.process_index("output.json")

# Or run the complete pipeline with one call
result = indexer.run_pipeline(
    project_name="MyProject",
    target_dir="src",
    output_file="output.json"
)
```

#### Language-Specific Options

**Rust:**
```python
graph = indexer.run_pipeline(
    config_path="/path/to/config.json",       # cargo customization
    exclude_vendored_libraries=True,          # exclude vendored deps
)
```
Requires `Cargo.toml`.

**TypeScript / JavaScript:**
```python
graph = indexer.run_pipeline(
    yarn_workspaces=True,       # or pnpm_workspaces / npm_workspaces
)
```
Auto-detects workspace type, installs dependencies, and patches tsconfig to enable `allowJs: true` for JS files. If no `tsconfig.json`/`jsconfig.json` exists, `--infer-tsconfig` is enabled automatically.

**Go:**
```python
graph = indexer.run_pipeline()  # no language-specific options
```
Requires `go.mod`.

**C/C++:**
```python
graph = indexer.run_pipeline(
    compdb_path="/path/to/compile_commands.json",  # optional, auto-discovered
)
```
Uses clangd background indexing (`.idx` files). Auto-generates `compile_commands.json` from CMake or `bear -- make` if missing.

#### Advanced Features

**Cache Management:**
```python
# Run pipeline with cache awareness
# skip_level options: None, 'raw', 'decode', 'graph'
result = indexer.run_pipeline(
    project_name="MyProject",
    skip_level="graph"  # Reuse graph.pkl if exists
)

# Clear cache at different levels
indexer.clear_cache(level="all")     # Remove all cache files
indexer.clear_cache(level="graph")   # Keep only graph.pkl
indexer.clear_cache(level="decode")  # Keep only index.decoded
indexer.clear_cache(level="raw")     # Keep only index.scip
```

**Exclude Patterns:**
```python
# Exclude specific directories or files from indexing
indexer = LSIndexer(
    "/path/to/project",
    exclude_patterns=["tests/*", "*.test.py", "build/*"]
)
```

### LSGraphDecoder

Build a graph directly from an existing decoded index:

```python
from codenib.ls_router import LSGraphDecoder

decoder = LSGraphDecoder("index.decoded", project_root="/path/to/repo", language="rust")
graph = decoder.decode()

# C/C++: pass the .idx directory
decoder = LSGraphDecoder(".cache/clangd/index/", project_root="/path/to/repo", language="cpp")
```

### FAQ
If the following error occurs:
```
FATAL ERROR: Ineffective mark-compacts near heap limit Allocation failed - JavaScript heap out of memory
```

Please increase the memory space for node.js
``` bash
export NODE_OPTIONS="--max-old-space-size=16384"
```

If `compile_commands.json` is missing for C/C++ projects:
```bash
# CMake
cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON

# Make
bear -- make
```
