# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Read-side behaviour of the commit window served to the graph view."""

import json
import os

from codeminer.web.commit_window import CommitWindow, window_dir


def _write_manifest(repo_dir, commits, **extra) -> str:
    d = window_dir(str(repo_dir))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "commit_window.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"commits": commits, **extra}, fh)
    return path


def _commit(sha: str, short: str, method: str = "patched", **extra) -> dict:
    return {
        "sha": sha,
        "short": short,
        "subject": f"subject {short}",
        "date": "2026-07-20",
        "author": "someone",
        "method": method,
        "build_seconds": 1.0,
        "node_count": 100,
        "edge_count": 200,
        "changed_files": 1,
        **extra,
    }


class TestAvailability:
    def test_absent_manifest_is_unavailable(self, tmp_path):
        assert CommitWindow(str(tmp_path)).available is False

    def test_unparseable_manifest_is_unavailable(self, tmp_path):
        d = window_dir(str(tmp_path))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "commit_window.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        assert CommitWindow(str(tmp_path)).available is False

    def test_empty_commit_list_is_unavailable(self, tmp_path):
        _write_manifest(tmp_path, [])
        assert CommitWindow(str(tmp_path)).available is False


class TestManifestInvalidation:
    """A rebuilt window must take effect without restarting the server."""

    def test_rebuild_is_picked_up(self, tmp_path):
        _write_manifest(tmp_path, [_commit("a" * 40, "aaaaaaa", method="cold")])
        window = CommitWindow(str(tmp_path))
        assert len(window.commits()) == 1

        path = _write_manifest(
            tmp_path,
            [
                _commit("b" * 40, "bbbbbbb"),
                _commit("a" * 40, "aaaaaaa", method="cold"),
            ],
        )
        # Force a distinct mtime; filesystem granularity can collapse two
        # writes in the same tick into one timestamp.
        os.utime(path, (0, 0))

        assert len(window.commits()) == 2

    def test_graph_cache_cleared_on_rebuild(self, tmp_path):
        _write_manifest(tmp_path, [_commit("a" * 40, "aaaaaaa", method="cold")])
        window = CommitWindow(str(tmp_path))
        window.graph_for(None)  # populates the negative cache (no pkl on disk)
        assert window._graphs

        path = _write_manifest(tmp_path, [_commit("a" * 40, "aaaaaaa", method="cold")])
        os.utime(path, (0, 0))
        window.commits()

        assert not window._graphs, "stale graph cache survived a manifest rebuild"


class TestResolve:
    def _window(self, tmp_path) -> CommitWindow:
        _write_manifest(
            tmp_path,
            [
                _commit("b" * 40, "bbbbbbb"),
                _commit("a" * 40, "aaaaaaa", method="cold"),
            ],
        )
        return CommitWindow(str(tmp_path))

    def test_none_selects_newest(self, tmp_path):
        assert self._window(tmp_path).resolve(None)["sha"] == "b" * 40

    def test_full_sha(self, tmp_path):
        assert self._window(tmp_path).resolve("a" * 40)["short"] == "aaaaaaa"

    def test_short_sha(self, tmp_path):
        assert self._window(tmp_path).resolve("aaaaaaa")["sha"] == "a" * 40

    def test_prefix(self, tmp_path):
        assert self._window(tmp_path).resolve("aaaa")["sha"] == "a" * 40

    def test_case_insensitive(self, tmp_path):
        assert self._window(tmp_path).resolve("AAAAAAA")["sha"] == "a" * 40

    def test_unknown_returns_none(self, tmp_path):
        assert self._window(tmp_path).resolve("deadbeef") is None

    def test_too_short_prefix_returns_none(self, tmp_path):
        assert self._window(tmp_path).resolve("aa") is None

    def test_ambiguous_prefix_returns_none(self, tmp_path):
        _write_manifest(
            tmp_path,
            [
                _commit("abcd" + "1" * 36, "abcd111"),
                _commit("abcd" + "2" * 36, "abcd222"),
            ],
        )
        assert CommitWindow(str(tmp_path)).resolve("abcd") is None


class TestGraphFor:
    def test_missing_snapshot_returns_none(self, tmp_path):
        """An unloadable snapshot must be reported, not silently substituted."""
        _write_manifest(
            tmp_path,
            [_commit("a" * 40, "aaaaaaa", method="cold", graph_path="/nope/x.pkl")],
        )
        assert CommitWindow(str(tmp_path)).graph_for(None) is None

    def test_unknown_commit_returns_none(self, tmp_path):
        _write_manifest(tmp_path, [_commit("a" * 40, "aaaaaaa", method="cold")])
        assert CommitWindow(str(tmp_path)).graph_for("deadbeef") is None


class TestSummary:
    def test_unavailable_shape(self, tmp_path):
        s = CommitWindow(str(tmp_path)).summary()
        assert s == {"available": False, "commits": [], "selected": None}

    def test_selected_is_newest(self, tmp_path):
        _write_manifest(
            tmp_path,
            [
                _commit("b" * 40, "bbbbbbb"),
                _commit("a" * 40, "aaaaaaa", method="cold"),
            ],
            languages=["python"],
        )
        s = CommitWindow(str(tmp_path)).summary()
        assert s["available"] is True
        assert s["selected"] == "b" * 40
        assert [c["short"] for c in s["commits"]] == ["bbbbbbb", "aaaaaaa"]

    def test_legacy_manifest_language_field(self, tmp_path):
        """Older manifests carry `language` but not `languages`."""
        _write_manifest(
            tmp_path,
            [_commit("a" * 40, "aaaaaaa", method="cold")],
            language="python",
        )
        assert CommitWindow(str(tmp_path)).summary()["languages"] == ["python"]
