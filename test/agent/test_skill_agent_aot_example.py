# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Smoke test for ``examples/skill_agent_aot.py``.

Runs the example with ``--no-llm`` against the session-cached
``httpie_cli_repo`` fixture. Verifies Phase 1 wrote a manifest with a
fresh BM25 entry and that the example exits cleanly.

LLM-dependent Phase 2 paths are exercised by ``test_query_e2e.py`` (the
:func:`query` facade itself) — this test only validates that the
``examples/`` driver script stays runnable from a clean shell as a
copy-paste tutorial.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_EXAMPLE = (
    Path(__file__).resolve().parent.parent.parent / "examples" / "skill_agent_aot.py"
)


@pytest.mark.integration
def test_skill_agent_aot_example_phase1_smoke(httpie_cli_repo, tmp_path):
    """Run the example with --no-llm; assert repo_manifest.json shows a fresh BM25."""
    if not _EXAMPLE.exists():
        pytest.skip(f"example missing: {_EXAMPLE}")

    cache_dir = tmp_path / "cache"

    result = subprocess.run(
        [
            sys.executable,
            str(_EXAMPLE),
            "--repo",
            str(httpie_cli_repo),
            "--cache-dir",
            str(cache_dir),
            "--index-types",
            "bm25",
            "--languages",
            "python",
            "--no-llm",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, (
        f"example failed (exit={result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    manifest_path = cache_dir / "repo_manifest.json"
    assert manifest_path.exists(), f"manifest not written at {manifest_path}"

    with open(manifest_path) as f:
        manifest = json.load(f)
    assert "bm25" in manifest["indexes"], "bm25 entry missing from manifest"
    assert (
        manifest["indexes"]["bm25"]["status"] == "fresh"
    ), f"expected fresh bm25 entry, got {manifest['indexes']['bm25']}"

    # The Phase-1-only branch must explicitly say it skipped Phase 2.
    assert "[Phase 2] Skipped (--no-llm)" in result.stdout
