# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Full-build admission must match the existing canonical document writer."""

import pytest

from codenib.index.embedding import builders
from codenib.index.embedding import vector_store as module


class _Embedding:
    def embed_query(self, _text):
        return [0.0, 0.0]

    def embed_documents(self, texts):
        return [[0.0, 0.0] for _ in texts]


def test_builder_rejects_oversized_l2_before_any_embedding(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "large.py").write_text(
        'def answer():\n    return "' + "\u263a" * 2000 + '"\n', encoding="utf-8"
    )
    index = tmp_path / "index"
    index.mkdir()
    previous = index / "previous-generation"
    previous.write_bytes(b"unchanged")
    monkeypatch.setattr(module, "_MAX_PORTABLE_DOCUMENTS_JSON_BYTES", 1000)

    def embedding_boundary(**_kwargs):
        pytest.fail("Oversized L2 must be rejected before initializing L0 embedding")

    monkeypatch.setattr(builders, "CodeVectorStore", embedding_boundary)
    with pytest.raises(ValueError, match="canonical vector documents exceed"):
        builders.build_hierarchical_vector_store(
            repo_path=str(repository),
            index_path=str(index),
            languages=["python"],
            embedding_model="test-model",
            embedding_provider="huggingface",
            embedding_dimension=4,
            artifact_metadata={"builder_schema": 8},
            strict_chunking=True,
            force_rebuild=True,
        )
    assert list(index.iterdir()) == [previous]
    assert previous.read_bytes() == b"unchanged"


@pytest.mark.parametrize("level", ["l0", "l2"])
def test_admission_matches_saved_bytes_and_accepts_the_exact_limit(
    tmp_path, monkeypatch, level
):
    chunks = [
        {
            "file": "pkg/example.py",
            "content": 'def calculate():\n    return "\u263a\\quoted"\n',
            "name": "calculate",
            "chunk_type": "file" if level == "l0" else "function",
            "start_line": 0,
            "end_line": 1,
        },
        {"file": "empty.py", "content": ""},
    ]
    estimated = module._validate_schema_8_code_chunks(iter(chunks), level=level)
    store = module.CodeVectorStore(
        embedding_model="test-model",
        embedding_provider="huggingface",
        dimension=2,
        embedding=_Embedding(),
        artifact_metadata={"builder_schema": 8},
    )
    try:
        store.add_code_chunks(chunks, level=level)
        store._save_level(tmp_path, level, "test-model")
        path = tmp_path / level / "documents_test-model.json"
        original = path.read_bytes()
        assert estimated == len(original)

        monkeypatch.setattr(module, "_MAX_PORTABLE_DOCUMENTS_JSON_BYTES", estimated)
        assert (
            module._validate_schema_8_code_chunks(iter(chunks), level=level)
            == estimated
        )
        store._save_level(tmp_path, level, "test-model")
        assert path.read_bytes() == original

        monkeypatch.setattr(module, "_MAX_PORTABLE_DOCUMENTS_JSON_BYTES", estimated - 1)
        with pytest.raises(ValueError, match="canonical vector documents exceed"):
            module._validate_schema_8_code_chunks(iter(chunks), level=level)
        with pytest.raises(ValueError, match="canonical vector documents exceed"):
            store._save_level(tmp_path, level, "test-model")
        assert path.read_bytes() == original
    finally:
        store.close()
