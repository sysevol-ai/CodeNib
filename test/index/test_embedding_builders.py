# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

from codeminer.index.embedding import builders


def test_hierarchical_chunker_contract_is_explicit_and_shared():
    contract = builders.hierarchical_chunker_kwargs(["python"], 300)

    assert contract["l0"] == {
        "language": "python",
        "repo_config": contract["l0"]["repo_config"],
        "max_lines_per_chunk": None,
        "chunk_depth": 0,
        "skeleton_mode": True,
    }
    assert contract["l2"] == {
        "language": "python",
        "repo_config": contract["l2"]["repo_config"],
        "max_lines_per_chunk": 300,
        "chunk_depth": 2,
        "l2_level_exclusive": True,
        "skeleton_mode": False,
    }
    assert contract["l0"]["repo_config"] is contract["l2"]["repo_config"]


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

        def chunk_repository(self, repo_path):
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

    result = builders.build_hierarchical_vector_store(
        repo_path=str(tmp_path),
        index_path=str(tmp_path / "index"),
        languages=["python"],
        build_levels=["l2"],
        embedding_model="test-model",
        embedding_provider="huggingface",
        embedding_dimension=2,
        embedding=embedding,
        force_rebuild=True,
    )

    assert captured["embedding"] is embedding
    assert result.l2_documents
