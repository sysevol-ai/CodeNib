#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Exercise an installed lightweight grep/MCP package without paid calls.

Run with Python's -I flag so the source checkout cannot shadow the wheel. Parser
download is allowed during preparation; the actual query's HTTP is replaced
with deterministic OpenRouter responses. Optional native vault acceptance uses
one unique, fake credential and always cleans up that entry.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from unittest.mock import patch


def _native_keyring() -> str:
    from codenib import openrouter_auth as auth

    # Never read, replace or remove the user's normal CodeNib credential entry.
    service = "codenib/install-smoke/" + secrets.token_hex(16)
    fake_key = "install-smoke-" + secrets.token_hex(16)
    with (
        patch.object(auth, "_SERVICE", service),
        patch.object(auth, "_ACCOUNT", "fixture"),
    ):
        auth.prepare_store("keyring")
        backend = auth._os_keyring()
        assert backend is not None
        try:
            auth.save_key(fake_key, store="keyring")
            assert auth.credential() == (fake_key, "keyring")
        finally:
            auth.forget_key(store="keyring")
        assert auth.credential() == (None, "none")
        return type(backend).__module__


def _query(root: Path) -> dict:
    import requests

    from codenib.agent.runtime.grep_jev import GrepJevConfig
    from codenib.mcp.grep_jev import explore_repository

    repository = root / "repository"
    repository.mkdir()
    (repository / "service.py").write_bytes(
        b"def retry_request():\n    return 'retry network request'\n\n"
        b"def unrelated():\n    return 'other behavior'\n"
    )
    calls = []

    def send(_session, request, **kwargs):
        assert request.headers["Authorization"] == "Bearer install-smoke-fixture"
        assert kwargs["allow_redirects"] is False
        payload = json.loads(request.body)
        calls.append(request.url)
        if request.url == "https://openrouter.ai/api/v1/chat/completions":
            body = {
                "model": "anthropic/claude-sonnet-4.6",
                "usage": {"cost": 0.001},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "actions": [
                                        {
                                            "pattern": "retry",
                                            "glob": "**/*.py",
                                            "case_sensitive": True,
                                        }
                                    ]
                                }
                            )
                        },
                    }
                ],
            }
        else:
            assert request.url == "https://openrouter.ai/api/alpha/decisions"
            body = {
                "model": "typesafe/jev-1.13-20260917",
                "usage": {"cost": 0.001},
                "answers": {
                    name: {
                        "type": "score",
                        "score": 3,
                        "confidence": 1,
                        "probabilities": {"0": 0, "1": 0, "2": 0, "3": 1},
                        "legend": {
                            str(i): value
                            for i, value in enumerate(question["criteria"])
                        },
                    }
                    for name, question in payload["questions"].items()
                },
            }
        response = requests.Response()
        response.url = request.url
        response.status_code = 200
        response._content = json.dumps(body).encode()
        response._content_consumed = True
        return response

    with (
        patch.object(requests.Session, "send", send),
        patch.object(
            socket.socket, "connect", side_effect=AssertionError("unexpected network")
        ),
    ):
        result = explore_repository(
            repository,
            GrepJevConfig(api_key="install-smoke-fixture"),
            "Where is retry handled?",
        )
    assert len(calls) == 2
    assert result["source"]["verified"] is True
    assert result["source"]["commit_verified"] is False
    assert result["files"][0]["file"] == "service.py"
    context = result["files"][0]["contexts"][0]
    assert context["start_line"] == 1
    assert "def retry_request" in context["content"]
    assert not (repository / ".codenib").exists()
    assert "install-smoke-fixture" not in json.dumps(result)
    return {"source_verified": True, "fixture_provider_calls": len(calls)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-keyring", action="store_true")
    args = parser.parse_args()

    from tree_sitter_language_pack import get_language

    import codenib
    from codenib.agent.runtime.grep_jev import resolve_ripgrep

    installed = Path(distribution("codenib").locate_file("codenib")).resolve()
    assert Path(codenib.__file__).resolve().parent == installed
    for name in ("torch", "faiss-cpu", "igraph", "litellm", "sentence-transformers"):
        try:
            distribution(name)
        except PackageNotFoundError:
            continue
        raise AssertionError(f"Lightweight installation unexpectedly contains {name}")

    executable = shutil.which("codenib")
    assert executable is not None
    get_language("python")  # Download a parser, never a model, before offline query.
    with tempfile.TemporaryDirectory(prefix="codenib-grep-install-") as temporary:
        # Match CLI repository normalization: macOS /var is a symlink and a
        # Windows TEMP directory may contain DOS short-name path components.
        root = Path(temporary).resolve()
        clean_env = {
            name: value
            for name, value in os.environ.items()
            if not any(
                word in name.upper()
                for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
            )
        }
        clean_env.update(CODENIB_HOME=str(root / "state"), PATH=str(root / "empty"))
        with patch.dict(os.environ, clean_env, clear=True):
            binary = resolve_ripgrep()
            assert binary is not None and Path(binary).is_absolute()
            subprocess.run(
                [executable, "mcp", "--runtime-probe", "--retrieval-route", "grep-jev"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=root,
            )
            report = _query(root)
            report["native_keyring"] = (
                _native_keyring() if args.native_keyring else "not requested"
            )
            report["ripgrep_version"] = distribution("ripgrep-bin").version
            report["model_calls"] = 0
            print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
