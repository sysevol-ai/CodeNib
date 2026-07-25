# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from codenib.paths import (
    CODENIB_HOME_ENV,
    CODENIB_PREBUILT_DIR_ENV,
    CODENIB_RESULTS_DIR_ENV,
    CODENIB_TEMP_DIR_ENV,
    prebuilt_data_dir,
    repo_index_dir,
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


def test_repo_index_dir_is_repository_relative(tmp_path: Path) -> None:
    assert repo_index_dir(tmp_path) == tmp_path.resolve() / ".codenib_cache"
