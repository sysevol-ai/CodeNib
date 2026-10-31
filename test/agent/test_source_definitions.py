# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Source identity and unsupported-result boundaries of definition candidates."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from codenib.agent import source_definitions as definitions
from codenib.source_fingerprint import RepositoryChangedError, capture_repository_source


def location(file="sample.py", line=0, start=4, end=12):
    return {
        "uri": "file:///workspace/" + file,
        "range": {
            "start": {"line": line, "character": start},
            "end": {"line": line, "character": end},
        },
    }


@pytest.fixture
def candidate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    text = "def original(value):\n    return value\nanswer = original(1)\n"
    (repo / "sample.py").write_text(text)
    source = capture_repository_source(repo)
    identity = source.authenticated_identity_snapshot()
    provider = definitions.SourceDefinitionProvider(
        source, identity.fingerprint, ("unused-node", tmp_path / "unused.js")
    )
    # Unit boundary: raw replies are supplied directly. Installed acceptance
    # separately exercises the real worker and complete-source preparation.
    provider._sources = {"sample.py": text}
    provider.ready = {"unit_transport": True}
    provider.status = "ok"
    try:
        yield provider, source, repo
    finally:
        provider.close()
        source.close()


def reply(provider, locations):
    return {
        "source_fingerprint": provider.fingerprint,
        "response": {"jsonrpc": "2.0", "id": 1, "result": locations},
    }


def test_one_physical_declaration_keeps_candidate_metadata(candidate, monkeypatch):
    provider, source, _repo = candidate
    monkeypatch.setattr(
        provider, "_exchange", lambda *_a, **_kw: reply(provider, [location()])
    )

    rows = provider.definition(file_path="sample.py", line=2, character=9)

    assert len(rows) == 1 and rows[0].node_name == "sample.py:original"
    assert rows[0].start_line == 0 and rows[0].file == "sample.py"
    metadata = rows.provider_metadata_dict()
    assert metadata["behavior_contract"] == "captured_python_definition_candidate_v1"
    assert (
        metadata["index_snapshot"]
        == source.authenticated_identity_snapshot().fingerprint
    )
    assert metadata["position_encoding"] == "UTF8"
    assert provider.can_serve("references").status == "unsupported"


@pytest.mark.parametrize(
    "locations,reason",
    [
        ([], "definition_not_one_candidate"),
        (
            [location(), location(line=2, start=0, end=6)],
            "definition_not_one_candidate",
        ),
        ([location(file="missing.py")], "definition_without_captured_python"),
        ([location(file="../host.py")], "definition_without_captured_python"),
        ([location(line=2, start=0, end=6)], "definition_is_not_exact_declaration"),
        ([location(end=11)], "definition_is_not_exact_declaration"),
    ],
)
def test_unknown_ambiguous_or_non_declaration_reply_requires_graph(
    candidate, monkeypatch, locations, reason
):
    provider, _source, _repo = candidate
    monkeypatch.setattr(
        provider, "_exchange", lambda *_a, **_kw: reply(provider, locations)
    )

    with pytest.raises(definitions.SourceDefinitionUnavailable, match=reason):
        provider.definition(file_path="sample.py", line=2, character=9)


def test_reply_from_another_source_generation_is_rejected(candidate, monkeypatch):
    provider, _source, _repo = candidate
    wrong = reply(provider, [location()])
    wrong["source_fingerprint"] = "sha256-v2:" + "0" * 64
    monkeypatch.setattr(provider, "_exchange", lambda *_a, **_kw: wrong)

    with pytest.raises(
        definitions.SourceDefinitionUnavailable, match="generation_mismatch"
    ):
        provider.definition(file_path="sample.py", line=2, character=9)


def test_source_change_after_reply_rejects_publication(candidate, monkeypatch):
    provider, _source, repo = candidate

    def respond(*_args, **_options):
        (repo / "sample.py").write_text("def replacement():\n    return 9\n")
        return reply(provider, [location()])

    monkeypatch.setattr(provider, "_exchange", respond)
    with pytest.raises(RepositoryChangedError):
        provider.definition(file_path="sample.py", line=2, character=9)


def test_utf8_request_and_utf16_reply_preserve_non_bmp_columns(tmp_path, monkeypatch):
    text = 'emoji = "😀"\ndef 別名(value):\n    return value\nvalue = "😀"; result = 別名(1)\n'
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "unicode.py").write_text(text)
    with capture_repository_source(repo) as source:
        provider = definitions.SourceDefinitionProvider(
            source,
            source.authenticated_identity_snapshot().fingerprint,
            ("unused-node", tmp_path / "unused.js"),
        )
        provider._sources = {"unicode.py": text}
        provider.ready = {}
        sent = []

        def respond(action, **params):
            sent.append((action, params))
            return reply(provider, [location("unicode.py", 1, 4, 6)])

        monkeypatch.setattr(provider, "_exchange", respond)
        line = text.splitlines()[3]
        prefix = line[: line.index("別名")]
        rows = provider.definition(
            file_path="unicode.py", line=3, character=len(prefix.encode())
        )
        assert sent[0][1]["character"] == len(prefix.encode("utf-16-le")) // 2
        assert rows[0].node_name == "unicode.py:別名"
        provider.close()
        assert source.usable


def test_oversized_complete_frame_is_rejected_before_json_decode(
    candidate, monkeypatch
):
    provider, _source, _repo = candidate
    monkeypatch.setattr(definitions, "_MAX_REPLY_BYTES", 16)
    provider._pending = (
        definitions._PREFIX + json.dumps({"data": "x" * 30}).encode() + b"\n"
    )
    with pytest.raises(
        definitions.SourceDefinitionUnavailable, match="reply_too_large"
    ):
        provider._receive(time.monotonic() - 1)


def test_failed_wait_keeps_process_and_payload_for_explicit_retry(
    candidate, monkeypatch
):
    provider, source, _repo = candidate

    class Process:
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        returncode = None
        waits = []

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, *, timeout):
            self.waits.append(timeout)
            raise subprocess.TimeoutExpired("owned-worker", timeout)

    process = Process()
    provider.process = process
    provider._temporary = tempfile.TemporaryDirectory()
    payload_dir = Path(provider._temporary.name)

    def stalled(*_args, **_options):
        raise definitions.SourceDefinitionUnavailable("source_worker_deadline")

    monkeypatch.setattr(provider, "_exchange", stalled)
    with pytest.raises(subprocess.TimeoutExpired):
        provider.close()
    assert provider.process is process and payload_dir.exists()
    assert not process.stdin.closed and source.usable
    assert process.waits and all(value > 0 for value in process.waits)
    with pytest.raises(
        definitions.SourceDefinitionUnavailable, match="source_worker_needs_cleanup"
    ):
        provider.definition(file_path="sample.py", line=2, character=9)

    process.returncode = -9
    provider.close()
    assert provider.process is None and not payload_dir.exists()
    assert process.stdin.closed and process.stdout.closed
    assert source.usable


def test_runtime_rejects_wrong_bundle_even_when_package_version_matches(
    tmp_path, monkeypatch
):
    package = tmp_path / "node-tools/node_modules/browser-basedpyright"
    (package / "dist").mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"version": definitions.BASEDPYRIGHT_VERSION})
    )
    (package / "dist/pyright.worker.js").write_text("not the pinned provider")
    monkeypatch.setattr(definitions, "toolchain_dir", lambda: tmp_path)
    monkeypatch.setattr(definitions, "resolve_command", lambda _name: "/usr/bin/node")

    assert definitions.managed_definition_runtime() is None


def test_runtime_respects_an_explicit_python_server_override(monkeypatch):
    monkeypatch.setenv("CODENIB_PYTHON_LSP_CMD", "custom-python-server --stdio")

    def unexpected_probe(*_args):
        pytest.fail("an explicit provider choice must not probe the pinned worker")

    monkeypatch.setattr(definitions, "resolve_command", unexpected_probe)
    assert definitions.managed_definition_runtime() is None
