<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

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
  - `yarn` for repos using Yarn workspaces (optional but commonly needed)
  - `pnpm` for pnpm workspaces (optional)
- C/C++ (`clangd`):
  - `clangd` (`apt install clangd`)
  - `cmake` for CMake-based projects
  - `bear` for Make/Autotools projects (e.g., repositories without CMake compile DB)
  - Requires `compile_commands.json` (auto-generated from CMake or `bear -- make` if missing)
- Rust (`rust-analyzer`):
  - nightly toolchain with `rust-analyzer` component (auto-set via `RUSTUP_TOOLCHAIN=nightly`)
- Go (`scip-go`):
  - `go install github.com/sourcegraph/scip-go/cmd/scip-go@latest`
  - Project must contain `go.mod`

Example installs:
```bash
# TypeScript workspace tooling
npm install -g @sourcegraph/scip-typescript yarn

# C/C++ Make-based compilation database helper
# Ubuntu/Debian:
sudo apt-get update && sudo apt-get install -y bear
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

### Convert index.scip to index.decoded

First install the [protobuf](https://protobuf.dev/installation/).
Get the scip.proto from [SCIP](https://github.com/sourcegraph/scip/tree/main).

### Using the LSIndexer

The `LSIndexer` class provides a Python interface for working with SCIP indices. **It automatically handles conda environment isolation** to prevent conflicts with system Python packages.

#### Conda Environment Isolation

**Important:** The LSIndexer uses conda for environment isolation when running `scip-python`. This prevents issues with:
- Package version conflicts
- System Python package interference
- Inconsistent dependency resolution

The indexer automatically:
1. Checks if conda is installed
2. Creates a dedicated `scip-env` environment (if not exists) using [scip-environment.yml](https://github.com/sysevol-ai/CodeMiner/blob/main/codeminer/scip_interface/scip-environment.yml)
3. Runs all `scip-python` commands within this isolated environment

**Manual conda environment setup** (optional - the indexer does this automatically):
```bash
conda env create -f codeminer/scip_interface/scip-environment.yml
```

#### Basic Usage

```python
from codeminer.ls_router import LSIndexer

# Create an indexer for a project
# By default, output goes to /tmp/<project_name>/
indexer = LSIndexer("/path/to/project")

# Or specify a custom output directory
indexer = LSIndexer("/path/to/project", output_dir="/custom/output/path")

# Generate an index (runs in isolated conda environment automatically)
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
from codeminer.ls_router import LSGraphDecoder

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
