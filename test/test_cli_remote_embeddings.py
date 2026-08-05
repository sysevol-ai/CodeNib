# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from codenib import cli, provider_routes


class _EmbeddingHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.__class__.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "payload": payload,
            }
        )
        inputs = payload["input"]
        if isinstance(inputs, str):
            inputs = [inputs]
        response = {
            "object": "list",
            "model": payload["model"],
            "data": [
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": [1.0, float(index), 0.0, 0.0],
                }
                for index, _ in enumerate(inputs)
            ],
            "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs)},
        }
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args) -> None:
        return


def test_github_models_semantic_build_uses_remote_sdk_without_sentence_transformers(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text(
        "def answer(value: int) -> int:\n    return value + 1\n"
    )
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GITHUB_TOKEN", "runtime-token")
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    _EmbeddingHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/inference"
    monkeypatch.setattr(provider_routes, "GITHUB_MODELS_BASE_URL", endpoint)

    try:
        manifest, failed = cli.index_repository(
            repo,
            languages=["python"],
            views=["vector"],
            embedding_provider="github_models",
            embedding_model="openai/text-embedding-3-small",
            embedding_dimension=4,
            embedding_endpoint=endpoint,
            embedding_credential_env="GITHUB_TOKEN",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert failed == []
    entry = manifest.indexes["vector"]
    assert entry.config["embedding_provider"] == "github_models"
    assert entry.config["embedding_endpoint"] == endpoint
    assert entry.config["embedding_dimension"] == 4
    serialized = json.dumps(entry.config, sort_keys=True)
    assert "runtime-token" not in serialized
    assert "GITHUB_TOKEN" not in serialized
    assert _EmbeddingHandler.requests
    assert all(
        request["path"] == "/inference/embeddings"
        for request in _EmbeddingHandler.requests
    )
    assert all(
        request["authorization"] == "Bearer runtime-token"
        for request in _EmbeddingHandler.requests
    )
