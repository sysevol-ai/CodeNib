# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from codenib.paths import (
    CODENIB_HOME_ENV,
    CODENIB_PREBUILT_DIR_ENV,
    CODENIB_RESULTS_DIR_ENV,
    CODENIB_TEMP_DIR_ENV,
    legacy_repo_index_dir,
    prebuilt_data_dir,
    repo_index_dir,
    repo_state_dir,
    repository_state_key,
    results_dir,
    temp_state_dir,
    user_state_dir,
)


def test_configured_roots_are_independent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    prebuilt = tmp_path / "indexes"
    results = tmp_path / "results"
    temporary = tmp_path / "work"
    monkeypatch.setenv(CODENIB_HOME_ENV, str(home))
    monkeypatch.setenv(CODENIB_PREBUILT_DIR_ENV, str(prebuilt))
    monkeypatch.setenv(CODENIB_RESULTS_DIR_ENV, str(results))
    monkeypatch.setenv(CODENIB_TEMP_DIR_ENV, str(temporary))

    assert user_state_dir() == home
    assert prebuilt_data_dir() == prebuilt
    assert results_dir() == results
    assert temp_state_dir() == temporary


def test_prebuilt_defaults_below_codenib_home(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(CODENIB_HOME_ENV, str(tmp_path))
    monkeypatch.delenv(CODENIB_PREBUILT_DIR_ENV, raising=False)

    assert prebuilt_data_dir() == tmp_path / "prebuilt"


def test_results_default_below_codenib_home(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(CODENIB_HOME_ENV, str(tmp_path))
    monkeypatch.delenv(CODENIB_RESULTS_DIR_ENV, raising=False)

    assert results_dir() == tmp_path / "results"


def test_former_home_variable_is_not_read(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(CODENIB_HOME_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODE" + "MINER_HOME", str(tmp_path / "former"))

    assert user_state_dir() == tmp_path / ".codenib"


def test_repo_index_dir_is_user_owned_and_repository_specific(
    monkeypatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    first = tmp_path / "work" / "project"
    second = tmp_path / "other" / "project"
    monkeypatch.setenv(CODENIB_HOME_ENV, str(home))

    assert repo_state_dir(first).parent == home / "repositories"
    assert repo_index_dir(first) == repo_state_dir(first) / "indexes"
    assert repo_index_dir(first) != repo_index_dir(second)
    assert repository_state_key(first).startswith("project-")


def test_legacy_repo_index_dir_remains_discoverable(tmp_path: Path) -> None:
    assert legacy_repo_index_dir(tmp_path) == tmp_path.resolve() / ".codenib_cache"
