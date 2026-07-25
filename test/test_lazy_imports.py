# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_cli_and_static_wiki_do_not_import_optional_runtimes() -> None:
    root = Path(__file__).resolve().parents[1]
    script = """
import sys
import codenib
import codenib.web.app

forbidden = {
    "faiss",
    "igraph",
    "litellm",
    "matplotlib",
    "sentence_transformers",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit(f"optional runtimes imported eagerly: {loaded}")
"""

    subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
