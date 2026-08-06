# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

from codenib.index.embedding import builders


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
