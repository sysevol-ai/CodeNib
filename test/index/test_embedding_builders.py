# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import pytest

from codenib.index.embedding import builders


def test_cached_builder_closes_store_when_load_fails(monkeypatch, tmp_path):
    index_path = tmp_path / "index"
    index_path.mkdir()
    (index_path / "config_test-model.json").write_text("{}", encoding="utf-8")
    primary = RuntimeError("cached load failed")
    stores = []

    class FakeStore:
        def __init__(self, **_kwargs):
            self.close_calls = 0
            stores.append(self)

        def load(self, _path, **_kwargs):
            raise primary

        def close(self):
            self.close_calls += 1

    monkeypatch.setattr(builders, "CodeVectorStore", FakeStore)
    monkeypatch.setattr(
        builders,
        "_mint_trusted_local_vector_authorization",
        lambda *_args, **_kwargs: object(),
    )

    with pytest.raises(RuntimeError) as exc_info:
        builders.build_hierarchical_vector_store(
            repo_path=str(tmp_path),
            index_path=str(index_path),
            embedding_model="test-model",
            embedding_provider="huggingface",
            embedding_dimension=4,
        )

    assert exc_info.value is primary
    assert len(stores) == 1
    assert stores[0].close_calls == 1


def test_hierarchical_builder_reuses_a_supplied_embedding(monkeypatch, tmp_path):
    source = tmp_path / "source.py"
    source.write_text("def example():\n    pass\n", encoding="utf-8")
    chunk = SimpleNamespace(
        file="source.py",
        _asdict=lambda: {
            "file": "source.py",
            "content": source.read_text(encoding="utf-8"),
        },
    )

    class FakeChunker:
        def __init__(self, **kwargs):
            pass

        def chunk_repository(self, repo_path, *, strict=False):
            captured["strict_chunking"] = strict
            return [chunk]

    captured = {}

    class FakeStore:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.l0_documents = []
            self.l2_documents = []

        def add_code_chunks(self, chunks, level):
            self.l2_documents.extend(chunks)

        def save(self, path):
            pass

    monkeypatch.setattr(builders, "CodeChunker", FakeChunker)
    monkeypatch.setattr(builders, "CodeVectorStore", FakeStore)
    embedding = object()
    stale_level = tmp_path / "index" / "l0"
    stale_level.mkdir(parents=True)
    model_suffix = "test-model"
    stale_files = [
        stale_level / f"config_{model_suffix}.json",
        stale_level / f"index_{model_suffix}.faiss",
        stale_level / f"documents_{model_suffix}.pkl",
        stale_level / f"index_{model_suffix}.pkl",
    ]
    for path in stale_files:
        path.write_bytes(b"stale")
    unrelated = stale_level / "index_other-model.faiss"
    unrelated.write_bytes(b"other")

    result = builders.build_hierarchical_vector_store(
        repo_path=str(tmp_path),
        index_path=str(tmp_path / "index"),
        languages=["python"],
        build_levels=["l2"],
        embedding_model="test-model",
        embedding_provider="huggingface",
        embedding_dimension=2,
        embedding=embedding,
        artifact_metadata={"commit": "a" * 40},
        force_rebuild=True,
        strict_chunking=True,
    )

    assert captured["embedding"] is embedding
    assert captured["artifact_metadata"] == {"commit": "a" * 40}
    assert captured["strict_chunking"] is True
    assert result.l2_documents
    assert all(not path.exists() for path in stale_files)
    assert unrelated.read_bytes() == b"other"


def test_cached_index_without_authority_is_rebuilt_from_source(
    monkeypatch,
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.py"
    source.write_text("def example():\n    pass\n", encoding="utf-8")
    chunk = SimpleNamespace(
        file="source.py",
        _asdict=lambda: {
            "file": "source.py",
            "content": source.read_text(encoding="utf-8"),
        },
    )
    calls = {"chunk": 0, "load": 0, "save": 0}

    class FakeChunker:
        def __init__(self, **_kwargs):
            pass

        def chunk_repository(self, repo_path, *, strict=False):
            assert repo_path == str(repo)
            calls["chunk"] += 1
            return [chunk]

    class FakeStore:
        def __init__(self, **_kwargs):
            self.l0_documents = []
            self.l2_documents = []

        def load(self, *_args, **_kwargs):
            calls["load"] += 1

        def add_code_chunks(self, chunks, level):
            assert level == "l2"
            self.l2_documents.extend(chunks)

        def save(self, _path):
            calls["save"] += 1

    monkeypatch.setattr(builders, "CodeChunker", FakeChunker)
    monkeypatch.setattr(builders, "CodeVectorStore", FakeStore)

    index = tmp_path / "index"
    stale_l0 = index / "l0"
    stale_l0.mkdir(parents=True)
    (index / "config_test-model.json").write_text("{}", encoding="utf-8")
    (stale_l0 / "index_test-model.faiss").write_bytes(b"stale")

    result = builders.build_hierarchical_vector_store(
        repo_path=str(repo),
        index_path=str(index),
        languages=["python"],
        build_levels=["l2"],
        embedding_model="test-model",
        embedding_provider="huggingface",
        embedding_dimension=2,
    )

    assert result.l2_documents
    assert calls == {"chunk": 1, "load": 0, "save": 1}
    assert not stale_l0.exists()


def test_cached_index_without_authority_or_source_is_left_unchanged(tmp_path):
    index = tmp_path / "index"
    stale_l0 = index / "l0"
    stale_l0.mkdir(parents=True)
    config = index / "config_test-model.json"
    stale = stale_l0 / "index_test-model.faiss"
    config.write_bytes(b"config")
    stale.write_bytes(b"stale")
    before = {
        path.relative_to(index).as_posix(): path.read_bytes()
        for path in index.rglob("*")
        if path.is_file()
    }

    with pytest.raises(ValueError, match="repo_path is required to rebuild"):
        builders.build_hierarchical_vector_store(
            repo_path="",
            index_path=str(index),
            languages=["python"],
            build_levels=["l2"],
            embedding_model="test-model",
            embedding_provider="huggingface",
            embedding_dimension=2,
        )

    after = {
        path.relative_to(index).as_posix(): path.read_bytes()
        for path in index.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_authorized_cache_is_loaded_without_rechunking(monkeypatch, tmp_path):
    index = tmp_path / "index"
    index.mkdir()
    (index / "config_test-model.json").write_text("{}", encoding="utf-8")
    authorization = object()
    calls = []

    class FakeStore:
        def __init__(self, **_kwargs):
            pass

        def load(self, path, *, native_index_authorization):
            calls.append((path, native_index_authorization))

    monkeypatch.setattr(builders, "CodeVectorStore", FakeStore)
    monkeypatch.setattr(
        builders,
        "CodeChunker",
        lambda **_kwargs: pytest.fail("authorized cache must not be rechunked"),
    )

    result = builders.build_hierarchical_vector_store(
        repo_path="",
        index_path=str(index),
        embedding_model="test-model",
        embedding_provider="huggingface",
        embedding_dimension=2,
        native_index_authorization=authorization,
    )

    assert isinstance(result, FakeStore)
    assert calls == [(str(index), authorization)]


def test_invalid_cache_authority_propagates_without_rebuild(monkeypatch, tmp_path):
    index = tmp_path / "index"
    index.mkdir()
    (index / "config_test-model.json").write_text("{}", encoding="utf-8")

    class FakeStore:
        def __init__(self, **_kwargs):
            pass

        def load(self, _path, *, native_index_authorization):
            assert native_index_authorization is invalid_authorization
            raise ValueError("authorization does not match captured bytes")

    invalid_authorization = object()
    monkeypatch.setattr(builders, "CodeVectorStore", FakeStore)
    monkeypatch.setattr(
        builders,
        "CodeChunker",
        lambda **_kwargs: pytest.fail("invalid authority must not trigger rebuild"),
    )

    with pytest.raises(ValueError, match="does not match captured bytes"):
        builders.build_hierarchical_vector_store(
            repo_path=str(tmp_path),
            index_path=str(index),
            embedding_model="test-model",
            embedding_provider="huggingface",
            embedding_dimension=2,
            native_index_authorization=invalid_authorization,
        )
