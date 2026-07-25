# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Serve per-commit symbol graphs produced by ``scripts/build_commit_window.py``.

The builder writes one graph snapshot per commit plus a ``commit_window.json``
manifest. This module is the read side: it locates that manifest for a repo,
exposes the selectable commits, and loads (and caches) the graph for a chosen
commit.

Kept separate from ``repo_registry`` on purpose -- that module carries local,
uncommitted operator changes, so feature code lives here instead.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Dict, List, Optional

from ..graph.code_graph import CodeGraph
from ..paths import REPO_INDEX_DIRNAME

logger = logging.getLogger(__name__)

CACHE_DIRNAME = REPO_INDEX_DIRNAME
WINDOW_DIRNAME = "commit_window"
MANIFEST_NAME = "commit_window.json"


def window_dir(repo_dir: str) -> str:
    """Directory holding a repo's commit-window artifacts."""
    return os.path.join(os.path.abspath(repo_dir), CACHE_DIRNAME, WINDOW_DIRNAME)


def manifest_path(repo_dir: str) -> str:
    return os.path.join(window_dir(repo_dir), MANIFEST_NAME)


def window_stats(commits: List[dict]) -> Optional[dict]:
    """Aggregate cold-vs-patched cost for a window.

    The single place this figure is derived. A speedup also appears in
    ``build_commit_window.py``'s closing log line; adding a second derivation in
    the API and a third in the frontend is how two different numbers end up on
    two different slides. Every surface renders what this returns.

    ``speedup`` is ``None`` -- never ``NaN`` or ``inf`` -- when there is no cold
    anchor, no patched transitions, or either figure is zero. Callers must treat
    that as "no claim available" rather than substituting a default.

    Zero-cost transitions stay in the mean: a transition that touched no indexed
    source legitimately cost 0.00s, and dropping it would flatter the result.
    """
    if not commits:
        return None

    cold = next((c for c in commits if c.get("method") == "cold"), None)
    patched = [c for c in commits if c.get("method") == "patched"]

    def _seconds(entry: dict) -> float:
        try:
            return float(entry.get("build_seconds") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    cold_seconds = _seconds(cold) if cold else None
    mean_patch = (sum(_seconds(c) for c in patched) / len(patched)) if patched else None

    speedup = None
    if cold_seconds and mean_patch:
        speedup = round(cold_seconds / mean_patch, 1)

    return {
        "commit_count": len(commits),
        "patched_count": len(patched),
        "cold_seconds": round(cold_seconds, 2) if cold_seconds is not None else None,
        "mean_patch_seconds": round(mean_patch, 2) if mean_patch is not None else None,
        "speedup": speedup,
    }


class CommitWindow:
    """Read-only view over one repo's per-commit graph snapshots.

    Graphs are loaded lazily and cached by commit SHA. A repo with no window
    on disk yields ``available == False`` and callers fall back to the single
    manifest-linked graph.
    """

    def __init__(self, repo_dir: str) -> None:
        self.repo_dir = os.path.abspath(repo_dir)
        self._manifest: Optional[dict] = None
        self._mtime: Optional[float] = None
        self._graphs: Dict[str, Optional[CodeGraph]] = {}
        self._lock = threading.RLock()

    # -- manifest ---------------------------------------------------------

    def _load_manifest(self) -> Optional[dict]:
        """Load the manifest, re-reading it when it changes on disk.

        Held under the lock for the whole read: publishing "loaded" before the
        parse finishes would let a concurrent request observe an empty manifest
        and report the window as unavailable. Endpoints reach this through
        ``asyncio.to_thread``, so concurrent first-requests are the normal case,
        not a rare one.

        Keyed on mtime so re-running ``build_commit_window.py`` against a live
        server takes effect without a restart -- otherwise the first request
        after startup pins the window for the process's lifetime.
        """
        path = manifest_path(self.repo_dir)
        try:
            mtime: Optional[float] = os.path.getmtime(path)
        except OSError:
            mtime = None

        with self._lock:
            if mtime is not None and mtime == self._mtime:
                return self._manifest

            self._mtime = mtime
            self._graphs.clear()
            if mtime is None:
                logger.debug("commit-window: no manifest at %s", path)
                self._manifest = None
                return None
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self._manifest = json.load(fh)
            except (
                Exception
            ) as exc:  # noqa: BLE001 - a bad manifest just disables the feature
                logger.warning("commit-window: unusable manifest %s: %s", path, exc)
                self._manifest = None
            return self._manifest

    @property
    def available(self) -> bool:
        m = self._load_manifest()
        return bool(m and m.get("commits"))

    def commits(self) -> List[dict]:
        """Selectable commits, newest first (UI order)."""
        m = self._load_manifest()
        if not m:
            return []
        return list(m.get("commits") or [])

    def newest(self) -> Optional[dict]:
        commits = self.commits()
        return commits[0] if commits else None

    def resolve(self, commit: Optional[str]) -> Optional[dict]:
        """Find a commit entry by full or short SHA.

        ``None`` selects the newest commit, so callers can pass an absent query
        parameter straight through.
        """
        commits = self.commits()
        if not commits:
            return None
        if not commit:
            return commits[0]
        needle = commit.strip().lower()
        exact = []
        for entry in commits:
            sha = str(entry.get("sha", "")).lower()
            short = str(entry.get("short", "")).lower()
            if needle in (sha, short):
                exact.append(entry)
        if exact:
            return exact[0]
        if len(needle) < 4:
            return None
        prefixes = [
            entry
            for entry in commits
            if str(entry.get("sha", "")).lower().startswith(needle)
        ]
        return prefixes[0] if len(prefixes) == 1 else None

    # -- graphs -----------------------------------------------------------

    def graph_for(self, commit: Optional[str]) -> Optional[CodeGraph]:
        """Load (and cache) the graph snapshot for *commit*.

        Returns ``None`` when the window is absent or the commit is unknown, so
        the caller can fall back to the repo's default graph.
        """
        entry = self.resolve(commit)
        if entry is None:
            return None
        sha = str(entry.get("sha", ""))
        with self._lock:
            if sha in self._graphs:
                return self._graphs[sha]

            path = entry.get("graph_path")
            # Manifests record absolute paths at build time; tolerate a moved
            # cache directory by also probing the expected filename.
            candidates = [
                p
                for p in (
                    path,
                    os.path.join(
                        window_dir(self.repo_dir), f"graph_{entry.get('short')}.pkl"
                    ),
                )
                if p
            ]
            graph: Optional[CodeGraph] = None
            for candidate in candidates:
                if not os.path.isfile(candidate):
                    continue
                try:
                    graph = CodeGraph.load_graph(candidate)
                    logger.info(
                        "commit-window: loaded graph %s (%s)",
                        entry.get("short"),
                        candidate,
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - stale/old-format snapshot
                    logger.warning(
                        "commit-window: graph at %s unusable: %s", candidate, exc
                    )
            if graph is None:
                logger.warning(
                    "commit-window: no usable snapshot for %s", entry.get("short")
                )
            self._graphs[sha] = graph
            return graph

    def summary(self) -> dict:
        """Payload for the commit selector."""
        m = self._load_manifest()
        if not m or not m.get("commits"):
            return {"available": False, "commits": [], "selected": None}
        commits = [
            {
                "sha": c.get("sha"),
                "short": c.get("short"),
                "subject": c.get("subject"),
                "date": c.get("date"),
                "author": c.get("author"),
                "method": c.get("method"),
                "build_seconds": c.get("build_seconds"),
                "node_count": c.get("node_count"),
                "edge_count": c.get("edge_count"),
                "changed_files": c.get("changed_files"),
            }
            for c in m["commits"]
        ]
        # Newer manifests record every language actually built; older ones only
        # carry the single `language` field.
        languages = m.get("languages") or ([m["language"]] if m.get("language") else [])
        return {
            "available": True,
            "stats": window_stats(m["commits"]),
            "ref": m.get("ref"),
            "language": m.get("language"),
            "languages": languages,
            "lsp_startup_seconds": m.get("lsp_startup_seconds"),
            "commits": commits,
            "selected": commits[0]["sha"] if commits else None,
        }
