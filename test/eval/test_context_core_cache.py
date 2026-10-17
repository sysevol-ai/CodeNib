# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""A pilot cache must bind the case and local model before native parsing."""

import json
from types import SimpleNamespace

import pytest

from codenib.compiler.manifest import IndexEntry, RepoManifest
from scripts.benchmark_context_core import digest, prepare_manifest


@pytest.mark.parametrize(
    "mutation", [None, "input", "case", "source", "revision", "route", "stale"]
)
def test_cache_producer_identity_checked_before_native_load(tmp_path, mutation):
    repo = tmp_path / "repo"
    repo.mkdir()
    producer = tmp_path / "producer"
    (producer / "indexes").mkdir(parents=True)
    output = tmp_path / "output"
    output.mkdir()
    record = {"instance_id": "case", "base_commit": "c" * 40}
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(record))
    model, revision = "jinaai/jina-embeddings-v2-base-code", "a" * 40
    entry = IndexEntry(
        index_type="vector",
        path=str(producer / "indexes/vector"),
        built_at="",
        built_at_epoch=0.0,
        status="fresh",
        source_fingerprint="sha256-v2:" + "d" * 64,
        config={
            "embedding_model": model,
            "embedding_provider": "huggingface",
            "embedding_dimension": 768,
            "embedding_kwargs": {
                "revision": revision,
                "model_kwargs": {"trust_remote_code": True},
            },
        },
    )
    manifest = RepoManifest(
        repo_path=str(repo),
        commit=record["base_commit"],
        source_fingerprint=entry.source_fingerprint,
        indexes={"vector": entry},
    )
    protocol = {
        "input_sha256": digest(input_path),
        "embedding_model": model,
        "embedding_revision": revision,
    }
    raw = {
        "instance_id": "case",
        "status": "ok",
        "snapshot_before": {"commit": record["base_commit"]},
        "snapshot_after": {"commit": record["base_commit"]},
    }
    if mutation == "input":
        input_path.write_text("different input")
    elif mutation == "case":
        raw["instance_id"] = "other"
    elif mutation == "source":
        manifest.repo_path = str(tmp_path / "other")
    elif mutation == "revision":
        entry.config["embedding_kwargs"]["revision"] = "b" * 40
    elif mutation == "route":
        entry.config["embedding_provider"] = "openai"
    elif mutation == "stale":
        entry.status = "stale"
    manifest.save(producer / "indexes/repo_manifest.json")
    (producer / "index-outcomes.json").write_text(json.dumps(manifest.to_dict()))
    (producer / "protocol.json").write_text(json.dumps(protocol))
    (producer / "trajectory.json").write_text(json.dumps(raw))
    args = SimpleNamespace(
        reuse_from=producer,
        output=output,
        input=input_path,
        embedding_model=model,
        embedding_revision=revision,
    )
    if mutation:
        with pytest.raises(ValueError):
            prepare_manifest(args, record, repo, ["python"])
        assert not (output / "cache-producer.json").exists()
    else:
        loaded, evidence = prepare_manifest(args, record, repo, ["python"])
        assert loaded.to_dict() == manifest.to_dict()
        assert evidence == "contextbench-explicit-owned-cache-reuse"
        assert (output / "cache-producer.json").exists()
    assert not (output / "model-intent.json").exists()
