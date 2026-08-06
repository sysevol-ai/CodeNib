# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Unit contracts for expensive CI selection and parser caching."""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.classify_ci_changes import classify_refs, classify_serial_changes

ROOT = Path(__file__).resolve().parents[1]


def _pyproject(
    version: str,
    dependency: str = "fastapi>=0.100.0",
    development_status: str = "3 - Alpha",
) -> bytes:
    return (
        "[project]\n"
        'name = "codenib"\n'
        f"version = {version!r}\n"
        f"dependencies = [{dependency!r}]\n"
        f'classifiers = ["Development Status :: {development_status}"]\n'
    ).encode()


def _lock(version: str, dependency: str = "fastapi") -> bytes:
    return (
        "version = 1\n"
        "[[package]]\n"
        'name = "codenib"\n'
        f"version = {version!r}\n"
        'source = { editable = "." }\n'
        f"dependencies = [{{ name = {dependency!r} }}]\n"
    ).encode()


def _classify(paths, before=None, after=None):
    before = before or {}
    after = after or {}
    return classify_serial_changes(
        paths,
        read_before=before.__getitem__,
        read_after=after.__getitem__,
    )


def test_docs_only_change_skips_serial_chain() -> None:
    decision = _classify(["README.md", "docs/releases/0.2.0.md"])

    assert decision.run_serial is False
    assert decision.reason == "no serial-chain paths changed"


def test_synchronized_project_version_update_skips_serial_chain() -> None:
    before = {
        "pyproject.toml": _pyproject("0.1.0"),
        "uv.lock": _lock("0.1.0"),
    }
    after = {
        "pyproject.toml": _pyproject("0.2.0", development_status="4 - Beta"),
        "uv.lock": _lock("0.2.0"),
    }

    decision = _classify(["CHANGELOG.md", "pyproject.toml", "uv.lock"], before, after)

    assert decision.run_serial is False
    assert "version metadata" in decision.reason


def test_dependency_change_with_version_bump_runs_serial_chain() -> None:
    before = {
        "pyproject.toml": _pyproject("0.1.0"),
        "uv.lock": _lock("0.1.0"),
    }
    after = {
        "pyproject.toml": _pyproject("0.2.0", "fastapi>=0.200.0"),
        "uv.lock": _lock("0.2.0", "fastapi-next"),
    }

    decision = _classify(["pyproject.toml", "uv.lock"], before, after)

    assert decision.run_serial is True
    assert "beyond the project version" in decision.reason


def test_unpaired_release_metadata_change_runs_serial_chain() -> None:
    decision = _classify(["pyproject.toml"])

    assert decision.run_serial is True
    assert "synchronized" in decision.reason


def test_graph_or_ci_inputs_run_serial_chain() -> None:
    for path in (
        "codenib/graph/code_graph.py",
        "core/CMakeLists.txt",
        ".github/actions/prewarm-parsers/action.yml",
        "scripts/classify_ci_changes.py",
    ):
        assert _classify([path]).run_serial is True


def test_serial_reason_escapes_control_characters_in_paths() -> None:
    decision = _classify(["codenib/graph/bad\nrun-serial=false.py"])

    assert decision.run_serial is True
    assert "\n" not in decision.reason
    assert r"\nrun-serial=false.py" in decision.reason


def test_renaming_serial_file_outside_allowlist_runs_serial_chain(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "ci-policy@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "CI Policy"], cwd=tmp_path, check=True
    )
    source = tmp_path / "codenib" / "graph" / "route.py"
    source.parent.mkdir(parents=True)
    source.write_text("GRAPH = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()

    target = tmp_path / "notes" / "route.py"
    target.parent.mkdir()
    source.rename(target)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "move"], cwd=tmp_path, check=True)
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()

    decision = classify_refs(tmp_path, base, head)

    assert decision.run_serial is True
    assert "codenib/graph/route.py" in decision.reason


def test_ci_workflow_reuses_a_versioned_bounded_parser_cache() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    action = (ROOT / ".github/actions/prewarm-parsers/action.yml").read_text(
        encoding="utf-8"
    )

    assert workflow.count("uses: ./.github/actions/prewarm-parsers") == 2
    assert "cold_parser_cache" in workflow
    assert "dorny/paths-filter" not in workflow
    assert "scripts/classify_ci_changes.py" in workflow
    assert "Set up classifier Python" in workflow
    assert 'python-version: "3.11"' in workflow
    assert 'RUN_SERIAL=false\n                REASON="manual light tier"' in workflow
    assert "Remove incomplete run-scoped parser cache" in workflow
    assert "Remove run-scoped parser cache" in workflow
    assert "tree-sitter-language-pack-${PARSER_VERSION}" in action
    assert 'XDG_ROOT="$CACHE_PARENT/xdg"' in action
    assert "XDG_CACHE_HOME" in action
    assert "timeout --foreground" in action
    assert "${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in action
    assert action.count("shell: bash -l {0}") == 2
    assert "cold-run-dir" in action
    assert "-mtime +7" in action


def test_ci_reuses_versioned_toolchains_and_serializes_consumers() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    action = (ROOT / ".github/actions/setup-env/action.yml").read_text(encoding="utf-8")

    assert (
        'SCIP_PYTHON_SHA="$(git -C third_party/scip-python rev-parse HEAD)"' in action
    )
    assert 'MARKER="$TOOLCHAIN_CACHE/scip-python.sha"' in action
    assert 'grep -qx "$SCIP_PYTHON_SHA" "$MARKER"' in action
    assert 'export CARGO_HOME="$TOOLCHAIN_CACHE/cargo"' in action
    assert 'export RUSTUP_HOME="$TOOLCHAIN_CACHE/rustup"' in action
    assert 'RUSTUP="$CARGO_HOME/bin/rustup"' in action
    assert "needs: [preflight, integration-serial, scip-core]" in workflow
    assert "needs.scip-core.result != 'cancelled'" in workflow
