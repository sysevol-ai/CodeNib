#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Freeze the repository-disjoint ContextBench external-validity protocol."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> int:
    from huggingface_hub import hf_hub_download
    from pyarrow import parquet

    from codeminer.eval.contextbench_study import (
        CONTEXTBENCH_DATASET,
        CONTEXTBENCH_REVISION,
        CONTEXTBENCH_TEST_FILE,
        build_protocol,
        sha256_file,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    dataset_file = Path(
        hf_hub_download(
            repo_id=CONTEXTBENCH_DATASET,
            repo_type="dataset",
            filename=CONTEXTBENCH_TEST_FILE,
            revision=CONTEXTBENCH_REVISION,
        )
    )
    protocol = build_protocol(
        parquet.read_table(dataset_file).to_pylist(),
        dataset_file_sha256=sha256_file(dataset_file),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(protocol, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    selection = protocol["selection"]
    print(
        f"frozen {selection['selected_rows']} rows / "
        f"{selection['selected_repositories']} repositories; output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
