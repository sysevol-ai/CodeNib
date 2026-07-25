# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Launch the local CodeNib backend and Wiki frontend."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

from ..paths import user_state_dir
from .local import LocalWiki

_MIN_NODE_VERSION = (18, 18, 0)


def _packaged_frontend_dir() -> Path:
    return Path(__file__).resolve().parent / "frontend"


def find_frontend_dir(explicit: str | os.PathLike[str] | None = None) -> Optional[Path]:
    """Locate a source or packaged CodeNib frontend."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("CODENIB_WEB_FRONTEND_DIR"):
        candidates.append(Path(os.environ["CODENIB_WEB_FRONTEND_DIR"]))
    candidates.append(Path(__file__).resolve().parents[2] / "web")
    candidates.append(_packaged_frontend_dir())

    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if (resolved / "package.json").is_file():
            return resolved
    return None


def _frontend_digest(source: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        if any(part in {".next", "node_modules"} for part in path.parts):
            continue
        digest.update(str(path.relative_to(source)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def materialize_frontend(source: Path) -> Path:
    """Copy wheel-owned frontend files to writable user state."""
    packaged = _packaged_frontend_dir().resolve()
    source = source.resolve()
    if source != packaged:
        return source

    digest = _frontend_digest(source)
    target = user_state_dir() / "frontend" / digest[:12]
    marker = target / ".codenib-frontend-digest"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == digest:
        return target

    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns(".next", "node_modules"),
    )
    marker.write_text(digest + "\n", encoding="utf-8")
    return target


def _wait_for_http(
    url: str,
    process: subprocess.Popen,
    *,
    timeout_seconds: float = 90.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"process exited before becoming ready: {url} "
                f"(status {process.returncode})"
            )
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status < 500:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for {url}")


def _stop(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _loopback_host(host: str) -> str:
    return "127.0.0.1" if host in {"0.0.0.0", "::"} else host


def node_runtime_status() -> tuple[bool, str]:
    """Return whether the installed Node.js can run the Wiki frontend."""
    node = shutil.which("node")
    if node is None:
        return False, "missing (requires >= 18.18.0)"
    try:
        result = subprocess.run(
            [node, "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return False, f"unusable ({exc})"

    raw_version = result.stdout.strip()
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", raw_version)
    if match is None:
        return False, f"unrecognized version: {raw_version or 'empty output'}"
    version = tuple(int(part) for part in match.groups())
    if version < _MIN_NODE_VERSION:
        return False, f"{raw_version} (requires >= 18.18.0)"
    return True, raw_version


def _ensure_frontend_dependencies(frontend_dir: Path, *, install: bool) -> None:
    next_cli = frontend_dir / "node_modules" / "next" / "dist" / "bin" / "next"
    if next_cli.is_file():
        return
    if not install:
        raise RuntimeError(
            f"frontend dependencies are missing under {frontend_dir}; "
            "rerun without --no-install-frontend"
        )
    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError("npm is required to install the Wiki frontend")
    print(f"Installing Wiki frontend dependencies in {frontend_dir} ...")
    try:
        subprocess.run([npm, "ci"], cwd=frontend_dir, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"failed to install Wiki frontend dependencies in {frontend_dir}"
        ) from exc


def launch_local_wiki(
    local: LocalWiki,
    *,
    frontend_dir: str | os.PathLike[str] | None,
    api_host: str,
    api_port: int,
    frontend_host: str,
    frontend_port: int,
    install_frontend: bool,
    open_browser: bool,
) -> int:
    """Run backend and frontend until either exits or the user interrupts."""
    frontend = find_frontend_dir(frontend_dir)
    if frontend is None:
        raise RuntimeError(
            "CodeNib Wiki frontend was not found. Set --frontend-dir or "
            "CODENIB_WEB_FRONTEND_DIR to a checkout containing web/package.json."
        )
    frontend = materialize_frontend(frontend)
    node_ok, node_detail = node_runtime_status()
    npm = shutil.which("npm")
    if not node_ok:
        raise RuntimeError(f"Node.js is unavailable: {node_detail}")
    if npm is None:
        raise RuntimeError("npm is required for the Wiki frontend")
    _ensure_frontend_dependencies(frontend, install=install_frontend)

    env = os.environ.copy()
    env["CODENIB_DEMO_CONFIG"] = str(local.config_path)
    env["CODENIB_API_BASE"] = f"http://{_loopback_host(api_host)}:{api_port}"
    env.pop("NEXT_PUBLIC_API_BASE", None)

    backend: subprocess.Popen | None = None
    frontend_process: subprocess.Popen | None = None
    try:
        backend = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "codenib.web.app:app",
                "--host",
                api_host,
                "--port",
                str(api_port),
            ],
            env=env,
        )
        _wait_for_http(
            f"http://{_loopback_host(api_host)}:{api_port}/api/health",
            backend,
        )

        frontend_process = subprocess.Popen(
            [
                npm,
                "run",
                "dev",
                "--",
                "--hostname",
                frontend_host,
                "--port",
                str(frontend_port),
            ],
            cwd=frontend,
            env=env,
        )
        public_url = f"http://{_loopback_host(frontend_host)}:{frontend_port}"
        _wait_for_http(public_url, frontend_process)
        repo_url = f"{public_url}/{local.repo_id}"
        print(f"\nCodeNib Wiki is ready: {repo_url}")
        print("Press Ctrl-C to stop.")
        if open_browser:
            webbrowser.open(repo_url)

        while True:
            backend_status = backend.poll()
            frontend_status = frontend_process.poll()
            if backend_status is not None:
                return backend_status or 1
            if frontend_status is not None:
                return frontend_status or 1
            time.sleep(0.5)
    finally:
        _stop(frontend_process)
        _stop(backend)


__all__ = [
    "find_frontend_dir",
    "launch_local_wiki",
    "materialize_frontend",
    "node_runtime_status",
]
