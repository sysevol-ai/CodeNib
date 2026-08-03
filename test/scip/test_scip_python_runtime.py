# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import json
import signal
from pathlib import Path

import pytest

from codenib.scip_interface import scip_indexer_base, scip_indexer_python
from codenib.scip_interface.scip_indexer_python import SCIPPythonIndexer
from codenib.scip_interface.scip_pb2 import Index


def test_checked_run_kills_process_group_when_caller_cancels(monkeypatch):
    class InterruptedProcess:
        pid = 1234
        args = ["scip-python", "index"]

        def __init__(self):
            self.communicate_calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            if timeout is not None:
                raise KeyboardInterrupt
            return "", ""

        def poll(self):
            return None

    process = InterruptedProcess()
    killed = []
    monkeypatch.setattr(
        scip_indexer_python.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(scip_indexer_python.os, "getpgid", lambda pid: pid + 1)
    monkeypatch.setattr(
        scip_indexer_python.os,
        "killpg",
        lambda pgid, sig: killed.append((pgid, sig)),
    )

    with pytest.raises(KeyboardInterrupt):
        scip_indexer_python._run_checked_with_timeout(
            process.args,
            timeout=30,
        )

    assert killed == [(1235, signal.SIGKILL)]
    assert process.communicate_calls == 2


def _captured_subprocess_env(monkeypatch, tmp_path, node_options=None):
    indexer = SCIPPythonIndexer(tmp_path, output_dir=tmp_path / "out")
    monkeypatch.setattr(indexer, "_get_conda_env_bin", lambda: "/scip/bin")
    if node_options is None:
        monkeypatch.delenv("NODE_OPTIONS", raising=False)
    else:
        monkeypatch.setenv("NODE_OPTIONS", node_options)

    captured = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(scip_indexer_python, "_run_checked_with_timeout", fake_run)
    assert indexer._run_in_conda_env(["scip-python", "index"], Path(tmp_path))
    return captured["env"]


def test_scip_python_raises_default_node_heap_for_cold_large_repos(
    monkeypatch, tmp_path
):
    env = _captured_subprocess_env(monkeypatch, tmp_path)

    assert env["NODE_OPTIONS"] == "--max-old-space-size=16384"


def test_scip_python_preserves_explicit_node_heap(monkeypatch, tmp_path):
    env = _captured_subprocess_env(
        monkeypatch,
        tmp_path,
        "--trace-gc --max-old-space-size=24576",
    )

    assert env["NODE_OPTIONS"] == "--trace-gc --max-old-space-size=24576"


def test_scip_python_prefers_an_existing_path_tool(monkeypatch, tmp_path):
    indexer = SCIPPythonIndexer(tmp_path, output_dir=tmp_path / "out")
    monkeypatch.setattr(
        scip_indexer_python.shutil,
        "which",
        lambda command: "/tools/scip-python" if command == "scip-python" else None,
    )
    monkeypatch.setattr(
        indexer,
        "_ensure_conda_env",
        lambda: (_ for _ in ()).throw(AssertionError("Conda should not be used")),
    )

    assert indexer._check_indexer_available() is True
    assert indexer._direct_indexer_path == "/tools/scip-python"
    assert indexer._build_index_command()[:2] == [
        "/tools/scip-python",
        "index",
    ]
    command = indexer._build_index_command()
    environment_path = Path(command[command.index("--environment") + 1])
    assert environment_path.read_text() == "[]\n"


def test_scip_python_direct_run_preserves_node_heap(monkeypatch, tmp_path):
    indexer = SCIPPythonIndexer(tmp_path, output_dir=tmp_path / "out")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)

    monkeypatch.setattr(scip_indexer_python, "_run_checked_with_timeout", fake_run)
    monkeypatch.delenv("NODE_OPTIONS", raising=False)

    assert indexer._run_direct(["/tools/scip-python", "index"], tmp_path)
    assert captured["cmd"][0] == "/tools/scip-python"
    assert captured["env"]["NODE_OPTIONS"] == "--max-old-space-size=16384"


def test_scip_python_index_timeout_can_be_extended_for_large_repos(
    monkeypatch, tmp_path
):
    indexer = SCIPPythonIndexer(tmp_path, output_dir=tmp_path / "out")
    captured = {}

    def fake_run(_cmd, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(scip_indexer_python, "_run_checked_with_timeout", fake_run)
    monkeypatch.setenv("CODENIB_SCIP_PYTHON_INDEX_TIMEOUT_SECONDS", "1800")

    assert indexer._run_direct(["/tools/scip-python", "index"], tmp_path)
    assert captured["timeout"] == 1800.0


@pytest.mark.parametrize("value", ["invalid", "0", "-1", "nan", "inf"])
def test_scip_python_invalid_index_timeout_uses_the_ci_default(
    monkeypatch, tmp_path, value
):
    indexer = SCIPPythonIndexer(tmp_path, output_dir=tmp_path / "out")
    captured = {}

    def fake_run(_cmd, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(scip_indexer_python, "_run_checked_with_timeout", fake_run)
    monkeypatch.setenv("CODENIB_SCIP_PYTHON_INDEX_TIMEOUT_SECONDS", value)

    assert indexer._run_direct(["/tools/scip-python", "index"], tmp_path)
    assert captured["timeout"] == 600.0


def test_scip_binary_decode_uses_packaged_protobuf_descriptor(monkeypatch, tmp_path):
    indexer = SCIPPythonIndexer(tmp_path, output_dir=tmp_path / "out")
    monkeypatch.setattr(
        scip_indexer_base.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("decode must not invoke protoc")
        ),
    )
    payload = Index()
    document = payload.documents.add()
    document.language = "python"
    document.relative_path = "calculator.py"
    occurrence = document.occurrences.add()
    occurrence.range.extend([0, 0, 0, 3])
    occurrence.symbol = "scip-python python repo 1.0 calculator/release_sum()."
    indexer.index_file.write_bytes(payload.SerializeToString())

    assert indexer.decode_index() is True
    decoded = indexer.decoded_file.read_text()
    assert 'relative_path: "calculator.py"' in decoded
    assert "calculator/release_sum()" in decoded


def test_scip_python_applies_temporary_excludes_without_dirtying_repo(tmp_path):
    indexer = SCIPPythonIndexer(
        tmp_path,
        output_dir=tmp_path / "out",
        exclude_patterns=["third_party/**", ".next/**"],
    )
    config_path = tmp_path / "pyrightconfig.json"

    with indexer._temporary_exclude_config():
        assert json.loads(config_path.read_text()) == {
            "exclude": [".next/**", "third_party/**"]
        }

    assert not config_path.exists()


def test_scip_python_preserves_project_owned_pyright_config(tmp_path):
    config_path = tmp_path / "pyrightconfig.json"
    original = '{"include": ["src"]}\n'
    config_path.write_text(original)
    indexer = SCIPPythonIndexer(
        tmp_path,
        output_dir=tmp_path / "out",
        exclude_patterns=["third_party/**"],
    )

    with indexer._temporary_exclude_config():
        assert config_path.read_text() == original

    assert config_path.read_text() == original
