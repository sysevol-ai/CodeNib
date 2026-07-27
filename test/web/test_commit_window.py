# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Read-side behaviour of the commit window served to the graph view."""

import json
import os

import pytest

from codenib.paths import legacy_repo_index_dir, repo_index_dir
from codenib.web.commit_window import CommitWindow, window_dir


@pytest.fixture(autouse=True)
def _isolated_codenib_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "user-state"))


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


class TestWindowPath:
    def test_uses_user_owned_repository_state(self, tmp_path):
        expected = repo_index_dir(tmp_path) / "commit_window"

        assert window_dir(str(tmp_path)) == str(expected)

    def test_falls_back_to_legacy_repository_local_window(self, tmp_path):
        legacy = legacy_repo_index_dir(tmp_path) / "commit_window"
        legacy.mkdir(parents=True)
        (legacy / "commit_window.json").write_text('{"commits": []}')

        assert window_dir(str(tmp_path)) == str(legacy)


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

    def test_migration_from_legacy_to_user_state_is_picked_up(self, tmp_path):
        legacy = legacy_repo_index_dir(tmp_path) / "commit_window"
        legacy.mkdir(parents=True)
        legacy_manifest = legacy / "commit_window.json"
        legacy_manifest.write_text(
            json.dumps({"commits": [_commit("a" * 40, "aaaaaaa", method="cold")]})
        )
        window = CommitWindow(str(tmp_path))
        assert len(window.commits()) == 1

        primary = repo_index_dir(tmp_path) / "commit_window"
        primary.mkdir(parents=True)
        primary_manifest = primary / "commit_window.json"
        primary_manifest.write_text(
            json.dumps(
                {
                    "commits": [
                        _commit("b" * 40, "bbbbbbb"),
                        _commit("a" * 40, "aaaaaaa", method="cold"),
                    ]
                }
            )
        )
        legacy_mtime = legacy_manifest.stat().st_mtime
        os.utime(primary_manifest, (legacy_mtime, legacy_mtime))

        assert len(window.commits()) == 2

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


class TestWindowStats:
    """The single derivation point for the cold-vs-patched figure."""

    def test_normal_window(self):
        from codenib.web.commit_window import window_stats

        s = window_stats(
            [
                _commit("b" * 40, "b", build_seconds=2.0),
                _commit("c" * 40, "c", build_seconds=6.0),
                _commit("a" * 40, "a", method="cold", build_seconds=96.0),
            ]
        )
        assert s == {
            "commit_count": 3,
            "patched_count": 2,
            "cold_seconds": 96.0,
            "mean_patch_seconds": 4.0,
            "speedup": 24.0,
        }

    def test_zero_cost_transition_stays_in_the_mean(self):
        """A transition touching no indexed source is real; excluding it flatters."""
        from codenib.web.commit_window import window_stats

        s = window_stats(
            [
                _commit("b" * 40, "b", build_seconds=0.0),
                _commit("c" * 40, "c", build_seconds=8.0),
                _commit("a" * 40, "a", method="cold", build_seconds=80.0),
            ]
        )
        assert s["mean_patch_seconds"] == 4.0
        assert s["speedup"] == 20.0

    def test_no_cold_anchor_yields_no_speedup(self):
        from codenib.web.commit_window import window_stats

        s = window_stats([_commit("b" * 40, "b", build_seconds=2.0)])
        assert s["cold_seconds"] is None
        assert s["speedup"] is None

    def test_single_commit_window_yields_no_speedup(self):
        from codenib.web.commit_window import window_stats

        s = window_stats([_commit("a" * 40, "a", method="cold", build_seconds=96.0)])
        assert s["patched_count"] == 0
        assert s["mean_patch_seconds"] is None
        assert s["speedup"] is None

    def test_zero_mean_patch_does_not_divide(self):
        from codenib.web.commit_window import window_stats

        s = window_stats(
            [
                _commit("b" * 40, "b", build_seconds=0.0),
                _commit("a" * 40, "a", method="cold", build_seconds=96.0),
            ]
        )
        assert s["mean_patch_seconds"] == 0.0
        assert s["speedup"] is None

    def test_zero_cold_does_not_divide(self):
        from codenib.web.commit_window import window_stats

        s = window_stats(
            [
                _commit("b" * 40, "b", build_seconds=2.0),
                _commit("a" * 40, "a", method="cold", build_seconds=0.0),
            ]
        )
        assert s["speedup"] is None

    def test_non_numeric_build_seconds_does_not_raise(self):
        from codenib.web.commit_window import window_stats

        s = window_stats(
            [
                _commit("b" * 40, "b", build_seconds="oops"),
                _commit("a" * 40, "a", method="cold", build_seconds=96.0),
            ]
        )
        assert s["mean_patch_seconds"] == 0.0
        assert s["speedup"] is None

    def test_empty_window(self):
        from codenib.web.commit_window import window_stats

        assert window_stats([]) is None

    def test_summary_carries_stats(self, tmp_path):
        _write_manifest(
            tmp_path,
            [
                _commit("b" * 40, "bbbbbbb", build_seconds=4.0),
                _commit("a" * 40, "aaaaaaa", method="cold", build_seconds=96.0),
            ],
        )
        s = CommitWindow(str(tmp_path)).summary()
        assert s["stats"]["speedup"] == 24.0

    def test_unavailable_summary_has_no_stats(self, tmp_path):
        assert "stats" not in CommitWindow(str(tmp_path)).summary()
