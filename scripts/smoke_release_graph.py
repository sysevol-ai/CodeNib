#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Exercise installed graph setup, indexing, and Dependency Map serving."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
import urllib.parse
from contextlib import nullcontext
from pathlib import Path
from typing import Sequence

from smoke_release_services import _free_port, _run, _stop_process_group, _wait_for_json


def _fixture_repository(root: Path) -> Path:
    repo = root / "graph-smoke-repository"
    repo.mkdir()
    (repo / "calculator.py").write_text(
        "def release_sum(left: int, right: int) -> int:\n"
        '    """Return the release verification sum."""\n'
        "    return left + right\n\n\n"
        "def release_workflow() -> int:\n"
        '    """Exercise a source-linked call edge."""\n'
        "    return release_sum(20, 22)\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "# Graph smoke repository\n\n"
        "`release_workflow` delegates arithmetic to `release_sum`.\n",
        encoding="utf-8",
    )
    _run(["git", "init", "--quiet"], cwd=repo)
    _run(["git", "config", "user.email", "release-smoke@example.invalid"], cwd=repo)
    _run(["git", "config", "user.name", "CodeNib Release Smoke"], cwd=repo)
    _run(["git", "add", "."], cwd=repo)
    _run(["git", "commit", "--quiet", "-m", "initial fixture"], cwd=repo)
    return repo


def _assert_codemap(
    root: Path,
    repo: Path,
    *,
    executable: str,
    env: dict[str, str],
) -> None:
    frontend_port = _free_port()
    api_port = _free_port()
    log_path = root / "graph-wiki.log"
    command = [
        executable,
        "wiki",
        str(repo),
        "--no-index",
        "--no-open",
        "--port",
        str(frontend_port),
        "--api-port",
        str(api_port),
    ]
    print("+", " ".join(command), flush=True)

    with log_path.open("w", encoding="utf-8") as service_log:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdout=service_log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            api_base = f"http://127.0.0.1:{api_port}"
            repos = _wait_for_json(
                f"{api_base}/api/repos",
                process=process,
                timeout=120,
            )
            if len(repos) != 1:
                raise RuntimeError(f"unexpected repository list: {repos!r}")
            info = repos[0]
            if not info.get("capabilities", {}).get("codemap"):
                raise RuntimeError(f"Dependency Map capability is absent: {info!r}")

            repo_id = urllib.parse.quote(info["id"])
            query = urllib.parse.urlencode(
                {
                    "symbol": "release_workflow",
                    "direction": "callees",
                    "depth": 1,
                    "max_nodes": 12,
                }
            )
            codemap = _wait_for_json(
                f"{api_base}/api/repos/{repo_id}/codemap?{query}",
                process=process,
                timeout=60,
            )
            if not codemap.get("available"):
                raise RuntimeError(f"Dependency Map is unavailable: {codemap!r}")

            names = {node.get("name", "") for node in codemap.get("nodes", [])}
            if not any("release_workflow" in name for name in names):
                raise RuntimeError(f"caller symbol is missing: {codemap!r}")
            if not any("release_sum" in name for name in names):
                raise RuntimeError(f"callee symbol is missing: {codemap!r}")

            edges = codemap.get("edges") or []
            if not edges:
                raise RuntimeError(f"Dependency Map has no call edge: {codemap!r}")
            anchors = [
                anchor
                for edge in edges
                for anchor in edge.get("anchors") or []
                if anchor.get("file") == "calculator.py"
                and isinstance(anchor.get("line"), int)
            ]
            if not anchors:
                raise RuntimeError(
                    f"Dependency Map edge has no source anchor: {edges!r}"
                )
        except Exception:
            service_log.flush()
            details = log_path.read_text(encoding="utf-8", errors="replace")
            print(f"\n--- Graph Wiki log ---\n{details}", file=sys.stderr)
            raise
        finally:
            _stop_process_group(process)


def smoke(root: Path, *, executable: str = "codenib") -> None:
    root.mkdir(parents=True, exist_ok=True)
    repo = _fixture_repository(root)
    env = os.environ.copy()
    env["CODENIB_HOME"] = str(root / "user-state")

    doctor = _run(
        [
            executable,
            "doctor",
            str(repo),
            "--require",
            "graph",
            "--language",
            "python",
        ],
        cwd=root,
        env=env,
    )
    if "Python (python): scip" not in doctor.stdout:
        raise RuntimeError(
            "repository-aware graph provider was not reported:\n" + doctor.stdout
        )

    _run(
        [
            executable,
            "index",
            str(repo),
            "--preset",
            "graph",
            "--language",
            "python",
        ],
        cwd=root,
        env=env,
    )
    status = _run(["git", "status", "--porcelain"], cwd=repo, env=env).stdout
    if status:
        raise RuntimeError(f"graph indexing modified the repository: {status!r}")

    _assert_codemap(root, repo, executable=executable, env=env)
    print("Installed graph and Dependency Map smoke passed")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--executable", default="codenib")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    executable = shutil.which(args.executable)
    if executable is None:
        print(
            f"release graph smoke failed: command not found: {args.executable}",
            file=sys.stderr,
        )
        return 1

    context = (
        nullcontext(args.root.expanduser().resolve())
        if args.root is not None
        else tempfile.TemporaryDirectory(prefix="codenib-release-graph-")
    )
    try:
        with context as value:
            smoke(Path(value), executable=executable)
    except Exception as exc:
        print(f"release graph smoke failed: {exc}", file=sys.stderr)
        traceback.print_exception(exc)
        if isinstance(exc, subprocess.CalledProcessError):
            if exc.stdout:
                print(exc.stdout, file=sys.stderr)
            if exc.stderr:
                print(exc.stderr, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
