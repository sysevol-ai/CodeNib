from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_ROOT = REPO_ROOT / "artifact"
SCRIPT = ARTIFACT_ROOT / "artifact_eval.py"
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


def test_release_manifest_has_no_external_figure_root():
    manifest = json.loads(
        (ARTIFACT_ROOT / "bundle-manifest.json").read_text(encoding="utf-8")
    )

    roots = {source["root"] for source in manifest["sources"]}
    assert roots == {"code", "data", "base_dataset", "synthesis_dataset"}
    figure_source = next(
        source for source in manifest["sources"] if source["id"] == "figure-programs"
    )
    assert figure_source["path"] == "artifact/figure"
    assert figure_source["root"] == "code"


def test_reviewer_sources_have_no_machine_local_paths():
    reproducer = artifact_eval.load_reproducer(ARTIFACT_ROOT)
    reviewer_files = [
        ARTIFACT_ROOT / "artifact_eval.py",
        ARTIFACT_ROOT / "paper_artifact_config.json",
        *(ARTIFACT_ROOT / "figure" / name for name in reproducer.SOURCE_FILES),
    ]

    for path in reviewer_files:
        text = path.read_text(encoding="utf-8")
        assert "/mnt/" not in text, path
        assert "/home/" not in text, path


def test_reviewer_entrypoints_are_well_formed():
    runner = ARTIFACT_ROOT / "run.sh"
    assert os.access(runner, os.X_OK)
    subprocess.run(["bash", "-n", str(runner)], check=True)

    dockerfile = (ARTIFACT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "figure/requirements-paper.txt" in dockerfile
    assert 'ENTRYPOINT ["python", "artifact_eval.py"]' in dockerfile
