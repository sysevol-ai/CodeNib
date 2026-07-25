# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for SCIPRustGraphDecoder workspace-member handling."""

from codenib.scip_interface.scip_decode_rust import SCIPRustGraphDecoder


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _decoder(tmp_path):
    return SCIPRustGraphDecoder("unused.scip", project_root=tmp_path)


def test_workspace_members_resolve_globs(tmp_path):
    _write(
        tmp_path / "Cargo.toml",
        '[package]\nname = "rootpkg"\n\n[workspace]\nmembers = ["crates/*"]\n',
    )
    _write(tmp_path / "crates" / "a" / "Cargo.toml", '[package]\nname = "crate-a"\n')

    assert _decoder(tmp_path)._load_workspace_crates() == {"rootpkg", "crate-a"}


def test_workspace_members_tolerate_malformed_globs(tmp_path):
    """uutils/coreutils-style member lists must not abort the whole decode.

    A trailing-slash pattern makes ``Path.glob`` raise ``IndexError`` on
    Python 3.12; empty and absolute patterns are equally unusable.
    """
    _write(
        tmp_path / "Cargo.toml",
        '[package]\nname = "rootpkg"\n\n'
        '[workspace]\nmembers = ["", "crates/", "/abs/path", "crates/*"]\n',
    )
    _write(tmp_path / "crates" / "a" / "Cargo.toml", '[package]\nname = "crate-a"\n')

    assert _decoder(tmp_path)._load_workspace_crates() == {"rootpkg", "crate-a"}
