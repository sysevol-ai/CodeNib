#!/usr/bin/env python3
"""Run integrity and retained-result evaluation for the paper artifact."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

sys.dont_write_bytecode = True

FIGURE_TESTS = (
    "figure/test_build_paper_claims.py",
    "figure/test_draw_maintenance_lifecycle.py",
    "figure/test_draw_pareto_rerank.py",
    "figure/test_draw_lsp_replay.py",
    "figure/test_reproduce_paper_figures.py",
    "figure/test_verify_paper_figures.py",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_checksums(artifact_root: Path) -> dict[str, int]:
    inventory = artifact_root / "SHA256SUMS"
    if not inventory.is_file():
        raise FileNotFoundError(f"missing checksum inventory: {inventory}")

    files = 0
    total_bytes = 0
    for line_number, line in enumerate(
        inventory.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line:
            continue
        try:
            expected, relative = line.split("  ", 1)
        except ValueError as exc:
            raise ValueError(f"invalid SHA256SUMS line {line_number}") from exc
        if len(expected) != 64:
            raise ValueError(f"invalid digest on SHA256SUMS line {line_number}")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"unsafe checksum path: {relative}")
        path = artifact_root / relative_path
        if not path.is_file():
            raise FileNotFoundError(f"missing artifact payload: {relative}")
        actual = sha256_file(path)
        if actual != expected:
            raise ValueError(
                f"checksum mismatch for {relative}: expected {expected}, got {actual}"
            )
        files += 1
        total_bytes += path.stat().st_size
    if files == 0:
        raise ValueError("SHA256SUMS contains no payload files")
    return {"files": files, "bytes": total_bytes}


def load_reproducer(artifact_root: Path):
    path = artifact_root / "figure" / "reproduce_paper_figures.py"
    if not path.is_file():
        raise FileNotFoundError(f"missing figure reproducer: {path}")
    spec = importlib.util.spec_from_file_location("artifact_figure_reproducer", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load figure reproducer: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def validate_inputs(artifact_root: Path) -> dict[str, int]:
    config = artifact_root / "paper_artifact_config.json"
    _, inputs = load_reproducer(artifact_root).load_config(config)
    path_count = sum(
        len(value) if isinstance(value, list) else 1 for value in inputs.values()
    )
    expected = artifact_root / "verification" / "expected"
    if not expected.is_dir():
        raise FileNotFoundError(f"missing expected outputs: {expected}")
    return {
        "input_groups": len(inputs),
        "input_paths": path_count,
        "expected_files": sum(path.is_file() for path in expected.iterdir()),
    }


def run_command(
    name: str,
    command: Sequence[str],
    *,
    cwd: Path,
    log_dir: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    environment = os.environ.copy()
    python_path = [str(cwd / "figure")]
    if environment.get("PYTHONPATH"):
        python_path.append(environment["PYTHONPATH"])
    environment.update(
        {
            "MPLBACKEND": "Agg",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONPATH": os.pathsep.join(python_path),
        }
    )
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path = log_dir / f"{name}.log"
    log_path.write_text(completed.stdout, encoding="utf-8")
    print(completed.stdout, end="")
    if completed.returncode:
        raise subprocess.CalledProcessError(completed.returncode, command)
    return {
        "command": list(command),
        "duration_seconds": time.monotonic() - started,
        "log": log_path.name,
    }


def require_external_output(artifact_root: Path, output_dir: Path) -> None:
    try:
        output_dir.relative_to(artifact_root)
    except ValueError:
        return
    raise ValueError("--output-dir must be outside the checksum-verified artifact")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("smoke", "full"))
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="extracted artifact root (defaults to this script's directory)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="external output directory; required for full mode",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    artifact_root = args.artifact_root.resolve()
    checksum_summary = verify_checksums(artifact_root)
    input_summary = validate_inputs(artifact_root)
    print(
        "PASS artifact smoke "
        f"files={checksum_summary['files']} inputs={input_summary['input_paths']}"
    )
    if args.mode == "smoke":
        return 0
    if args.output_dir is None:
        raise ValueError("--output-dir is required for full mode")

    output_dir = args.output_dir.resolve()
    require_external_output(artifact_root, output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    figure_output = output_dir / "figures"
    stages = {}
    stages["figure_tests"] = run_command(
        "figure_tests",
        (sys.executable, "-m", "unittest", *FIGURE_TESTS),
        cwd=artifact_root,
        log_dir=output_dir,
    )
    stages["reproduce"] = run_command(
        "reproduce",
        (
            sys.executable,
            "figure/reproduce_paper_figures.py",
            "--config",
            "paper_artifact_config.json",
            "--output-dir",
            str(figure_output),
        ),
        cwd=artifact_root,
        log_dir=output_dir,
    )
    stages["verify"] = run_command(
        "verify",
        (
            sys.executable,
            "figure/verify_paper_figures.py",
            "--expected-dir",
            "verification/expected",
            "--actual-dir",
            str(figure_output),
        ),
        cwd=artifact_root,
        log_dir=output_dir,
    )
    report = {
        "schema_version": 1,
        "status": "passed",
        "integrity": checksum_summary,
        "inputs": input_summary,
        "stages": stages,
    }
    report_path = output_dir / "artifact_eval_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"PASS artifact full evaluation report={report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
