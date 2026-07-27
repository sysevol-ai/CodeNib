#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Exercise installed sparse Ask through an OpenAI-compatible endpoint."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )


def _fixture_repository(root: Path) -> Path:
    repo = root / "agent-smoke-repository"
    repo.mkdir()
    (repo / "calculator.py").write_text(
        "def release_signature(left: int, right: int) -> int:\n"
        '    """Return the release verification sum."""\n'
        "    return left + right\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "# Agent smoke repository\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    _run(["git", "init", "--quiet"], cwd=repo, env=env)
    _run(
        ["git", "config", "user.email", "release-smoke@example.invalid"],
        cwd=repo,
        env=env,
    )
    _run(
        ["git", "config", "user.name", "CodeNib Release Smoke"],
        cwd=repo,
        env=env,
    )
    _run(["git", "add", "."], cwd=repo, env=env)
    _run(["git", "commit", "--quiet", "-m", "initial fixture"], cwd=repo, env=env)
    return repo


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request_json(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _wait_for_json(
    url: str,
    *,
    process: subprocess.Popen[Any],
    timeout: float,
) -> Any:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Wiki exited before {url} became ready "
                f"(status {process.returncode})"
            )
        try:
            return _request_json(url)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {url}: {last_error}")


def _stop_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        os.killpg(process.pid, signal.SIGINT)
    else:  # pragma: no cover - release CI currently runs on Linux
        process.terminate()
    try:
        process.wait(timeout=15)
        return
    except subprocess.TimeoutExpired:
        pass

    if os.name == "posix":
        os.killpg(process.pid, signal.SIGTERM)
    else:  # pragma: no cover
        process.terminate()
    process.wait(timeout=5)


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    """Two-turn endpoint: request BM25 once, then answer from its result."""

    requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        self.requests.append(request)
        messages = request.get("messages") or []
        has_tool_result = any(message.get("role") == "tool" for message in messages)

        if has_tool_result:
            message = {
                "role": "assistant",
                "content": (
                    "The repository defines `release_signature` in "
                    "`calculator.py`; it returns the sum of its two inputs."
                ),
            }
            finish_reason = "stop"
        else:
            names = {
                tool.get("function", {}).get("name")
                for tool in request.get("tools") or []
                if isinstance(tool, dict)
            }
            if "bm25_search" not in names:
                self.send_error(400, f"bm25_search missing from tools: {sorted(names)}")
                return
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_release_search",
                        "type": "function",
                        "function": {
                            "name": "bm25_search",
                            "arguments": json.dumps(
                                {"query": "release_signature", "top_k": 5}
                            ),
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"

        payload = {
            "id": f"chatcmpl-{len(self.requests)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.get("model", "fake-model"),
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _assert_chat_response(response: Any) -> None:
    if not isinstance(response, dict):
        raise RuntimeError(f"Ask returned a non-object response: {response!r}")
    if "returns the sum" not in response.get("answer", ""):
        raise RuntimeError(f"Ask returned an unexpected answer: {response!r}")
    if response.get("total_turns") != 2:
        raise RuntimeError(f"Ask did not complete in two turns: {response!r}")

    calls = response.get("tool_calls") or []
    if (
        len(calls) != 1
        or calls[0].get("skill_id") != "bm25_search"
        or calls[0].get("result_count", 0) < 1
        or calls[0].get("error") is not None
    ):
        raise RuntimeError(f"Ask did not execute grounded BM25 retrieval: {calls!r}")

    citations = response.get("citations") or []
    if (
        not citations
        or citations[0].get("file") != "calculator.py"
        or citations[0].get("start_line") != 1
    ):
        raise RuntimeError(f"Ask returned invalid source citations: {citations!r}")


def smoke(root: Path, *, executable: str = "codenib") -> None:
    root.mkdir(parents=True, exist_ok=True)
    repo = _fixture_repository(root)
    env = os.environ.copy()
    env["CODENIB_HOME"] = str(root / "user-state")
    env["CODENIB_RELEASE_SMOKE_KEY"] = "local-release-smoke"

    _FakeOpenAIHandler.requests = []
    model_server = ThreadingHTTPServer(
        ("127.0.0.1", _free_port()),
        _FakeOpenAIHandler,
    )
    model_thread = threading.Thread(target=model_server.serve_forever, daemon=True)
    model_thread.start()
    model_base = f"http://127.0.0.1:{model_server.server_port}/v1"

    try:
        _run(
            [
                executable,
                "doctor",
                "--require",
                "core",
                "--require",
                "wiki",
                "--require",
                "agent",
                "--model",
                "openai/fake-model",
                "--api-base",
                model_base,
                "--api-key-env",
                "CODENIB_RELEASE_SMOKE_KEY",
            ],
            cwd=root,
            env=env,
        )
        _run([executable, "index", str(repo)], cwd=root, env=env)
        status = _run(["git", "status", "--porcelain"], cwd=repo, env=env).stdout
        if status:
            raise RuntimeError(f"indexing modified the target repository: {status!r}")

        frontend_port = _free_port()
        api_port = _free_port()
        log_path = root / "agent-wiki.log"
        command = [
            executable,
            "wiki",
            str(repo),
            "--no-index",
            "--no-open",
            "--model",
            "openai/fake-model",
            "--api-base",
            model_base,
            "--api-key-env",
            "CODENIB_RELEASE_SMOKE_KEY",
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
                if len(repos) != 1 or not repos[0].get("capabilities", {}).get("chat"):
                    raise RuntimeError(f"installed Wiki did not expose Ask: {repos!r}")
                response = _request_json(
                    f"{api_base}/api/chat",
                    payload={
                        "repo_id": repos[0]["id"],
                        "messages": [
                            {
                                "role": "user",
                                "content": "What does release_signature do?",
                            }
                        ],
                    },
                    timeout=60,
                )
                _assert_chat_response(response)
                if len(_FakeOpenAIHandler.requests) != 2:
                    raise RuntimeError(
                        "Ask did not make exactly one tool-selection and one "
                        f"final-answer request: {len(_FakeOpenAIHandler.requests)}"
                    )
            except Exception:
                service_log.flush()
                details = log_path.read_text(encoding="utf-8", errors="replace")
                print(f"\n--- Agent Wiki log ---\n{details}", file=sys.stderr)
                print(
                    "\n--- Model requests ---\n"
                    + json.dumps(_FakeOpenAIHandler.requests, indent=2),
                    file=sys.stderr,
                )
                raise
            finally:
                _stop_process_group(process)
    finally:
        model_server.shutdown()
        model_server.server_close()
        model_thread.join(timeout=5)

    print("Installed sparse Ask service smoke passed")


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
            f"release agent smoke failed: command not found: {args.executable}",
            file=sys.stderr,
        )
        return 1

    context = (
        nullcontext(args.root.expanduser().resolve())
        if args.root is not None
        else tempfile.TemporaryDirectory(prefix="codenib-release-agent-")
    )
    try:
        with context as value:
            smoke(Path(value), executable=executable)
    except Exception as exc:
        print(f"release agent smoke failed: {exc}", file=sys.stderr)
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
