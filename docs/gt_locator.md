<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# GTLocator

Extract symbol-level ground truth changes (functions, classes, methods) from SWE-bench unified diff patches. Identifies which symbols were modified, added, or deleted.

Supports Python, Go, Rust, C/C++, C#, Java, Ruby, PHP, Kotlin, Swift, Scala, Lua, JavaScript, and TypeScript.

## How It Works

1. Clone repo and checkout `base_commit`
2. Extract symbols from affected files using tree-sitter chunkers
3. Apply patch
4. Extract symbols again
5. Compare before/after to classify changes as modified, added, or deleted

**Change detection** uses three-level filtering:
- Set difference → added/deleted symbols
- Line count change → modified (cheapest check)
- Line range overlap + content diff → modified (only when lengths match)

## Prerequisites

- `tree-sitter-language-pack` (bundled with CodeNib, provides parsers for all supported languages)
- `git` (for cloning and checking out repos)

## Usage

```python
from codenib.dataset.gt_locate import GTLocator

locator = GTLocator(work_dir="/data/repos")

result = locator.analyze_instance({
    "instance_id": "django__django-12345",
    "repo": "django/django",
    "base_commit": "abc123",
    "patch": "--- a/file.py\n+++ b/file.py\n@@ -10,3 +10,5 @@\n...",
})
```

**Parameters:**

- `work_dir` (str, optional): Directory for cloning repos. Default: `~/.codenib/tmp` (auto-cleaned on `cleanup()`)
- `language` (str, optional): Force a specific language instead of auto-detecting from file extensions

**Output:**
```python
{
    "instance_id": "django__django-12345",
    "target_files": ["django/db/models/query.py"],
    "code_blocks": [{
        "file_path": "django/db/models/query.py",
        "symbol": "QuerySet.filter()",
        "start_line": 100,    # 1-based
        "end_line": 120,
        "symbol_type": "method",
        "change_type": "modified",
    }],
    "symbols_modified": ["django/db/models/query.py:QuerySet.filter()"],
    "symbols_added": [],
    "symbols_deleted": [],
    "error": None,
}
```

**Symbol naming**: `file:func()`, `file:Class`, `file:Class.method()`. Line numbers in output are 1-based.

### Lightweight Usage

```python
# Just extract affected file paths from a patch (no clone needed)
files = locator.get_target_files(patch_content)
# ["django/db/models/query.py", "django/db/models/manager.py"]

# Extract changed line ranges from a patch
ranges = locator.get_changed_line_ranges(patch_content)
# {"django/db/models/query.py": [(100, 105), (200, 210)]}
```

### Batch Processing

```python
from codenib.dataset.gt_locate import build_gt_metadata

results = build_gt_metadata(dataset, work_dir="/data/repos", keep_repos=True)
```

`SwebenchDataset.load_eval_metadata()` calls `build_gt_metadata` itself when
its annotation file does not exist yet, so retrieval and agent harnesses can
run on SWE-bench without a separate preparation step. The first run clones
every repository and is slow.

## Command Line

Write ground truth for a whole SWE-bench split:

```bash
python -m codenib.dataset.gt_locate --dataset verified --split test \
  --filter '^django__' --limit 20
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--dataset` | `verified` | `verified` or `lite` |
| `--split` | `test` | Hugging Face split |
| `--output` | `~/.codenib/swebench_{dataset}_{split}_gt.json` | Output JSON path |
| `--work-dir` | `~/.codenib/tmp` | Where repositories are cloned |
| `--limit` | all | Maximum number of instances |
| `--filter` | `.*` | Regular expression over instance IDs |
| `--keep-repos` | off | Keep repositories cloned into the default work directory |

## Line Numbers

Tree-sitter chunks are 0-based. `GTLocator` converts them once, when it emits
`code_blocks`, so every `start_line` and `end_line` in its output is 1-based
and inclusive. Scorers compare these blocks with predicted spans by overlap;
see [Harnesses and metrics](evaluation/harnesses.md#code-block-metrics).
