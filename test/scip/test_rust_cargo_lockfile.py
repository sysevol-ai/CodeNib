# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""rust-analyzer's cargo keeps its lockfile in CodeNib's state, not the checkout."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

import codenib.graph.incremental.lsp_client as lsp_client
from codenib.paths import CODENIB_HOME_ENV, repo_state_dir
from codenib.scip_interface import scip_indexer_base
from codenib.scip_interface.rust_analyzer import (
    CARGO_LOCKFILE_ENV,
    redirect_cargo_lockfile,
    rust_toolchain,
)
from codenib.scip_interface.scip_indexer_rust import SCIPRustIndexer


@pytest.fixture()
def crate(tmp_path, monkeypatch):
    monkeypatch.setenv(CODENIB_HOME_ENV, str(tmp_path / "home"))
    monkeypatch.delenv(CARGO_LOCKFILE_ENV, raising=False)
    root = tmp_path / "crate"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8",
    )
    (root / "src" / "lib.rs").write_text("pub fn one() -> u32 { 1 }\n")
    return root


def test_lockfile_goes_to_state_when_the_crate_has_none(crate):
    env: dict[str, str] = {}

    redirect_cargo_lockfile(env, crate, "scip")

    lockfile = repo_state_dir(crate) / "cargo" / "scip" / "Cargo.lock"
    assert env[CARGO_LOCKFILE_ENV] == str(lockfile)
    assert lockfile.parent.is_dir() and not lockfile.exists()
    assert sorted(p.name for p in crate.iterdir()) == ["Cargo.toml", "src"]


def test_an_existing_lockfile_is_copied_and_left_alone(crate):
    own = crate / "Cargo.lock"
    own.write_text("# pinned by the project\nversion = 4\n", encoding="utf-8")
    before = own.stat()
    env: dict[str, str] = {}

    redirect_cargo_lockfile(env, crate, "scip")

    copied = repo_state_dir(crate) / "cargo" / "scip" / "Cargo.lock"
    assert env[CARGO_LOCKFILE_ENV] == str(copied)
    assert copied.read_bytes() == own.read_bytes()
    after = own.stat()
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)


def test_a_callers_own_setting_wins(crate):
    env = {CARGO_LOCKFILE_ENV: "/elsewhere/Cargo.lock"}

    redirect_cargo_lockfile(env, crate, "scip")

    assert env == {CARGO_LOCKFILE_ENV: "/elsewhere/Cargo.lock"}
    assert not (repo_state_dir(crate) / "cargo").exists()


def test_scip_generation_runs_rust_analyzer_with_the_state_lockfile(
    crate, tmp_path, monkeypatch
):
    indexer = SCIPRustIndexer(crate, output_dir=tmp_path / "index")
    monkeypatch.setattr(indexer, "_check_indexer_available", lambda: True)
    seen: dict[str, object] = {}

    def run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(scip_indexer_base.subprocess, "run", run)

    assert scip_indexer_base.SCIPIndexerBase.generate_index(indexer)
    env = seen["env"]
    assert env["RUSTUP_TOOLCHAIN"] == rust_toolchain()
    assert env[CARGO_LOCKFILE_ENV] == str(
        repo_state_dir(crate) / "cargo" / "scip" / "Cargo.lock"
    )
    assert seen["cwd"] == crate


def test_live_rust_analyzer_uses_its_own_state_lockfile(crate):
    env = lsp_client._lsp_process_env("rust", crate)

    assert env[CARGO_LOCKFILE_ENV] == str(
        repo_state_dir(crate) / "cargo" / "lsp" / "Cargo.lock"
    )
    assert CARGO_LOCKFILE_ENV not in lsp_client._lsp_process_env("rust")
    assert CARGO_LOCKFILE_ENV not in lsp_client._lsp_process_env("python", crate)


def _pinned_cargo_available() -> bool:
    if shutil.which("rustup") is None:
        return False
    probe = subprocess.run(
        ["rustup", "run", rust_toolchain(), "cargo", "--version"],
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


@pytest.mark.skipif(
    not _pinned_cargo_available(), reason="the pinned Rust toolchain is not installed"
)
def test_pinned_cargo_writes_the_lockfile_where_it_is_told(crate):
    """Guards against a cargo release that stops honouring the setting."""

    env = dict(os.environ)
    redirect_cargo_lockfile(env, crate, "scip")

    subprocess.run(
        ["rustup", "run", rust_toolchain(), "cargo", "metadata", "--format-version=1"],
        cwd=crate,
        env=env,
        check=True,
        capture_output=True,
        timeout=120,
    )

    assert not (crate / "Cargo.lock").exists()
    assert (repo_state_dir(crate) / "cargo" / "scip" / "Cargo.lock").is_file()
