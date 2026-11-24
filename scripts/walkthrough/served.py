# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Merge one repository's walks into the file the Wiki serves.

The served file is ``<data_dir>/walkthroughs/<repo_id>.json``, read by
``codenib.web.walkthroughs``: version 1, the repository (id, slug, commit,
model), the shared areas, links, paths and Wiki citations, the walks, and
the source lines every walk shows. The build log is left out.
"""

from __future__ import annotations

import json
from pathlib import Path

from codenib.web.walkthroughs import WALKTHROUGH_VERSION, walkthrough_path

SERVED_KEYS = ("repo", "areas", "links", "paths", "wiki", "walks", "source")
REPO_KEYS = ("id", "slug", "commit", "model")


def merge_parts(parts: list[dict]) -> dict:
    """One file from per-question files that each hold one walk.

    The first part supplies the repository fields; walks keep their order;
    source lines and paths are unioned; the logs' calls and checks are
    concatenated.
    """

    out = json.loads(json.dumps(parts[0]))
    out["walks"] = [p["walks"][0] for p in parts]
    for p in parts[1:]:
        for f, lines in p["source"].items():
            out["source"].setdefault(f, {}).update(lines)
        out["paths"].update(p["paths"])
        out["log"]["calls"] += p["log"]["calls"]
        out["log"]["checks"] += p["log"]["checks"]
    return out


def served_form(merged: dict) -> dict:
    out = {"version": WALKTHROUGH_VERSION, **{k: merged[k] for k in SERVED_KEYS}}
    out["repo"] = {k: merged["repo"][k] for k in REPO_KEYS if k in merged["repo"]}
    return out


def write_served(merged: dict, out_dir: str | Path) -> Path:
    """Write ``<out_dir>/<repo_id>.json``; its path.

    ``out_dir`` is the ``walkthroughs`` directory inside a data dir.
    """

    repo_id = merged["repo"]["id"]
    if walkthrough_path(out_dir, repo_id) is None:
        raise ValueError(f"unsafe repository id {repo_id!r}")
    target = Path(out_dir) / f"{repo_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(served_form(merged), ensure_ascii=False, separators=(",", ":"))
    target.write_text(text, encoding="utf-8")
    return target
