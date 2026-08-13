# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Unit contracts for expensive CI selection and parser caching."""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

import yaml

from scripts.classify_ci_changes import classify_refs, classify_serial_changes

ROOT = Path(__file__).resolve().parents[1]
_ACTION_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")


def _workflow(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def _workflow_triggers(document: dict):
    # PyYAML follows YAML 1.1 and parses the unquoted key `on` as True.
    return document.get("on", document.get(True))


def test_external_github_actions_are_pinned_to_full_commit_shas() -> None:
    offenders: list[str] = []
    step_groups: list[tuple[Path, list[dict]]] = []

    for path in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in document.get("jobs", {}).values():
            step_groups.append((path, job.get("steps", [])))

    for path in sorted((ROOT / ".github" / "actions").rglob("action.y*ml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        step_groups.append((path, document.get("runs", {}).get("steps", [])))

    for path, steps in step_groups:
        for step in steps:
            reference = str(step.get("uses", ""))
            if not reference:
                continue
            if reference.startswith(("./", "docker://")):
                continue
            if "@" not in reference:
                offenders.append(f"{path.relative_to(ROOT)}: {reference}")
                continue
            revision = reference.rsplit("@", 1)[1]
            if _ACTION_SHA_RE.fullmatch(revision) is None:
                offenders.append(f"{path.relative_to(ROOT)}: {reference}")

    assert offenders == []


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
        ".github/workflows/ci-full.yml",
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
    workflow = (ROOT / ".github/workflows/ci-full.yml").read_text(encoding="utf-8")
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
    workflow = (ROOT / ".github/workflows/ci-full.yml").read_text(encoding="utf-8")
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


def test_pull_request_workflows_use_only_ephemeral_runners() -> None:
    workflow_dir = ROOT / ".github/workflows"
    offenders: list[str] = []

    for path in workflow_dir.glob("*.yml"):
        document = _workflow(str(path.relative_to(ROOT)))
        triggers = _workflow_triggers(document)
        if not isinstance(triggers, dict) or "pull_request" not in triggers:
            continue
        for name, job in document.get("jobs", {}).items():
            if "self-hosted" in str(job.get("runs-on", "")):
                offenders.append(f"{path.name}:{name}")

    assert offenders == []

    # release-verify is reusable and is called by release.yml on pull requests.
    release_verify = (ROOT / ".github/workflows/release-verify.yml").read_text(
        encoding="utf-8"
    )
    assert "runs-on: self-hosted" not in release_verify

    auto_label = _workflow(".github/workflows/auto-label.yml")
    assert "pull_request_target" in _workflow_triggers(auto_label)
    assert all(
        "actions/checkout" not in str(step.get("uses", ""))
        for step in auto_label["jobs"]["label"]["steps"]
    )


def test_trusted_full_ci_is_separate_from_pull_request_ci() -> None:
    pull_request_ci = _workflow(".github/workflows/ci.yml")
    full_ci = _workflow(".github/workflows/ci-full.yml")

    assert "pull_request" in _workflow_triggers(pull_request_ci)
    assert "pull_request" not in _workflow_triggers(full_ci)
    assert set(pull_request_ci["jobs"]) == {"unit"}
    assert pull_request_ci["jobs"]["unit"]["runs-on"] == "ubuntu-latest"
    assert full_ci["jobs"]["preflight"]["runs-on"] == "ubuntu-latest"
    for name in (
        "unit",
        "integration",
        "integration-serial",
        "scip-core",
        "graph-consumer",
        "slow",
    ):
        assert full_ci["jobs"][name]["runs-on"] == "self-hosted"


def test_full_ci_enforces_the_maintained_native_core_gate() -> None:
    workflow = _workflow(".github/workflows/ci-full.yml")
    steps = workflow["jobs"]["scip-core"]["steps"]
    dependency_step = next(
        step for step in steps if step.get("name") == "Install C++ build deps"
    )
    gate_step = next(
        step for step in steps if step.get("name") == "Run maintained core gate"
    )
    dependency_run = str(dependency_step["run"])
    gate_run = str(gate_step["run"])
    build_step = next(
        step for step in steps if step.get("name") == "Build core/ (pybind)"
    )
    build_run = str(build_step["run"])
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    core_build_target = makefile.split("\ncore-build:", 1)[1].split("\n\n", 1)[0]
    core_target = makefile.split("\ncore-test:", 1)[1].split("\n\n", 1)[0]

    assert "pkg-config --exists zlib" in dependency_run
    assert "zlib1g-dev" in dependency_run
    core_packages = next(
        line
        for line in makefile.splitlines()
        if line.startswith("CORE_SYSTEM_PACKAGES")
    )
    assert "zlib1g-dev" in core_packages
    assert "make core-test" in gate_run
    assert "pytest test/scip/test_scip_core.py" not in gate_run
    assert "LD_PRELOAD" in gate_run
    assert "decode_clangd_fact_query_index" in core_target
    assert "clangd_fact_query_snapshot" in core_target
    for target in (build_run, core_build_target):
        assert "-DCODENIB_BUILD_TREE_SITTER_POC=OFF" in target
        assert "-DCODENIB_BUILD_PYTHON_CHUNK_BATCH_POC=OFF" in target
    for api in (
        "extract_python_chunk_spans",
        "python_chunk_poc_contract",
        "extract_python_repository_chunk_spans",
        "python_chunk_batch_poc_contract",
    ):
        assert api in core_target
    for command in (
        "./build/core/scip_decode_test",
        "./build/core/graph_layers_test",
        "./build/core/fact_batch_buffer_test",
        "./build/core/content_digest_test",
        "./build/core/fact_query_index_test",
        "test/ls_index/test_clangd_fact_query.py",
    ):
        assert command in core_target


def test_python_chunk_poc_builds_are_mutually_isolated() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    old_build = makefile.split("\ncore-chunk-poc-build:", 1)[1].split("\n\n", 1)[0]
    batch_build = makefile.split("\ncore-chunk-batch-poc-build:", 1)[1].split(
        "\n\n", 1
    )[0]
    batch_test = makefile.split("\ncore-chunk-batch-poc-test:", 1)[1].split("\n\n", 1)[
        0
    ]
    batch_gate = makefile.split("\ncore-chunk-batch-poc-gate:", 1)[1].split("\n\n", 1)[
        0
    ]

    assert "PYTHON_CHUNK_BATCH_POC_BUILD_DIR ?= build/core-chunk-batch-poc" in makefile
    assert "-DCODENIB_BUILD_TREE_SITTER_POC=ON" in old_build
    assert "-DCODENIB_BUILD_PYTHON_CHUNK_BATCH_POC=OFF" in old_build
    assert "PYTHON_CHUNK_POC_BUILD_DIR must be distinct" in old_build
    assert "-DCODENIB_BUILD_TREE_SITTER_POC=OFF" in batch_build
    assert "-DCODENIB_BUILD_PYTHON_CHUNK_BATCH_POC=ON" in batch_build
    assert "PYTHON_CHUNK_BATCH_POC_BUILD_DIR must be distinct" in batch_build
    assert "codenib_core_py python_chunk_batch_poc_test" in batch_build
    assert '"$(PYTHON_CHUNK_BATCH_POC_BUILD_DIR)/python_chunk_batch_poc_test"' in (
        batch_test
    )
    assert "origin.is_relative_to(expected)" in batch_test
    for api in (
        "extract_python_repository_chunk_spans",
        "python_chunk_batch_poc_contract",
        "extract_python_chunk_spans",
        "python_chunk_poc_contract",
    ):
        assert api in batch_test
    assert "callable(getattr(core, name, None))" in batch_test
    assert "test/chunker/test_python_repository_batch_poc.py" in batch_test
    assert "test/scripts/test_profile_python_repository_chunk_gate.py" in batch_test
    assert "python-repository-chunk-gate" in batch_gate
    assert (
        'CODENIB_CHUNK_GATE_BUILD_DIR="$(abspath '
        '$(PYTHON_CHUNK_BATCH_POC_BUILD_DIR))"'
    ) in batch_gate


def test_clangd_workload_gate_pins_matrix_subjects_and_json_artifact() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("\nclangd-workload-gate:", 1)[1].split("\n\n", 1)[0]
    manifest = json.loads(
        (ROOT / "scripts/profiling/clangd_workload_subjects.json").read_text(
            encoding="utf-8"
        )
    )

    assert "test_symbol_results_errors_counts_and_relations_match_graph" in target
    assert "profile_clangd_workload_gate.py" in target
    assert '--output-json "$(CLANGD_WORKLOAD_GATE_OUTPUT)"' in target
    assert '--subject-manifest "$(CLANGD_WORKLOAD_GATE_SUBJECT_MANIFEST)"' in target
    assert manifest["schema_version"] == 1
    assert len(manifest["subjects"]) == 3
    assert all(
        re.fullmatch(r"[0-9a-f]{40}", subject["revision"])
        for subject in manifest["subjects"]
    )
    categories = {
        category
        for subject in manifest["subjects"]
        for category in subject["categories"]
    }
    assert {
        "template-heavy",
        "macro-heavy",
        "header-heavy",
        "multi-target",
    } <= categories


def test_draft_ci_defers_hosted_unit_tests_until_review() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "ready_for_review" in workflow
    assert "github.event.pull_request.draft" in workflow
    assert "contains(github.event.pull_request.labels.*.name, 'full-ci')" in workflow


def test_pull_request_ci_control_labels_are_synced() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    labels = yaml.safe_load((ROOT / ".github/labels.yml").read_text(encoding="utf-8"))
    configured = {str(label["name"]) for label in labels}
    referenced = set(
        re.findall(r"pull_request\.labels\.\*\.name,\s*'([^']+)'", workflow)
    )

    assert referenced == {"full-ci", "skip-tests"}
    assert referenced <= configured


def test_default_make_target_excludes_external_and_billed_tiers() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    unit_expression = (
        'pytest -m "not slow and not integration and not integration_serial '
        'and not integration_serial_consumer" -x --tb=short'
    )
    assert f"test:\n\t{unit_expression}" in makefile
    assert 'test-slow:\n\tpytest -m "slow" --tb=short' in makefile
    assert "test-all:\n\tpytest" in makefile


def test_slow_ci_requires_and_exports_explicit_vertex_credentials() -> None:
    workflow_path = ROOT / ".github/workflows/ci-full.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    document = _workflow(str(workflow_path.relative_to(ROOT)))
    setup_step = next(
        step
        for step in document["jobs"]["slow"]["steps"]
        if step.get("name") == "Setup environment"
    )

    assert "GOOGLE_APPLICATION_CREDENTIALS_JSON is required" in workflow
    assert 'python -m json.tool "$CREDENTIALS_PATH" >/dev/null' in workflow
    assert (
        'echo "GOOGLE_APPLICATION_CREDENTIALS=$CREDENTIALS_PATH" >> "$GITHUB_ENV"'
        in workflow
    )
    assert setup_step["with"]["project-extras"] == "test,vertex"
    assert (
        document["jobs"]["slow"]["env"]["VERTEXAI_LOCATION"]
        == "${{ vars.VERTEXAI_LOCATION || 'us-east5' }}"
    )


def test_shared_ci_setup_validates_configurable_project_extras() -> None:
    action = _workflow(".github/actions/setup-env/action.yml")
    install_step = next(
        step
        for step in action["runs"]["steps"]
        if step.get("name") == "Install dependencies"
    )
    install_run = str(install_step["run"])

    assert action["inputs"]["project-extras"]["default"] == "test"
    assert (
        install_step["env"]["CODENIB_CI_PROJECT_EXTRAS"]
        == "${{ inputs.project-extras }}"
    )
    assert "^[A-Za-z0-9_-]+(,[A-Za-z0-9_-]+)*$" in install_run
    assert 'pip install -e ".[${CODENIB_CI_PROJECT_EXTRAS}]"' in install_run


def test_slow_embedding_cache_rebuild_is_narrowly_revision_scoped() -> None:
    source = (ROOT / "test/agent/test_agent_embedding_search.py").read_text(
        encoding="utf-8"
    )

    assert "except ValueError as exc:" in source
    assert '"Vector config embedding revision mismatch" not in str(exc)' in source
    assert "force_rebuild=True" in source


def test_live_provider_tests_do_not_hide_runtime_failures() -> None:
    provider_tests = (
        "test_vertex_ai.py",
        "test_agent_embedding_search.py",
        "test_bm25_search_agent.py",
        "test_query_e2e.py",
    )
    violations: list[str] = []

    for filename in provider_tests:
        path = ROOT / "test" / "agent" / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function in (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        ):
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Return)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, bool)
                ):
                    violations.append(
                        f"{filename}:{node.lineno} returns a boolean from a test"
                    )
                if not isinstance(node, ast.ExceptHandler):
                    continue
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and isinstance(child.func.value, ast.Name)
                        and child.func.value.id == "pytest"
                        and child.func.attr == "skip"
                    ):
                        violations.append(
                            f"{filename}:{child.lineno} skips a runtime exception"
                        )

    assert not violations, "\n".join(violations)
