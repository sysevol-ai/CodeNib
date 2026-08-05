# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import shlex
import shutil
import stat
import subprocess
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

import codenib.artifacts.github as github_artifacts
import codenib.mcp.server as server_module
from codenib.artifacts import (
    CONTEXT_ARTIFACT_MANIFEST,
    bind_context_artifact,
    extract_context_artifact_archive,
    fetch_github_context_artifact,
    render_artifact_mcp_config,
    resolve_github_context_artifact,
    stage_context_artifact,
    verify_context_artifact,
)
from codenib.cli import run
from codenib.compiler.index_builders import VectorIndexBuilder
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.index.embedding.vector_store import CodeVectorStore
from codenib.mcp.context import ServerContext
from codenib.source_fingerprint import fingerprint_repository


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _bm25_artifact(
    tmp_path: Path,
    *,
    source_symlink: bool = False,
) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "sample.py"
    if source_symlink:
        outside = tmp_path / "outside.py"
        outside.write_text("SECRET = 'outside checkout'\n", encoding="utf-8")
        source.symlink_to(outside)
    else:
        source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "CodeNib Test")
    _git(repo, "config", "user.email", "codenib@example.invalid")
    _git(repo, "add", "sample.py")
    _git(repo, "commit", "--quiet", "-m", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")

    index_root = tmp_path / "indexes"
    view = index_root / "bm25"
    view.mkdir(parents=True)
    (view / "documents.json").write_text(
        json.dumps(
            [
                {
                    "page_content": "sample py value 1",
                    "metadata": {
                        "file": "sample.py",
                        "name": "VALUE",
                        "node_id": "sample.py:VALUE",
                        "type": "variable",
                        "start_line": 0,
                        "end_line": 0,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    (view / "bm25_metadata.json").write_text(
        json.dumps(
            {
                "project_root": str(repo),
                "max_k": 15,
                "language": "english",
            }
        ),
        encoding="utf-8",
    )
    source_identity = fingerprint_repository(repo)
    manifest_path = index_root / "repo_manifest.json"
    RepoManifest(
        repo_path=str(repo),
        commit=commit,
        last_indexed_commit=commit,
        source_fingerprint=source_identity.value,
        last_indexed_source_fingerprint=source_identity.value,
        languages=["python"],
        file_count=source_identity.file_count,
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(view),
                built_at="2026-08-04T00:00:00+00:00",
                built_at_epoch=1.0,
                status="fresh",
                commit=commit,
                source_fingerprint=source_identity.value,
            )
        },
        capabilities={
            "sparse_search": True,
            "dense_search": False,
            "hybrid_search": False,
            "symbol_navigation": False,
        },
        compiled_at="2026-08-04T00:00:00+00:00",
        compiled_at_epoch=1.0,
    ).save(manifest_path)
    artifact = tmp_path / "artifact"
    stage_context_artifact(
        repo,
        manifest_path,
        artifact,
        repository="example/project",
    )
    return repo, artifact, commit


class _PortableEmbedding:
    def embed_query(self, text: str) -> list[float]:
        values = [
            float(value + 1) for value in hashlib.sha256(text.encode()).digest()[:4]
        ]
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]


def _vector_artifact(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "semantic-repo"
    repo.mkdir()
    (repo / "search.py").write_text(
        "def locate_symbol(query):\n    return query.casefold()\n",
        encoding="utf-8",
    )
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "CodeNib Test")
    _git(repo, "config", "user.email", "codenib@example.invalid")
    _git(repo, "add", "search.py")
    _git(repo, "commit", "--quiet", "-m", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    config = VectorIndexBuilder(
        languages=["python"],
        embedding_model="test/model",
        embedding_provider="huggingface",
        embedding_dimension=4,
        build_levels=["l2"],
    ).artifact_identity()
    index_root = tmp_path / "semantic-indexes"
    vector = index_root / "vector"
    with patch.object(
        CodeVectorStore,
        "_initialize_embedding_model",
        return_value=_PortableEmbedding(),
    ):
        store = CodeVectorStore(
            embedding_model="test/model",
            embedding_provider="huggingface",
            dimension=4,
            index_metric="ip",
            store_path=str(vector),
            artifact_metadata=config,
        )
        store.add_code_chunks(
            [
                {
                    "content": "def locate_symbol(query): return query.casefold()",
                    "chunk_type": "function",
                    "name": "locate_symbol",
                    "file": str(repo / "search.py"),
                    "start_line": 0,
                    "end_line": 1,
                }
            ],
            level="l2",
        )
        store.save()

    source_identity = fingerprint_repository(repo)
    manifest_path = index_root / "repo_manifest.json"
    RepoManifest(
        repo_path=str(repo),
        commit=commit,
        last_indexed_commit=commit,
        source_fingerprint=source_identity.value,
        last_indexed_source_fingerprint=source_identity.value,
        languages=["python"],
        file_count=source_identity.file_count,
        indexes={
            "vector": IndexEntry(
                index_type="vector",
                path=str(vector),
                built_at="2026-08-04T00:00:00+00:00",
                built_at_epoch=1.0,
                status="fresh",
                config=dict(config),
                metadata=dict(config),
                commit=commit,
                source_fingerprint=source_identity.value,
            )
        },
        capabilities={
            "sparse_search": False,
            "dense_search": True,
            "hybrid_search": False,
            "symbol_navigation": False,
        },
        compiled_at="2026-08-04T00:00:00+00:00",
        compiled_at_epoch=1.0,
    ).save(manifest_path)
    artifact = tmp_path / "semantic-artifact"
    stage_context_artifact(
        repo,
        manifest_path,
        artifact,
        repository="example/semantic-project",
    )
    return repo, artifact, commit


def _metadata(artifact: Path) -> dict:
    return json.loads((artifact / CONTEXT_ARTIFACT_MANIFEST).read_text())


def _write_metadata(artifact: Path, metadata: dict) -> None:
    (artifact / CONTEXT_ARTIFACT_MANIFEST).write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _refresh_inventory_file(artifact: Path, metadata: dict, relative: str) -> None:
    path = artifact / relative
    record = next(item for item in metadata["files"] if item["path"] == relative)
    record["bytes"] = path.stat().st_size
    record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()


def _zip_tree(source: Path, output: Path, *, prefix: str = "") -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                relative = path.relative_to(source).as_posix()
                archive.write(path, f"{prefix}{relative}")


def test_verify_and_bind_artifact_before_loading_bm25(tmp_path: Path) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)

    verified = verify_context_artifact(
        artifact,
        expected_repository="example/project",
        expected_commit=commit,
    )
    binding = bind_context_artifact(
        artifact,
        repo,
        expected_repository="example/project",
        expected_commit=commit,
    )

    assert verified.views == ("bm25",)
    assert binding.manifest.repo_path == str(repo)
    assert binding.manifest.indexes["bm25"].path == str(artifact / "views" / "bm25")
    context = ServerContext.load(binding.manifest, views=["bm25"])
    assert context.bm25 is not None
    assert context.bm25.project_root == str(repo)
    results = context.bm25.search("value", return_code_content=True)
    assert results[0].file == "sample.py"
    assert "VALUE = 1" in (results[0].content or "")


def test_bind_and_query_portable_semantic_artifact_without_pickle(
    tmp_path: Path,
) -> None:
    repo, artifact, commit = _vector_artifact(tmp_path)
    assert not list(artifact.rglob("*.pkl"))
    binding = bind_context_artifact(
        artifact,
        repo,
        expected_repository="example/semantic-project",
        expected_commit=commit,
    )

    with patch.object(
        CodeVectorStore,
        "_initialize_embedding_model",
        return_value=_PortableEmbedding(),
    ):
        context = ServerContext.load(binding.manifest, views=["vector"])
        assert context.vector is not None
        results = context.vector.search("locate a symbol", top_k=1)

    assert results[0].node_name == "locate_symbol"
    assert results[0].file == "search.py"


def test_verify_rejects_digest_mismatch(tmp_path: Path) -> None:
    _repo, artifact, _commit = _bm25_artifact(tmp_path)
    (artifact / "views" / "bm25" / "documents.json").write_text("[]\n")

    with pytest.raises(ValueError, match="digest mismatch"):
        verify_context_artifact(artifact)


def test_verify_rejects_extra_file_and_symlink(tmp_path: Path) -> None:
    _repo, artifact, _commit = _bm25_artifact(tmp_path)
    (artifact / "extra.txt").write_text("not inventoried")
    with pytest.raises(ValueError, match="file set differs"):
        verify_context_artifact(artifact)

    (artifact / "extra.txt").unlink()
    (artifact / "link").symlink_to(artifact / "repo_manifest.json")
    with pytest.raises(ValueError, match="symbolic link"):
        verify_context_artifact(artifact)


def test_verify_rejects_pickle_even_when_inventoried(tmp_path: Path) -> None:
    _repo, artifact, _commit = _bm25_artifact(tmp_path)
    payload = b"not executable, but the format is forbidden"
    (artifact / "views" / "bm25" / "documents.pkl").write_bytes(payload)
    metadata = _metadata(artifact)
    metadata["files"].append(
        {
            "path": "views/bm25/documents.pkl",
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    )
    _write_metadata(artifact, metadata)

    with pytest.raises(ValueError, match="must not contain pickle"):
        verify_context_artifact(artifact)


def test_verify_rejects_inventory_path_escape(tmp_path: Path) -> None:
    _repo, artifact, _commit = _bm25_artifact(tmp_path)
    metadata = _metadata(artifact)
    metadata["files"][0]["path"] = "../outside"
    _write_metadata(artifact, metadata)

    with pytest.raises(ValueError, match="normalized relative POSIX path"):
        verify_context_artifact(artifact)


def test_verify_rejects_repository_and_commit_mismatch(tmp_path: Path) -> None:
    _repo, artifact, commit = _bm25_artifact(tmp_path)
    with pytest.raises(ValueError, match="repository mismatch"):
        verify_context_artifact(
            artifact,
            expected_repository="different/project",
        )
    with pytest.raises(ValueError, match="commit mismatch"):
        verify_context_artifact(
            artifact,
            expected_commit="f" * 40 if commit != "f" * 40 else "e" * 40,
        )


def test_verify_rejects_unsafe_bm25_source_contract(tmp_path: Path) -> None:
    _repo, artifact, _commit = _bm25_artifact(tmp_path)
    metadata_path = artifact / "views" / "bm25" / "bm25_metadata.json"
    metadata_path.write_text('{"project_root": "/tmp"}\n', encoding="utf-8")
    metadata = _metadata(artifact)
    _refresh_inventory_file(
        artifact,
        metadata,
        "views/bm25/bm25_metadata.json",
    )
    _write_metadata(artifact, metadata)
    with pytest.raises(ValueError, match="project root"):
        verify_context_artifact(artifact)

    metadata_path.write_text('{"project_root": "source"}\n', encoding="utf-8")
    documents_path = artifact / "views" / "bm25" / "documents.json"
    documents = json.loads(documents_path.read_text())
    documents[0]["metadata"]["file"] = "../../outside.py"
    documents_path.write_text(json.dumps(documents), encoding="utf-8")
    metadata = _metadata(artifact)
    _refresh_inventory_file(
        artifact,
        metadata,
        "views/bm25/bm25_metadata.json",
    )
    _refresh_inventory_file(
        artifact,
        metadata,
        "views/bm25/documents.json",
    )
    _write_metadata(artifact, metadata)
    with pytest.raises(ValueError, match="repository-relative POSIX path"):
        verify_context_artifact(artifact)


def test_bind_rejects_source_symlink_outside_checkout(tmp_path: Path) -> None:
    repo, artifact, _commit = _bm25_artifact(tmp_path, source_symlink=True)

    with pytest.raises(ValueError, match="inside the repository checkout"):
        bind_context_artifact(artifact, repo)


def test_bind_rejects_checkout_commit_drift(tmp_path: Path) -> None:
    repo, artifact, _commit = _bm25_artifact(tmp_path)
    (repo / "second.py").write_text("SECOND = 2\n", encoding="utf-8")
    _git(repo, "add", "second.py")
    _git(repo, "commit", "--quiet", "-m", "second")

    with pytest.raises(ValueError, match="checkout commit does not match"):
        bind_context_artifact(artifact, repo)


def test_bind_rejects_checkout_source_drift(tmp_path: Path) -> None:
    repo, artifact, _commit = _bm25_artifact(tmp_path)
    (repo / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="source files do not match"):
        bind_context_artifact(artifact, repo)


def test_extract_archive_verifies_and_removes_single_root_prefix(
    tmp_path: Path,
) -> None:
    _repo, artifact, commit = _bm25_artifact(tmp_path)
    archive = tmp_path / "artifact.zip"
    _zip_tree(artifact, archive, prefix="download/")

    verified = extract_context_artifact_archive(
        archive,
        tmp_path / "extracted",
        expected_repository="example/project",
        expected_commit=commit,
    )

    assert verified.commit == commit
    assert verified.metadata_path == tmp_path / "extracted" / (
        CONTEXT_ARTIFACT_MANIFEST
    )


def test_extract_archive_rejects_traversal_and_symlink(tmp_path: Path) -> None:
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr(CONTEXT_ARTIFACT_MANIFEST, "{}")
        archive.writestr("../outside", "bad")
    with pytest.raises(ValueError, match="path is unsafe"):
        extract_context_artifact_archive(traversal, tmp_path / "traversal-output")

    linked = tmp_path / "linked.zip"
    with zipfile.ZipFile(linked, "w") as archive:
        archive.writestr(CONTEXT_ARTIFACT_MANIFEST, "{}")
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "repo_manifest.json")
    with pytest.raises(ValueError, match="symbolic link"):
        extract_context_artifact_archive(linked, tmp_path / "linked-output")


def test_extract_archive_enforces_expanded_size_before_writing(tmp_path: Path) -> None:
    archive = tmp_path / "large.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(CONTEXT_ARTIFACT_MANIFEST, "{}")
        bundle.writestr("large.bin", "x" * 1024)

    with pytest.raises(ValueError, match="expanded bytes"):
        extract_context_artifact_archive(
            archive,
            tmp_path / "large-output",
            max_bytes=128,
        )
    assert not (tmp_path / "large-output").exists()


def test_extract_archive_limits_directory_entries(tmp_path: Path) -> None:
    archive = tmp_path / "directories.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(CONTEXT_ARTIFACT_MANIFEST, "{}")
        for index in range(67):
            bundle.writestr(f"directory-{index}/", "")

    with pytest.raises(ValueError, match="archive exceeds .* entries"):
        extract_context_artifact_archive(
            archive,
            tmp_path / "directory-output",
            max_files=1,
        )


def _github_record(
    *,
    artifact_id: int,
    commit: str,
    digest: str,
    expired: bool = False,
) -> dict:
    return {
        "id": artifact_id,
        "name": f"codenib-context-example-project-{commit[:12]}",
        "size_in_bytes": 1024,
        "archive_download_url": (
            "https://api.github.com/repos/example/project/actions/artifacts/"
            f"{artifact_id}/zip"
        ),
        "digest": digest,
        "expired": expired,
        "created_at": f"2026-08-0{artifact_id}T00:00:00Z",
        "workflow_run": {"head_sha": commit},
    }


def test_resolve_github_artifact_uses_exact_head_sha_and_newest_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo, _artifact, commit = _bm25_artifact(tmp_path)
    digest = f"sha256:{'a' * 64}"
    observed: dict = {}

    def fake_json(url: str, *, token: str | None) -> dict:
        observed.update(url=url, token=token)
        return {
            "artifacts": [
                _github_record(artifact_id=1, commit="f" * 40, digest=digest),
                _github_record(artifact_id=2, commit=commit, digest=digest),
                _github_record(artifact_id=3, commit=commit, digest=digest),
                _github_record(
                    artifact_id=4,
                    commit=commit,
                    digest=digest,
                    expired=True,
                ),
            ]
        }

    monkeypatch.setattr(github_artifacts, "_github_json", fake_json)
    record = resolve_github_context_artifact(
        "Example/Project",
        commit,
        token="runtime-token",
    )

    assert record.artifact_id == 3
    assert record.head_sha == commit
    assert observed["token"] == "runtime-token"
    assert "name=codenib-context-example-project-" in observed["url"]


def test_fetch_github_artifact_verifies_archive_digest_and_reuses_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo, artifact, commit = _bm25_artifact(tmp_path)
    archive = tmp_path / "source.zip"
    _zip_tree(artifact, archive)
    archive_bytes = archive.read_bytes()
    archive_digest = hashlib.sha256(archive_bytes).hexdigest()
    record = _github_record(
        artifact_id=7,
        commit=commit,
        digest=f"sha256:{archive_digest}",
    )
    record["size_in_bytes"] = len(archive_bytes)
    calls = {"list": 0, "download": 0}

    def fake_json(_url: str, *, token: str | None) -> dict:
        assert token == "runtime-token"
        calls["list"] += 1
        return {"artifacts": [record]}

    def fake_download(
        _url: str,
        output: Path,
        *,
        token: str,
        max_bytes: int,
    ) -> tuple[int, str]:
        assert token == "runtime-token"
        assert len(archive_bytes) < max_bytes
        calls["download"] += 1
        shutil.copyfile(archive, output)
        return len(archive_bytes), archive_digest

    monkeypatch.setattr(github_artifacts, "_github_json", fake_json)
    monkeypatch.setattr(github_artifacts, "_download_archive", fake_download)
    output = tmp_path / "cache" / commit
    first = fetch_github_context_artifact(
        "example/project",
        commit,
        output_dir=output,
        token="runtime-token",
    )
    second = fetch_github_context_artifact(
        "example/project",
        commit,
        output_dir=output,
    )

    assert first.downloaded is True
    assert first.record is not None and first.record.artifact_id == 7
    assert second.downloaded is False
    assert second.record is None
    assert calls == {"list": 1, "download": 1}
    with pytest.raises(ValueError, match="inventory exceeds 2 files"):
        fetch_github_context_artifact(
            "example/project",
            commit,
            output_dir=output,
            max_files=2,
        )


def test_fetch_github_artifact_rejects_archive_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo, artifact, commit = _bm25_artifact(tmp_path)
    archive = tmp_path / "source.zip"
    _zip_tree(artifact, archive)
    record = _github_record(
        artifact_id=8,
        commit=commit,
        digest=f"sha256:{'0' * 64}",
    )

    monkeypatch.setattr(
        github_artifacts,
        "_github_json",
        lambda _url, *, token: {"artifacts": [record]},
    )

    def fake_download(
        _url: str,
        output: Path,
        *,
        token: str,
        max_bytes: int,
    ) -> tuple[int, str]:
        shutil.copyfile(archive, output)
        return output.stat().st_size, "f" * 64

    monkeypatch.setattr(github_artifacts, "_download_archive", fake_download)
    output = tmp_path / "cache" / commit
    with pytest.raises(ValueError, match="archive digest"):
        fetch_github_context_artifact(
            "example/project",
            commit,
            output_dir=output,
            token="runtime-token",
        )
    assert not output.exists()


def test_github_download_does_not_forward_token_to_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"zip-payload"
    requests: list[urllib.request.Request] = []

    class RedirectingOpener:
        def open(self, request: urllib.request.Request, timeout: int):
            requests.append(request)
            headers = {"Location": "https://objects.example/artifact.zip"}
            raise urllib.error.HTTPError(
                request.full_url,
                302,
                "Found",
                headers,
                None,
            )

    class ObjectResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(request: urllib.request.Request, timeout: int):
        requests.append(request)
        return ObjectResponse(payload)

    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *_handlers: RedirectingOpener(),
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    output = tmp_path / "artifact.zip"
    size, digest = github_artifacts._download_archive(
        "https://api.github.com/repos/example/project/actions/artifacts/1/zip",
        output,
        token="never-forward-this-token",
        max_bytes=1024,
    )

    assert size == len(payload)
    assert digest == hashlib.sha256(payload).hexdigest()
    assert requests[0].get_header("Authorization") == (
        "Bearer never-forward-this-token"
    )
    assert requests[1].get_header("Authorization") is None


def test_github_api_refuses_redirect_with_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []

    class RedirectingOpener:
        def open(self, request: urllib.request.Request, timeout: int):
            requests.append(request)
            raise urllib.error.HTTPError(
                request.full_url,
                302,
                "Found",
                {"Location": "https://attacker.example/artifacts"},
                None,
            )

    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *_handlers: RedirectingOpener(),
    )

    with pytest.raises(RuntimeError, match="HTTP 302"):
        github_artifacts._github_json(
            "https://api.github.com/repos/example/project/actions/artifacts",
            token="never-forward-this-token",
        )

    assert len(requests) == 1
    assert requests[0].get_header("Authorization") == (
        "Bearer never-forward-this-token"
    )


def test_artifact_cli_verifies_binding_and_renders_configs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)

    assert (
        run(
            [
                "artifact",
                "verify",
                str(artifact),
                "--repo",
                str(repo),
                "--repository",
                "example/project",
                "--commit",
                commit,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Repository:       example/project" in output
    assert f"Commit:           {commit}" in output
    assert f"Checkout:         {repo}" in output

    assert (
        run(
            [
                "artifact",
                "mcp-config",
                str(artifact),
                "--repo",
                str(repo),
                "--repository",
                "example/project",
                "--host",
                "json",
            ]
        )
        == 0
    )
    config = json.loads(capsys.readouterr().out)
    args = config["mcpServers"]["codenib"]["args"]
    assert args[:2] == ["mcp", "--artifact"]
    assert args[-2:] == ["--repository", "example/project"]


def test_render_artifact_mcp_host_commands_are_shell_safe(tmp_path: Path) -> None:
    repo, artifact, _commit = _bm25_artifact(tmp_path)
    codex = shlex.split(
        render_artifact_mcp_config(
            artifact,
            repo,
            host="codex",
            server_name="repository-context",
        )
    )
    claude = shlex.split(
        render_artifact_mcp_config(
            artifact,
            repo,
            host="claude",
            server_name="repository-context",
        )
    )

    assert codex[:6] == [
        "codex",
        "mcp",
        "add",
        "repository-context",
        "--",
        "codenib",
    ]
    assert claude[:7] == [
        "claude",
        "mcp",
        "add-json",
        "--scope",
        "project",
        "repository-context",
        json.dumps(
            {
                "type": "stdio",
                "command": "codenib",
                "args": codex[6:],
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ),
    ]


def test_mcp_server_starts_from_verified_artifact(tmp_path: Path) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)

    with patch.object(server_module.mcp, "run") as run_server:
        server_module.main(
            [
                "--artifact",
                str(artifact),
                "--repo",
                str(repo),
                "--repository",
                "example/project",
                "--log-level",
                "ERROR",
            ]
        )

    run_server.assert_called_once_with(transport="stdio")
    context = server_module.get_context()
    assert context.bm25 is not None
    manifest = asyncio.run(server_module.get_manifest())
    assert manifest["repo"]["commit"] == commit
    assert manifest["artifact"] == {
        "verified": True,
        "schema": "codenib.context-artifact.v1",
        "repository": "example/project",
        "commit": commit,
        "views": ["bm25"],
    }
    assert manifest["runtime"]["loaded_views"] == ["bm25"]
