# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import pytest

from codenib.index.embedding.vector_store import _HuggingFaceEmbeddingWrapper


class _FakeSentenceTransformer:
    def __init__(
        self,
        max_positions,
        position_padding_idx,
        *,
        embedding_positions=None,
    ):
        self.tokenizer = SimpleNamespace(model_max_length=10**30)
        table_size = (
            embedding_positions if embedding_positions is not None else max_positions
        )
        self.max_seq_length = max_positions or table_size
        position_embeddings = SimpleNamespace(
            num_embeddings=table_size,
            padding_idx=position_padding_idx,
        )
        auto_model = SimpleNamespace(
            config=SimpleNamespace(max_position_embeddings=max_positions),
            embeddings=SimpleNamespace(position_embeddings=position_embeddings),
        )
        self._transformer = SimpleNamespace(auto_model=auto_model)

    def __getitem__(self, index):
        assert index == 0
        return self._transformer


@pytest.mark.parametrize(
    ("max_positions", "position_padding_idx", "requested", "expected"),
    [
        (512, None, None, 512),
        (1026, 1, None, 1024),
        (1026, 1, 256, 256),
        (1026, 1, 2048, 1024),
    ],
)
def test_huggingface_sequence_length_respects_position_capacity(
    max_positions, position_padding_idx, requested, expected
):
    wrapper = _HuggingFaceEmbeddingWrapper.__new__(_HuggingFaceEmbeddingWrapper)
    wrapper._model = _FakeSentenceTransformer(max_positions, position_padding_idx)

    wrapper._apply_max_seq_length("test/model", requested)

    assert wrapper._model.tokenizer.model_max_length == expected
    assert wrapper._model.max_seq_length == expected


@pytest.mark.parametrize("configured_positions", [None, 4096])
def test_huggingface_sequence_length_uses_the_loaded_embedding_table(
    configured_positions,
):
    wrapper = _HuggingFaceEmbeddingWrapper.__new__(_HuggingFaceEmbeddingWrapper)
    wrapper._model = _FakeSentenceTransformer(
        configured_positions,
        1,
        embedding_positions=1026,
    )

    wrapper._apply_max_seq_length("test/model", None)

    assert wrapper._model.tokenizer.model_max_length == 1024
    assert wrapper._model.max_seq_length == 1024
