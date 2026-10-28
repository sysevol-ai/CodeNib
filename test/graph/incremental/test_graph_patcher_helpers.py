# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import pytest
import test_graph_patcher as graph_patch


@pytest.mark.parametrize("reuse_cache", [True, False])
def test_failed_scip_build_is_not_retried(tmp_path, monkeypatch, reuse_cache):
    """The cache-aware pipeline already attempted the failed cold build."""
    calls = []

    def fail_build(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(graph_patch, "_git", lambda *_args, **_kwargs: "source-sha")
    monkeypatch.setattr(
        graph_patch,
        "LSIndexer",
        lambda **_kwargs: SimpleNamespace(run_pipeline=fail_build),
    )

    with pytest.raises(AssertionError, match="SCIP run_pipeline returned None"):
        graph_patch._build_graph_with_scip(
            tmp_path,
            "python",
            tmp_path / "cache",
            reuse_cache=reuse_cache,
        )

    assert calls == [
        {"skip_level": "graph" if reuse_cache else None, "report_profile": False}
    ]
