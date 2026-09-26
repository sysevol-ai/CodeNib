# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Public source → real rg/chunker, with every provider call forbidden."""

from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests
from fastapi.testclient import TestClient
from pydantic import ValidationError

from codenib.agent.runtime import grep_jev
from codenib.compiler.manifest import MANIFEST_FILENAME, RepoManifest
from codenib.paths import repo_index_dir
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import capture_repository_source
from codenib.web.public_trial import PublicTrialConfig, create_public_trial_app


@pytest.fixture
def publication(tmp_path, monkeypatch):
    root = tmp_path / "requests"
    root.mkdir()
    (root / "service.py").write_text(
        "def retry_request():\n    return 'retry network request'\n\n"
        "def unrelated():\n    return 'other behavior'\n"
    )
    (root / "private").mkdir()
    (root / "private" / "private.py").write_text("private_marker = 'never publish'\n")
    selection = RepositorySourceSelection(("private",))
    manifest = RepoManifest(repo_path=str(root), source_selection=selection)
    manifest.save(repo_index_dir(root) / MANIFEST_FILENAME)
    with capture_repository_source(root, selection=selection) as source:
        identity = source.authenticated_identity_snapshot()

    def forbidden(*_args, **_kwargs):
        pytest.fail("Public source service must never access a key or call a model")

    monkeypatch.setattr(requests.Session, "send", forbidden)
    monkeypatch.setattr(grep_jev.GrepJevConfig, "credential", forbidden)
    monkeypatch.setattr(grep_jev, "_plan", forbidden)
    monkeypatch.setattr(grep_jev.OpenRouterDecisions, "decide", forbidden)
    config = PublicTrialConfig.model_validate(
        {
            "origins": ["https://demo.example"],
            "hosts": ["testserver"],
            "repositories": [
                {
                    "id": "psf__requests-v2.32.5",
                    "root": str(root),
                    "repository": "psf/requests",
                    "commit": "a" * 40,
                    "source_fingerprint": identity.fingerprint,
                }
            ],
        }
    )
    app = create_public_trial_app(config)
    return root, config, app, TestClient(app)


def planned(config, **overrides):
    return {
        "source_fingerprint": config.repositories[0].source_fingerprint,
        "plan": {
            "actions": [
                {"pattern": "retry", "glob": "**/*.py", "case_sensitive": False}
            ]
        },
        **overrides,
    }


BASE = "/trial/repos/psf__requests-v2.32.5"


def test_public_planner_input_and_verified_source_share_the_product_route(publication):
    root, config, _, client = publication
    metadata = client.get(BASE)
    assert metadata.status_code == 200
    body = metadata.json()
    assert body["payload"]["source_file_count"] == 1
    assert body["payload"]["issue"] == ""
    assert body["protocol"]["planner_system"] == grep_jev.PLANNER_SYSTEM
    assert "def retry" not in metadata.text
    assert "private" not in metadata.text
    assert str(root) not in metadata.text

    response = client.post(BASE + "/candidates", json=planned(config))
    assert response.status_code == 200
    result = response.json()
    assert result["source_fingerprint"] == body["source_fingerprint"]
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert (
        candidate["source"]
        == "def retry_request():\n    return 'retry network request'"
    )
    assert (candidate["start_line"], candidate["end_line"]) == (1, 2)
    assert (
        candidate["url"]
        == f"https://github.com/psf/requests/blob/{'a' * 40}/service.py#L1-L2"
    )
    assert "private_marker" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert client.post("/api/chat", json={"question": "retry"}).status_code == 404


def test_stale_source_and_stale_client_are_rejected(publication):
    root, config, _, client = publication
    stale = client.post(
        BASE + "/candidates",
        json=planned(config, source_fingerprint="sha256-v2:" + "b" * 64),
    )
    assert stale.status_code == 409
    (root / "service.py").write_text("def changed():\n    return 'unpublished'\n")
    assert client.get(BASE).status_code == 409
    response = client.post(BASE + "/candidates", json=planned(config))
    assert response.status_code == 409
    assert "unpublished" not in response.text


def test_request_rejection_never_echoes_keys_or_source_paths(publication, caplog):
    root, config, _, client = publication
    secret = "must-not-appear-in-an-error"
    for headers in ({"Authorization": f"Bearer {secret}"}, {"Cookie": f"key={secret}"}):
        response = client.get(BASE, headers=headers)
        assert response.status_code == 400
        assert secret not in response.text
    invalid = client.post(BASE + "/candidates", json=planned(config, api_key=secret))
    assert invalid.status_code == 422
    assert secret not in invalid.text
    oversized = client.post(
        BASE + "/candidates",
        content=secret * 2000,
        headers={"Content-Type": "application/json"},
    )
    assert oversized.status_code == 413
    assert secret not in oversized.text
    assert secret not in caplog.text
    assert str(root) not in invalid.text


def test_origins_hosts_and_repository_allowlist_are_enforced(publication):
    _, _, _, client = publication
    assert (
        client.get(BASE, headers={"Origin": "https://evil.example"}).status_code == 403
    )
    assert client.get(BASE, headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/trial/repos/private").status_code == 404
    response = client.get(BASE, headers={"Origin": "https://demo.example"})
    assert response.headers["access-control-allow-origin"] == "https://demo.example"
    assert "access-control-allow-credentials" not in response.headers


def test_forwarded_ips_cannot_bypass_the_process_rate_limit(publication):
    _, _, app, client = publication
    now = [0.0]
    app.state.admission.clock = lambda: now[0]
    app.state.admission.started = 0
    for i in range(6):
        response = client.get(BASE, headers={"X-Forwarded-For": f"198.51.100.{i}"})
        assert response.status_code == 200
    limited = client.get(BASE)
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    now[0] = 61
    assert client.get(BASE).status_code == 200
    assert app.state.admission.active == 0


def test_source_worker_slots_cover_cleanup_until_each_worker_returns(
    publication, monkeypatch
):
    _, _, app, client = publication
    entered = threading.Barrier(3)
    release = threading.Event()
    from codenib.web import public_trial

    original = public_trial._source_response

    def blocked(*args):
        # Hold the complete real source operation inside its admitted worker.
        entered.wait(timeout=5)
        assert release.wait(timeout=5)
        return original(*args)

    monkeypatch.setattr(public_trial, "_source_response", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(client.get, BASE) for _ in range(2)]
        try:
            entered.wait(timeout=5)
            assert app.state.admission.active == 2
            assert client.get(BASE).status_code == 429
        finally:
            release.set()
        assert [call.result(timeout=5).status_code for call in pending] == [200, 200]
    assert app.state.admission.active == 0


def test_invalid_regex_does_not_start_fallback_or_leak_input(publication):
    _, config, app, client = publication
    payload = planned(config)
    payload["plan"]["actions"][0]["pattern"] = "secret("
    response = client.post(BASE + "/candidates", json=payload)
    assert response.status_code == 503
    assert "secret(" not in response.text
    assert app.state.admission.active == 0


@pytest.mark.parametrize(
    "updates",
    [
        {"origins": ["https://demo.example/path"]},
        {"origins": ["http://public.example"]},
        {"origins": ["http://[::1]:3000"]},
        {"origins": ["https://[::1]:3000"]},
        {"origins": ["https://user:pass@demo.example"]},
        {"hosts": ["*"]},
        {"repositories": []},
    ],
)
def test_operator_config_rejects_implicit_publication(publication, updates):
    _, config, _, _ = publication
    with pytest.raises(ValidationError):
        PublicTrialConfig.model_validate_json(
            json.dumps({**config.model_dump(), **updates})
        )


@pytest.mark.parametrize("proxy", [None, "127.0.0.1", "*"])
def test_launcher_trusts_only_explicit_proxy_addresses(
    publication, tmp_path, monkeypatch, proxy
):
    import uvicorn

    from codenib.web import public_trial

    _, config, _, _ = publication
    path = tmp_path / "trial.json"
    path.write_text(config.model_dump_json())
    argv = ["public_trial", "--config", str(path)]
    if proxy:
        argv += ["--trusted-proxy", proxy]
    monkeypatch.setattr(sys, "argv", argv)
    launched = []
    monkeypatch.setattr(uvicorn, "run", lambda _app, **kw: launched.append(kw))
    if proxy == "*":
        with pytest.raises(SystemExit) as exc:
            public_trial.main()
        assert exc.value.code == 2
        assert not launched
        return
    public_trial.main()
    assert launched[0]["proxy_headers"] is bool(proxy)
    assert launched[0]["forwarded_allow_ips"] == (proxy or "")
    assert launched[0]["access_log"] is False
