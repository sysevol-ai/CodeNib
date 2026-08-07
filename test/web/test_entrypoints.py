# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for build-manifest entry-point discovery."""

import json

from codenib.web.entrypoints import discover_entry_points, entry_point_files


def _write(root, relative, content=""):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_package_json_prefers_source_over_the_bundle(tmp_path):
    """preact's `main` is `dist/preact.mjs`, which is not in the symbol graph;
    its `source` is `src/index.js`, which is."""
    _write(tmp_path, "src/index.js", "export function render() {}\n")
    _write(tmp_path, "dist/preact.mjs", "// bundle\n")
    _write(
        tmp_path,
        "package.json",
        json.dumps(
            {
                "name": "preact",
                "main": "dist/preact.mjs",
                "module": "dist/preact.mjs",
                "source": "src/index.js",
            }
        ),
    )

    entries = discover_entry_points(str(tmp_path))

    assert [(e.path, e.kind, e.label) for e in entries] == [
        ("src/index.js", "package", "preact")
    ]


def test_export_subpaths_remap_declarations_to_their_implementation(tmp_path):
    """preact's `./compat` export names only `compat/src/index.d.ts`; the code
    a map should show is `compat/src/index.js` beside it."""
    _write(tmp_path, "src/index.js")
    _write(tmp_path, "compat/src/index.d.ts", "export declare function memo(): void;")
    _write(tmp_path, "compat/src/index.js", "export function memo() {}")
    _write(
        tmp_path,
        "package.json",
        json.dumps(
            {
                "name": "preact",
                "source": "src/index.js",
                "exports": {
                    ".": {"types": "./src/index.d.ts", "default": "./dist/preact.mjs"},
                    "./compat": {
                        "types": "./compat/src/index.d.ts",
                        "default": "./compat/dist/compat.mjs",
                    },
                },
            }
        ),
    )

    by_label = {e.label: e.path for e in discover_entry_points(str(tmp_path))}

    assert by_label["preact"] == "src/index.js"
    assert by_label["preact/compat"] == "compat/src/index.js"


def test_manifest_entries_that_are_not_code_are_dropped(tmp_path):
    """babel's workspaces export `"./package.json": "./package.json"` — a real
    file, but not something to put on a code map."""
    _write(tmp_path, "lib/index.js")
    _write(
        tmp_path,
        "package.json",
        json.dumps(
            {
                "name": "thing",
                "exports": {
                    ".": "./lib/index.js",
                    "./package.json": "./package.json",
                    "./readme": "./README.md",
                },
            }
        ),
    )

    assert entry_point_files(str(tmp_path)) == {"lib/index.js"}


def test_pyproject_scripts_and_the_package_itself(tmp_path):
    # The distribution name is dashed; the import name is not.
    _write(tmp_path, "my_pkg/__init__.py")
    _write(tmp_path, "my_pkg/cli.py", "def main(): ...")
    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname = "my-pkg"\n[project.scripts]\nmycli = "my_pkg.cli:main"\n',
    )

    entries = [(e.path, e.kind, e.label) for e in discover_entry_points(str(tmp_path))]

    assert ("my_pkg/cli.py", "script", "mycli") in entries
    # A library declares no scripts, so the package itself has to count.
    assert ("my_pkg/__init__.py", "package", "my-pkg") in entries


def test_pyproject_package_is_found_under_src_and_lib_layouts(tmp_path):
    """requests lives in `src/requests/`; matplotlib lives in `lib/matplotlib/`."""
    _write(tmp_path, "lib/plotting/__init__.py")
    _write(tmp_path, "pyproject.toml", '[project]\nname = "plotting"\n')

    assert entry_point_files(str(tmp_path)) == {"lib/plotting/__init__.py"}


def test_cargo_conventional_targets_and_workspace_members(tmp_path):
    _write(tmp_path, "src/lib.rs")
    _write(tmp_path, "crates/alpha/src/lib.rs")
    _write(tmp_path, "crates/beta/src/main.rs")
    _write(
        tmp_path,
        "Cargo.toml",
        '[package]\nname = "root"\n[workspace]\nmembers = ["crates/*"]\n',
    )

    entries = {e.path: (e.kind, e.label) for e in discover_entry_points(str(tmp_path))}

    assert entries["src/lib.rs"] == ("library", "root")
    assert entries["crates/alpha/src/lib.rs"] == ("library", "alpha")
    assert entries["crates/beta/src/main.rs"] == ("binary", "beta")


def test_go_module_binaries(tmp_path):
    _write(tmp_path, "go.mod", "module example.com/thing\n")
    _write(tmp_path, "cmd/prometheus/main.go", "package main\n")
    _write(tmp_path, "cmd/promtool/main.go", "package main\n")

    entries = discover_entry_points(str(tmp_path))

    assert [(e.path, e.label) for e in entries] == [
        ("cmd/prometheus/main.go", "prometheus"),
        ("cmd/promtool/main.go", "promtool"),
    ]


def test_npm_workspaces_are_walked_when_the_root_exports_nothing(tmp_path):
    _write(tmp_path, "packages/core/src/index.js")
    _write(
        tmp_path,
        "packages/core/package.json",
        json.dumps({"name": "@thing/core", "source": "src/index.js"}),
    )
    _write(
        tmp_path,
        "package.json",
        json.dumps(
            {"name": "thing-monorepo", "private": True, "workspaces": ["packages/*"]}
        ),
    )

    entries = discover_entry_points(str(tmp_path))

    assert [(e.path, e.label) for e in entries] == [
        ("packages/core/src/index.js", "@thing/core")
    ]


def test_npm_workspace_manifest_symlink_cannot_be_read_outside_repo(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _write(repo, "packages/core/src/index.js")
    external_manifest = _write(
        outside,
        "package.json",
        json.dumps({"name": "outside", "source": "src/index.js"}),
    )
    (repo / "packages/core/package.json").symlink_to(external_manifest)
    _write(
        repo,
        "package.json",
        json.dumps({"private": True, "workspaces": ["packages/*"]}),
    )

    loaded = []
    load = json.load

    def record_load(handle):
        loaded.append(handle.name)
        return load(handle)

    monkeypatch.setattr(json, "load", record_load)

    assert discover_entry_points(str(repo)) == []
    assert str(repo / "packages/core/package.json") not in loaded


def test_cargo_conventional_target_symlink_cannot_escape_repo(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _write(repo, "Cargo.toml", '[package]\nname = "root"\n')
    _write(repo, "src/lib.rs", "pub fn safe() {}\n")
    external_main = _write(outside, "main.rs", "fn main() {}\n")
    (repo / "src/main.rs").symlink_to(external_main)

    assert entry_point_files(str(repo)) == {"src/lib.rs"}


def test_cargo_workspace_target_symlink_cannot_escape_repo(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _write(repo, "crates/safe/src/lib.rs", "pub fn safe() {}\n")
    external_main = _write(outside, "main.rs", "fn main() {}\n")
    escaped_target = repo / "crates/escaped/src/main.rs"
    escaped_target.parent.mkdir(parents=True)
    escaped_target.symlink_to(external_main)
    _write(repo, "Cargo.toml", '[workspace]\nmembers = ["crates/*"]\n')

    assert entry_point_files(str(repo)) == {"crates/safe/src/lib.rs"}


def test_go_target_symlinks_cannot_escape_repo(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _write(repo, "go.mod", "module example.com/thing\n")
    _write(repo, "cmd/safe/main.go", "package main\n")
    external_root = _write(outside, "root-main.go", "package main\n")
    external_cmd = _write(outside, "tool-main.go", "package main\n")
    (repo / "main.go").symlink_to(external_root)
    escaped_cmd = repo / "cmd/escaped/main.go"
    escaped_cmd.parent.mkdir()
    escaped_cmd.symlink_to(external_cmd)

    assert entry_point_files(str(repo)) == {"cmd/safe/main.go"}


def test_go_cmd_directory_symlink_cannot_escape_repo(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _write(repo, "go.mod", "module example.com/thing\n")
    _write(outside, "cmd/tool/main.go", "package main\n")
    (repo / "cmd").symlink_to(outside / "cmd", target_is_directory=True)

    assert discover_entry_points(str(repo)) == []


def test_package_manifest_paths_cannot_escape_the_repository(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _write(repo, "nested/.keep")
    _write(outside, "secret.js", "export const secret = true;\n")
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    _write(
        repo,
        "package.json",
        json.dumps(
            {
                "name": "unsafe",
                "source": "nested/../../outside/secret.js",
                "exports": {"./linked": "./linked/secret.js"},
            }
        ),
    )

    assert discover_entry_points(str(repo)) == []


def test_workspace_members_cannot_escape_the_repository(tmp_path):
    npm_repo = tmp_path / "npm-repo"
    cargo_repo = tmp_path / "cargo-repo"
    outside = tmp_path / "outside"
    _write(npm_repo, "nested/.keep")
    _write(cargo_repo, "nested/.keep")
    _write(outside, "src/index.js", "export const secret = true;\n")
    _write(outside, "src/lib.rs", "pub fn secret() {}\n")
    _write(
        outside,
        "package.json",
        json.dumps({"name": "outside", "source": "src/index.js"}),
    )
    _write(
        npm_repo,
        "package.json",
        json.dumps(
            {
                "name": "unsafe-workspace",
                "private": True,
                "workspaces": ["nested/../../outside"],
            }
        ),
    )
    _write(
        cargo_repo,
        "Cargo.toml",
        '[workspace]\nmembers = ["nested/../../outside"]\n',
    )

    assert discover_entry_points(str(npm_repo)) == []
    assert discover_entry_points(str(cargo_repo)) == []


def test_a_broken_or_absent_manifest_yields_nothing(tmp_path):
    assert discover_entry_points(str(tmp_path)) == []
    assert discover_entry_points(None) == []
    assert discover_entry_points(str(tmp_path / "nope")) == []

    _write(tmp_path, "package.json", "{ this is not json")
    _write(tmp_path, "pyproject.toml", "[project\nname = broken")
    assert discover_entry_points(str(tmp_path)) == []
