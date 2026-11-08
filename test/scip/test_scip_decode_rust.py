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


def _target_index(prefixes):
    """A demo crate's SCIP index; ``prefixes`` maps a target to its descriptor."""

    from codenib.scip_interface.scip_pb2 import Index

    index = Index()
    rows = {
        "src/lib.rs": [
            ("lib", "demo", "shapes/Thing#", [2, 11, 16], [2, 0, 5, 1], 1),
            ("lib", "demo", "shapes/impl#[Thing]area().", [6, 7, 11], [6, 4, 8, 5], 1),
        ],
        "src/main.rs": [
            ("bin", "demo", "main().", [0, 3, 7], [0, 0, 3, 1], 1),
            ("lib", "demo", "shapes/Thing#", [1, 4, 9], [], 0),
        ],
        "tests/integration.rs": [
            ("test", "demo", "helper().", [0, 3, 9], [0, 0, 2, 1], 1),
        ],
        "build.rs": [
            ("custom-build", "demo", "main().", [0, 3, 7], [0, 0, 1, 1], 1),
        ],
        "macros/src/lib.rs": [
            ("proc-macro", "demo-macros", "derive_shape!", [3, 9, 21], [3, 0, 6, 1], 1),
        ],
    }
    for path, occurrences in rows.items():
        document = index.documents.add(relative_path=path)
        for target, crate, descriptor, rng, enclosing, roles in occurrences:
            occurrence = document.occurrences.add(
                symbol=f"rust-analyzer cargo {crate} 0.1.0 {prefixes[target]}{descriptor}",
                symbol_roles=roles,
            )
            occurrence.range.extend(rng)
            occurrence.enclosing_range.extend(enclosing)
    return index


NEW_TARGETS = {
    "lib": "lib/",
    "bin": "bin/demo/",
    "test": "test/integration/",
    "custom-build": "custom-build/",
    "proc-macro": "proc-macro/",
}


def _names(tmp_path, index):
    from codenib.scip_interface.scip_indexer_base import index_text

    decoded = tmp_path / "index.decoded"
    decoded.write_text(index_text(index), encoding="utf-8")
    graph = SCIPRustGraphDecoder(str(decoded), project_root=str(tmp_path)).decode()
    return {
        v["name"]: v.attributes().get("unified_name")
        for v in graph.graph.vs
        if v.attributes().get("type") not in ("file", "directory", "root")
    }


def test_target_monikers_keep_library_names(tmp_path):
    """rust-analyzer's Cargo-target descriptors leave library names unchanged."""

    old = _names(tmp_path, _target_index(dict.fromkeys(NEW_TARGETS, "")))
    new = _names(tmp_path, _target_index(NEW_TARGETS))
    for name in ("demo/shapes/Thing", "demo/shapes/impl#[Thing]area"):
        assert new[name] == old[name]
    assert new["demo-macros/derive_shape!"] == old["demo-macros/derive_shape!"]
    # The build script's main() keeps its old name; named targets get their own.
    assert "demo/main" in new
    assert new["demo/bin/demo/main"] == "src/main.rs:main()"
    assert new["demo/test/integration/helper"] == "tests/integration.rs:helper()"
    assert set(old) - set(new) == {"demo/helper"}
