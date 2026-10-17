# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import os
import select
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from codenib.code_chunking.base import CodeChunk
from codenib.graph.code_graph import CodeGraph
from codenib.types import NODE_TYPE_FUNCTION
from scripts import benchmark_context_serving as cohort


def test_sampling_is_order_independent_and_never_reads_annotations():
    cases = [
        SimpleNamespace(
            instance_id=f"{lang}-{repo}-{i}", language=lang, repo=f"org/{repo}"
        )
        for lang in ("python", "java", "c")
        for repo in ("one", "two", "three")
        for i in range(8)
    ]
    excluded = [cases[0].instance_id]
    first = cohort.sample_cases(cases, 30, "frozen", excluded)
    second = cohort.sample_cases(list(reversed(cases)), 30, "frozen", excluded)
    assert [c.instance_id for c in first] == [c.instance_id for c in second]
    assert len({c.instance_id for c in first}) == 30
    assert not set(excluded) & {c.instance_id for c in first}
    assert {c.language for c in first} == {"python", "java", "c"}
    assert {c.repo for c in first} == {"org/one", "org/two", "org/three"}
    with pytest.raises(ValueError, match="Sample must fit"):
        cohort.sample_cases(cases, 14, "frozen")


def test_source_gap_coverage_restores_headers_constants_and_tests_without_gold():
    sources = {
        "code.py": [
            "# header\n",
            "VALUE = 1\n",
            "\n",
            "def f():\n",
            "    return VALUE\n",
        ],
        "tests/test_code.py": ["assert f() == 1\n"],
    }
    chunk = CodeChunk("SYNTHETIC", 3, 4, NODE_TYPE_FUNCTION, "f", "code.py", "f")
    nodes, omitted = cohort.complete_nodes([chunk], sources, max_chars=40)
    assert not omitted
    lines = {}
    for node in nodes:
        assert len(node.content) <= 40
        for line, text in enumerate(
            node.content.splitlines(keepends=True), node.start_line
        ):
            assert text == sources[node.file][line]
            assert (node.file, line) not in lines
            lines[node.file, line] = text
    assert len(lines) == 6
    assert "SYNTHETIC" not in "".join(lines.values())


def test_oversized_line_is_reported_without_hiding_later_source():
    source = {"code.py": ["x" * 41 + "\n", "VALUE = 1\n"]}
    nodes, omitted = cohort.complete_nodes([], source, max_chars=40)
    assert omitted == [["code.py", 1]]
    assert [(n.start_line, n.end_line, n.content) for n in nodes] == [
        (1, 1, "VALUE = 1\n")
    ]


@pytest.mark.parametrize("preparation", [False, True])
def test_graph_timeout_reaps_its_process_before_deleting_private_copy(
    tmp_path, monkeypatch, preparation
):
    events = []
    launches = []

    class Process:
        pid = 24680
        returncode = None

        def terminate(self):
            events.append(("terminate", self.pid))

        def wait(self, timeout=None):
            events.append(("wait", timeout))
            if timeout == 3:
                raise subprocess.TimeoutExpired("graph", timeout)
            self.returncode = 124

    monkeypatch.setattr(cohort.subprocess, "run", lambda *a, **k: None)

    def launch(command, **kwargs):
        launches.append(command)
        assert kwargs["start_new_session"]
        assert ("--allow-project-preparation" in command) is preparation
        private = Path(command[command.index("--repo") + 1])
        assert private != tmp_path and private.parent.exists()
        return Process()

    monkeypatch.setattr(cohort.subprocess, "Popen", launch)
    monkeypatch.setattr(
        cohort.os, "killpg", lambda pid, sig: events.append(("kill", pid))
    )
    output = tmp_path / "build"
    options = {"allow_project_preparation": True} if preparation else {}
    receipt = cohort.bounded_graph(tmp_path, "python", output, 3, **options)
    assert not receipt["available"]
    assert receipt["allow_project_preparation"] is preparation
    assert "exceeded" in receipt["error"]
    assert events == [
        ("wait", 3),
        ("terminate", 24680),
        ("wait", 10),
        ("kill", 24680),
        ("wait", None),
    ]
    private = Path(launches[0][launches[0].index("--repo") + 1])
    assert not private.parent.exists()
    assert CodeGraph.load_graph(output / "graph.pkl").graph.vcount() == 0


def test_preparation_failure_is_retained_without_billing(tmp_path):
    (tmp_path / "sonnet").mkdir()
    case = SimpleNamespace(instance_id="case", original_inst_id="original")
    row = cohort.serve_case(
        case,
        {"status": "error", "error": "clone failed"},
        tmp_path,
        tmp_path,
        "sonnet",
        None,
        0.02,
    )
    assert row["status"] == "error"
    assert row["routes"] == {}
    assert row["decision_cost_usd"] == 0
    assert json.loads((tmp_path / "sonnet/case.json").read_text()) == row


def source_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "source.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "source.py"], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "source",
        ],
        cwd=path,
        check=True,
    )


@pytest.mark.parametrize("preparation", [False, True])
@pytest.mark.parametrize("language", ["python", "ts", "go", "rust", "java", "cpp"])
def test_graph_worker_uses_portable_route_arguments(
    tmp_path, monkeypatch, language, preparation
):
    from codenib import ls_router

    source_repo(tmp_path)

    def build(
        repo,
        output,
        *,
        languages,
        project_name,
        allow_partial,
        allow_project_preparation,
    ):
        # Native backends already decode references; forwarding a Python-only
        # generation keyword to TypeScript, Rust or clangd raises TypeError.
        assert languages == [language]
        assert allow_project_preparation is preparation
        if preparation:
            (repo / "compile_commands.json").write_text("[]\n")
        return SimpleNamespace(
            graph=CodeGraph(), failed_languages={}, index_generation_reports={}
        )

    monkeypatch.setattr(ls_router, "build_graph_for_languages_with_report", build)
    cohort.graph_worker(
        SimpleNamespace(
            repo=tmp_path,
            output=tmp_path,
            language=language,
            allow_project_preparation=preparation,
        )
    )
    assert (tmp_path / "graph.pkl").is_file()
    assert (tmp_path / "source.py").read_text() == "VALUE = 1\n"


@pytest.mark.parametrize("change", ["worktree", "head"])
def test_graph_preparation_rejects_changed_source_before_publication(
    tmp_path, monkeypatch, change
):
    from codenib import ls_router

    source_repo(tmp_path)

    def build(repo, output, **kwargs):
        if change == "worktree":
            (repo / "source.py").write_text("VALUE = 2\n")
        else:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "--allow-empty",
                    "-qm",
                    "changed head",
                ],
                cwd=repo,
                check=True,
            )
        return SimpleNamespace(graph=CodeGraph())

    monkeypatch.setattr(ls_router, "build_graph_for_languages_with_report", build)
    with pytest.raises(ValueError, match="changed tracked source"):
        cohort.graph_worker(
            SimpleNamespace(
                repo=tmp_path,
                output=tmp_path,
                language="cpp",
                allow_project_preparation=True,
            )
        )
    assert not (tmp_path / "graph-status.json").exists()
    assert not (tmp_path / "graph.pkl").exists()


def test_timeout_cancellation_cleans_scips_nested_session():
    script = """
import signal, sys
from scripts.benchmark_context_serving import cancel_graph_worker
from codenib.scip_interface.scip_indexer_python import _run_checked_with_timeout
signal.signal(signal.SIGTERM, cancel_graph_worker)
_run_checked_with_timeout([sys.executable, "-c",
    "import os,time; print(os.getpid(), flush=True); time.sleep(120)"], timeout=60)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert select.select([process.stdout], [], [], 15)[0], "child did not start"
        child_pid = int(process.stdout.readline())
        cohort.stop_graph_worker(process)
        assert process.returncode == 124
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if process.poll() is None:
            cohort.stop_graph_worker(process)
        process.stdout.close()
        process.stderr.close()
