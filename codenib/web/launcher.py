# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Launch the local CodeNib backend and Wiki frontend."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from .annotations import TOKEN_ENV as ANNOTATION_TOKEN_ENV
from .local import LocalWiki
from .ports import validate_local_wiki_ports

_NODE_VERSION_REQUIREMENT = "^20.19.0 or >=22.12.0"


def _packaged_frontend_dir() -> Path:
    return Path(__file__).resolve().parent / "frontend"


def find_frontend_dir(explicit: str | os.PathLike[str] | None = None) -> Optional[Path]:
    """Locate a source checkout or prebuilt CodeNib frontend."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("CODENIB_WEB_FRONTEND_DIR"):
        candidates.append(Path(os.environ["CODENIB_WEB_FRONTEND_DIR"]))
    candidates.append(Path(__file__).resolve().parents[2] / "web")
    candidates.append(_packaged_frontend_dir())

    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if (resolved / "index.html").is_file() or (resolved / "package.json").is_file():
            return resolved
    return None


def is_prebuilt_frontend(frontend_dir: Path) -> bool:
    return (frontend_dir / "index.html").is_file() and not (
        frontend_dir / "package.json"
    ).is_file()


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
        return False, f"missing (requires {_NODE_VERSION_REQUIREMENT})"
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
    if not ((version[0] == 20 and version >= (20, 19, 0)) or version >= (22, 12, 0)):
        return False, f"{raw_version} (requires {_NODE_VERSION_REQUIREMENT})"
    return True, raw_version


def _ensure_frontend_dependencies(frontend_dir: Path, *, install: bool) -> None:
    vite_cli = frontend_dir / "node_modules" / "vite" / "bin" / "vite.js"
    if vite_cli.is_file():
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


def _page_order(tree: list) -> list[str]:
    """Overview first, then top-level pages, then the pages under them."""

    roots = [page for page in tree if isinstance(page, dict) and page.get("id")]
    ids = [str(page["id"]) for page in roots]
    for page in roots:
        ids.extend(
            str(child["id"])
            for child in page.get("children") or []
            if isinstance(child, dict) and child.get("id")
        )
    ordered = ["overview"] if "overview" in ids else []
    return ordered + [page for page in dict.fromkeys(ids) if page != "overview"]


def prewarm_workers() -> int:
    """How many pages a local agent writes at once (``CODENIB_PREWARM_WORKERS``).

    Each page is several model calls in a row, so one page at a time left a
    Wiki of 17 pages half written for over ten minutes. Four keeps a coding
    agent's plan within its usual concurrency; 1 to 8 may be set.
    """

    try:
        value = int(os.environ.get("CODENIB_PREWARM_WORKERS", "") or 4)
    except ValueError:
        value = 4
    return max(1, min(value, 8))


def prewarm_pages(api_base: str, repo_id: str, *, workers: int | None = None) -> tuple:
    """Ask the API for every page so each is written before it is opened.

    Returns ``(written, total)``. A page that fails is left for the reader to
    open, which tries again.
    """

    base = f"{api_base}/api/repos/{urllib.parse.quote(repo_id, safe='')}/wiki"

    def get(url: str, timeout: float) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError):
            return False

    try:
        with urllib.request.urlopen(base, timeout=900) as response:
            tree = json.loads(response.read().decode("utf-8")).get("pages") or []
    except (OSError, ValueError, urllib.error.URLError):
        return (0, 0)
    pages = _page_order(tree)
    if not pages:
        return (0, 0)
    written = 1 if get(f"{base}/{pages[0]}?materialize_media=false", 900) else 0
    with ThreadPoolExecutor(max_workers=workers or prewarm_workers()) as pool:
        results = pool.map(
            lambda page: get(
                f"{base}/{urllib.parse.quote(page, safe='')}?materialize_media=false",
                900,
            ),
            pages[1:],
        )
        written += sum(1 for ok in results if ok)
    return (written, len(pages))


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
    frontend_port, api_port = validate_local_wiki_ports(
        frontend_port=frontend_port,
        api_port=api_port,
    )
    frontend = find_frontend_dir(frontend_dir)
    if frontend is None:
        raise RuntimeError(
            "CodeNib Wiki frontend was not found. Set --frontend-dir or "
            "CODENIB_WEB_FRONTEND_DIR to a prebuilt frontend or source checkout."
        )
    frontend = frontend.resolve()
    prebuilt_frontend = is_prebuilt_frontend(frontend)
    npm = None
    if not prebuilt_frontend:
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
    env.update(local.runtime_env)
    # Annotations write into the checkout and start the person's agent, so
    # they are on only for a loopback API, behind a token the browser gets
    # from the URL printed below (see ``codenib/web/annotations.py``).
    session = ""
    if api_host in ("127.0.0.1", "localhost", "::1"):
        session = secrets.token_urlsafe(24)
    backend_env = dict(env)
    if session:
        backend_env[ANNOTATION_TOKEN_ENV] = session
    env.pop(ANNOTATION_TOKEN_ENV, None)
    # The frontend opens on the repositories served here, not the hosted
    # landing; the source frontend's dev server reads this variable.
    env["CODENIB_LOCAL_WIKI"] = "1"

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
            env=backend_env,
        )
        _wait_for_http(
            f"http://{_loopback_host(api_host)}:{api_port}/api/health",
            backend,
        )

        if prebuilt_frontend:
            frontend_process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "codenib.web.static_server",
                    "--directory",
                    str(frontend),
                    "--api-base",
                    env["CODENIB_API_BASE"],
                    "--host",
                    frontend_host,
                    "--port",
                    str(frontend_port),
                    "--local",
                ],
                env=env,
            )
        else:
            frontend_process = subprocess.Popen(
                [
                    npm,
                    "run",
                    "dev",
                    "--",
                    "--host",
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
        if session:
            repo_url += f"?session={session}"
        # Flushed: run in the background (as the Claude Code plugin does), the
        # output is a file, and this link carries the annotation session key.
        print(f"\nCodeNib Wiki is ready: {repo_url}", flush=True)
        if session:
            print(
                "Select words on a page to ask your coding agent about them, "
                "request a change, or leave a note.",
                flush=True,
            )
        print("Press Ctrl-C to stop.", flush=True)
        if local.prewarm:

            def warm() -> None:
                written, total = prewarm_pages(
                    f"http://{_loopback_host(api_host)}:{api_port}", local.repo_id
                )
                if total:
                    print(f"Wrote {written} of {total} Wiki pages.", flush=True)

            print(
                "Writing the Overview first, then the other pages, in the background.",
                flush=True,
            )
            threading.Thread(
                target=warm, name="codenib-wiki-prewarm", daemon=True
            ).start()
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
    "is_prebuilt_frontend",
    "launch_local_wiki",
    "node_runtime_status",
]
