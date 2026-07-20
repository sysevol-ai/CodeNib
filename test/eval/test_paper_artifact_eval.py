from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "experiments"
    / "artifacts"
    / "paper_artifact"
    / "artifact_eval.py"
)
SPEC = importlib.util.spec_from_file_location("paper_artifact_eval", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
artifact_eval = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = artifact_eval
SPEC.loader.exec_module(artifact_eval)


def test_verify_checksums_audits_inventory(tmp_path):
    payload = tmp_path / "payload.txt"
    payload.write_text("retained result\n", encoding="utf-8")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    (tmp_path / "SHA256SUMS").write_text(f"{digest}  payload.txt\n", encoding="utf-8")

    summary = artifact_eval.verify_checksums(tmp_path)

    assert summary == {"files": 1, "bytes": len(payload.read_bytes())}


def test_verify_checksums_rejects_path_escape(tmp_path):
    (tmp_path / "SHA256SUMS").write_text(f"{'0' * 64}  ../outside\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unsafe checksum path"):
        artifact_eval.verify_checksums(tmp_path)


def test_require_external_output_rejects_bundle_mutation(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        artifact_eval.require_external_output(tmp_path, tmp_path / "generated")

    artifact_eval.require_external_output(tmp_path, tmp_path.parent / "generated")


def test_run_command_exposes_figure_modules(tmp_path):
    figure = tmp_path / "figure"
    figure.mkdir()
    (figure / "artifact_probe.py").write_text("VALUE = 'visible'\n", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()

    result = artifact_eval.run_command(
        "probe",
        (sys.executable, "-c", "import artifact_probe; print(artifact_probe.VALUE)"),
        cwd=tmp_path,
        log_dir=logs,
    )

    assert result["log"] == "probe.log"
    assert (logs / "probe.log").read_text(encoding="utf-8") == "visible\n"
    assert os.environ.get("PYTHONPATH") != str(figure)
