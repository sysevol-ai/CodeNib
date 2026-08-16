# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from codenib.scip_interface.rust_analyzer import rust_analyzer_command


def test_explicit_rust_analyzer_executable_wins_over_rustup(monkeypatch):
    monkeypatch.setenv("CODENIB_RUST_ANALYZER", "/opt/rust/bin/rust-analyzer")
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/rustup")

    assert rust_analyzer_command("scip", "/repo") == [
        "/opt/rust/bin/rust-analyzer",
        "scip",
        "/repo",
    ]


def test_rust_analyzer_uses_selected_rustup_toolchain(monkeypatch):
    monkeypatch.delenv("CODENIB_RUST_ANALYZER", raising=False)
    monkeypatch.setenv("CODENIB_RUST_TOOLCHAIN", "stable")
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/rustup")

    assert rust_analyzer_command("--version") == [
        "rustup",
        "run",
        "stable",
        "rust-analyzer",
        "--version",
    ]
