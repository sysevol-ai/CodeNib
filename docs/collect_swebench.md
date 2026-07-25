<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Collecting SWE-bench Instances

This guide covers how to use the `collect_swebench.sh` script to sample representative SWE-bench instances across multiple languages.

## What It Does

The collection pipeline:

1. Loads SWE-bench datasets (Python from SWE-bench Lite, others from SWE-bench Multilingual)
2. Selects top repositories per language by instance count
3. Samples instances per repo with patch-size diversity
4. Classifies difficulty (low/medium/high) using a Claude agent
5. Extracts ground-truth code blocks from patches via tree-sitter
6. Filters out instances exceeding a GT code block threshold
7. Saves all artifacts to the output directory

## Prerequisites

1. **Install the project:**

   ```bash
   pip install -e .
   ```

2. **Authenticate for difficulty classification** — the pipeline uses `claude_agent_sdk` to call Claude. Choose one method:

   ```bash
   # Option A: log in via the Claude client (no API key needed)
   claude login

   # Option B: set an API key
   export ANTHROPIC_API_KEY="sk-ant-..."
   ```

## Quick Start

Run the wrapper script with default settings:

```bash
scripts/collect_swebench.sh
```

This samples from all 5 languages (`Python`, `Rust`, `TypeScript/JavaScript`, `C++/C`, `Go`) and writes output to `~/.codenib/swebench_sampling/`.

## Environment Variables

The shell wrapper accepts these environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `CACHE_DIR` | `~/.codenib` | Root cache directory (datasets, repo clones) |
| `OUTPUT_DIR` | `$CACHE_DIR/swebench_sampling` | Output directory for sampling artifacts |

Example:

```bash
CACHE_DIR=/data/codenib OUTPUT_DIR=/data/output scripts/collect_swebench.sh
```

## Python Script Options

For finer control, call `collect_swebench.py` directly:

```bash
PYTHONPATH=. python scripts/collect_swebench.py [OPTIONS]
```

### All Options

| Flag | Default | Description |
|------|---------|-------------|
| `--languages` | All 5 | Space- or comma-separated language list |
| `--split` | `test` | Dataset split |
| `--filter-instance` | `.*` | Regex filter for instance IDs |
| `--repos-per-language` | `5` | Max repos to select per language |
| `--instances-per-repo` | `4` | Target instances per repo |
| `--total-instances` | `100` | Target total instances (adaptive allocation) |
| `--min-instances` | `3` | Minimum instances a repo must have to be selected |
| `--max-gt-code-blocks` | `10` | Exclude instances with more GT blocks than this |
| `--difficulty-model` | `opus` | Model for difficulty classification |
| `--shallow-clone` / `--no-shallow-clone` | `true` | Use shallow git clones for repos |
| `--cache-dir` | `~/.codenib` | Root cache directory |
| `--repo-cache-dir` | same as `--cache-dir` | Separate directory for repo clones |
| `--output-dir` | `$cache-dir/swebench_sampling` | Output directory |
| `--multilingual-csv-path` | bundled | Path to `swebench_multilingual_repos.csv` |
| `--print-sample` | `5` | Print first N instances to stdout |
| `--save-sampled` | — | Extra path to save sampled instances JSON |

### Examples

**Sample only Go and Rust, 50 total instances:**

```bash
PYTHONPATH=. python scripts/collect_swebench.py \
  --languages Go Rust \
  --total-instances 50 \
  --output-dir output/go-rust
```

**Sample with a specific instance filter:**

```bash
PYTHONPATH=. python scripts/collect_swebench.py \
  --filter-instance "redis.*" \
  --languages "C++/C" \
  --output-dir output/redis-only
```

## Output Artifacts

The pipeline writes the following files to the output directory:

| File | Description |
|------|-------------|
| `selected_instances.json` | Full instance data with GT fields — input for the HuggingFace upload script |
| `selected_repos.json` | Selected repositories grouped by language |
| `selected_instances.csv` | Summary CSV with key fields (language, repo, difficulty, etc.) |
| `difficulty_cache.json` | Cached difficulty classifications (avoids re-calling the LLM) |
| `gt_locator_cache.json` | Cached ground-truth extraction results |

The `selected_instances.json` file is the primary output — pass it to `build_swebench_locator_hf_dataset.py` to upload to HuggingFace (see [upload_dataset_to_huggingface.md](upload_dataset_to_huggingface.md)).

## Caching

The pipeline caches aggressively to avoid redundant work on reruns:

- **Dataset cache** (`~/.codenib/`) — HuggingFace dataset downloads
- **Repo clones** (`cache-dir`) — shallow git clones of target repos
- **Difficulty cache** (`difficulty_cache.json`) — LLM difficulty classifications
- **GT cache** (`gt_locator_cache.json`) — ground-truth extraction results

To force a fresh run, delete the relevant cache files or use a new output directory.
