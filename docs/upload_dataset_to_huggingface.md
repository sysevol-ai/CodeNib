<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Uploading the Dataset to HuggingFace

This guide covers how to build and upload the SWE-bench locator dataset to HuggingFace Hub.

## Prerequisites

1. **Install dependencies** — the project already includes the `datasets` library (`>=4.0.0`):

   ```bash
   pip install -e .
   ```

2. **Authenticate with HuggingFace Hub** — choose one method:

   ```bash
   # Option A: interactive login (stores token in ~/.cache/huggingface/)
   hf login

   # Option B: environment variable
   export HF_TOKEN="hf_your_token_here"
   ```

   You need a **write** token. Create one at https://huggingface.co/settings/tokens.

3. **Prepare the input file** — you need a `selected_instances.json` file with ground-truth fields already computed (output of the GT extraction + difficulty classification pipeline).

## Upload Command

```bash
python scripts/build_swebench_locator_hf_dataset.py \
  --selected-instances path/to/selected_instances.json \
  --output-dir output/dataset \
  --push-to-hub "your-org/your-dataset-name"
```

### All Options

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--selected-instances` | Yes | — | Path to `selected_instances.json` with GT fields |
| `--output-dir` | Yes | — | Local output directory for Parquet files |
| `--split` | No | `test` | Dataset split name |
| `--push-to-hub` | No | — | HuggingFace Hub repo ID (e.g. `org/dataset-name`) |
| `--private` | No | `false` | Make the Hub dataset private |
| `--output-jsonl` | No | — | Also export a JSONL file |
| `--output-parquet` | No | — | Also export an extra Parquet file |

### Examples

**Local-only build (no upload):**

```bash
python scripts/build_swebench_locator_hf_dataset.py \
  --selected-instances data/selected_instances.json \
  --output-dir output/swebench-locator
```

## What Gets Uploaded

The script creates a HuggingFace `DatasetDict` with a single split (default: `test`) containing these fields per instance:

| Field | Type | Description |
|-------|------|-------------|
| `instance_id` | string | Unique SWE-bench identifier |
| `repo` | string | GitHub repository (e.g. `redis/redis`) |
| `base_commit` | string | Base commit hash |
| `patch` | string | The gold patch |
| `problem_statement` | string | Issue description |
| `language_group` | string | Language (e.g. `Go`, `C++/C`, `Rust`, `TypeScript/JavaScript`) |
| `difficulty_level` | string | `low`, `medium`, or `high` |
| `target_files` | list[string] | Files modified by the patch |
| `gt_code_blocks` | list[dict] | Ground-truth code blocks (see below) |
| `gt_code_blocks_count` | int | Number of GT code blocks |
| `gt_symbols_modified` | list[string] | Modified symbol names |
| `gt_symbols_deleted` | list[string] | Deleted symbol names |
| `gt_target_files` | list[string] | Target files from GT extraction |

Each entry in `gt_code_blocks` contains:

| Field | Type | Description |
|-------|------|-------------|
| `file_path` | string | Path to the file |
| `symbol` | string | Symbol name (function, class, etc.) |
| `symbol_type` | string | Type of symbol (`function`, `method`, `class`, `struct`, etc.) |
| `change_type` | string | `modified`, `added`, or `deleted` |
| `start_line` | int | Start line (1-based) |
| `end_line` | int | End line (1-based) |

> Note: `changed_loc` and `total_in_repo` fields are dropped from the output automatically.

## Output

After running, you'll see a summary like:

```
Loaded 50 instance(s) from data/selected_instances.json
Saved Parquet (50 rows) to output/swebench-locator/test-00000-of-00001.parquet
Pushed dataset to https://huggingface.co/datasets/sysevol-ai/swebench-locator
Summary: total=50  gt_nonempty=48  by_language={'Go': 14, 'C++/C': 12, 'Rust': 7, 'TypeScript/JavaScript': 17}
```

The dataset will be available at `https://huggingface.co/datasets/<your-org>/<your-dataset-name>`.

## Updating an Existing Dataset

Simply re-run the same command with `--push-to-hub` pointing to the existing repo. The `push_to_hub()` call will overwrite the split with the new data.

## Troubleshooting

- **`401 Unauthorized`** — run `hf login` or check your `HF_TOKEN`.
- **`403 Forbidden`** — your token may lack write permission, or you don't have push access to the target org/repo.
- **`No instances found`** — the input JSON is empty or malformed. Ensure it's a JSON array of instance dicts.
