#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Profile generic LSP graph decoding on a synthetic payload.

This isolates the Python decode/build cost from language-server JSON-RPC time.
Use it before adding a C++ LSP decoder: if decode is not a material fraction of
end-to-end indexing, core acceleration belongs elsewhere.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from codenib.ls_index.lsp_graph_decode import GenericLSPGraphDecoder


def _sym(name: str, kind: int, start: int, end: int, children=None) -> dict:
    return {
        "name": name,
        "kind": kind,
        "range": {
            "start": {"line": start, "character": 0},
            "end": {"line": end, "character": 0},
        },
        "selectionRange": {
            "start": {"line": start, "character": 4},
            "end": {"line": start, "character": 4 + len(name)},
        },
        "children": children or [],
    }


def build_payload(
    root: Path,
    *,
    files: int,
    methods_per_file: int,
    include_references: bool,
) -> dict:
    entries = []
    for file_idx in range(files):
        rel = Path("src/main/java/app") / f"Class{file_idx}.java"
        abs_path = root / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text("// synthetic\n", encoding="utf-8")

        class_name = f"Class{file_idx}"
        methods = [
            _sym(f"method{method_idx}", 6, method_idx * 3 + 2, method_idx * 3 + 3)
            for method_idx in range(methods_per_file)
        ]
        entry = {
            "path": rel.as_posix(),
            "symbols": [_sym(class_name, 5, 0, methods_per_file * 3 + 5, methods)],
        }
        if include_references and files > 1 and methods_per_file > 0:
            next_rel = Path("src/main/java/app") / f"Class{(file_idx + 1) % files}.java"
            next_abs = root / next_rel
            entry["references"] = [
                {
                    "target_unified_name": f"{rel.as_posix()}:{class_name}.method0()",
                    "target_start_line": 2,
                    "target_file": rel.as_posix(),
                    "locations": [
                        {
                            "uri": next_abs.as_uri(),
                            "range": {
                                "start": {"line": 3, "character": 8},
                                "end": {"line": 3, "character": 15},
                            },
                        }
                    ],
                }
            ]
        entries.append(entry)
    return {
        "schema_version": 1,
        "language": "java",
        "project_root": str(root),
        "files": entries,
    }


def profile_decode(payload: dict, root: Path) -> dict:
    index_path = root / "index.lsp.json"
    index_path.write_text(json.dumps(payload), encoding="utf-8")
    t0 = time.perf_counter()
    graph = GenericLSPGraphDecoder(str(index_path), project_root=str(root)).decode()
    elapsed = time.perf_counter() - t0
    return {
        "files": len(payload["files"]),
        "vertices": graph.graph.vcount(),
        "edges": graph.graph.ecount(),
        "decode_seconds": elapsed,
        "vertices_per_second": graph.graph.vcount() / elapsed if elapsed else None,
        "edges_per_second": graph.graph.ecount() / elapsed if elapsed else None,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=500)
    parser.add_argument("--methods-per-file", type=int, default=20)
    parser.add_argument("--include-references", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        payload = build_payload(
            root,
            files=args.files,
            methods_per_file=args.methods_per_file,
            include_references=args.include_references,
        )
        print(json.dumps(profile_decode(payload, root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
