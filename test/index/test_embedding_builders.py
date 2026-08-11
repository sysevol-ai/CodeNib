# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
from types import SimpleNamespace

import pytest

import codenib._atomic_directory as atomic_directory
from codenib.index.embedding import builders
from codenib.index.embedding.artifact_integrity import VECTOR_VIEW_UPDATE_MARKER
from codenib.native_index_authorization import (
    InvalidNativeIndexAuthorizationError,
    _mint_trusted_local_admin_authorization,
)


def _write_fake_vector_store(path, *, model_suffix, levels):
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    for level in levels:
        level_path = root / level
        level_path.mkdir()
        (level_path / f"config_{model_suffix}.json").write_text(
            "{}",
            encoding="utf-8",
        )
        (level_path / f"index_{model_suffix}.faiss").write_bytes(b"new-index")
        (level_path / f"documents_{model_suffix}.pkl").write_bytes(b"new-docs")
    (root / f"config_{model_suffix}.json").write_text("{}", encoding="utf-8")


def _file_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


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
            _write_fake_vector_store(
                path,
                model_suffix="test-model",
                levels=("l2",),
            )

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
    generation_root = stale_level.parent
    stale_state_files = [
        generation_root / "chunk_store.json",
        generation_root / "chunk_store.pkl",
        generation_root / "embeddings_cache.json",
        generation_root / "embeddings_cache.npz",
        generation_root / "embeddings_cache.pkl",
        generation_root / "incremental_state.json",
        generation_root / VECTOR_VIEW_UPDATE_MARKER,
    ]
    for path in stale_state_files:
        path.write_bytes(b"stale-generation-state")

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
    assert all(not path.exists() for path in stale_state_files)
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
            _write_fake_vector_store(
                _path,
                model_suffix="test-model",
                levels=("l2",),
            )

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
    authorization = _mint_trusted_local_admin_authorization(
        atomic_directory.capture_directory_ownership(index),
        view_type="vector",
        semantic_contract={},
        evidence=("verified-test-cache",),
    )
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


def test_wrong_semantic_cache_authority_rejects_before_model_init(
    monkeypatch,
    tmp_path,
):
    index = tmp_path / "index"
    index.mkdir()
    (index / "config_test-model.json").write_text("{}", encoding="utf-8")

    invalid_authorization = _mint_trusted_local_admin_authorization(
        atomic_directory.capture_directory_ownership(index),
        view_type="vector",
        semantic_contract={"dimension": 3},
        evidence=("wrong-semantic-test-cache",),
    )
    monkeypatch.setattr(
        builders,
        "CodeVectorStore",
        lambda **_kwargs: pytest.fail(
            "wrong-semantic authority must fail before model initialization"
        ),
    )
    monkeypatch.setattr(
        builders,
        "CodeChunker",
        lambda **_kwargs: pytest.fail("invalid authority must not trigger rebuild"),
    )

    with pytest.raises(
        InvalidNativeIndexAuthorizationError,
        match="does not match captured bytes",
    ):
        builders.build_hierarchical_vector_store(
            repo_path=str(tmp_path),
            index_path=str(index),
            embedding_model="test-model",
            embedding_provider="huggingface",
            embedding_dimension=2,
            artifact_metadata={"dimension": 2},
            native_index_authorization=invalid_authorization,
        )


def test_wrong_tree_cache_authority_rejects_before_model_init(
    monkeypatch,
    tmp_path,
):
    index = tmp_path / "index"
    index.mkdir()
    (index / "config_test-model.json").write_text("{}", encoding="utf-8")
    other = tmp_path / "other-index"
    other.mkdir()
    (other / "config_test-model.json").write_text(
        '{"different":true}',
        encoding="utf-8",
    )
    invalid_authorization = _mint_trusted_local_admin_authorization(
        atomic_directory.capture_directory_ownership(other),
        view_type="vector",
        semantic_contract={},
        evidence=("wrong-tree-test-cache",),
    )
    monkeypatch.setattr(
        builders,
        "CodeVectorStore",
        lambda **_kwargs: pytest.fail(
            "wrong-tree authority must fail before model initialization"
        ),
    )
    monkeypatch.setattr(
        builders,
        "CodeChunker",
        lambda **_kwargs: pytest.fail("invalid authority must not trigger rebuild"),
    )

    with pytest.raises(
        InvalidNativeIndexAuthorizationError,
        match="does not match captured bytes",
    ):
        builders.build_hierarchical_vector_store(
            repo_path=str(tmp_path),
            index_path=str(index),
            embedding_model="test-model",
            embedding_provider="huggingface",
            embedding_dimension=2,
            native_index_authorization=invalid_authorization,
        )


def test_failed_staged_save_preserves_existing_cache(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.py"
    source.write_text("def example():\n    pass\n", encoding="utf-8")
    chunk = SimpleNamespace(
        file="source.py",
        _asdict=lambda: {"file": "source.py", "content": source.read_text()},
    )

    class FakeChunker:
        def __init__(self, **_kwargs):
            pass

        def chunk_repository(self, repo_path, *, strict=False):
            del repo_path, strict
            return [chunk]

    class FailingStore:
        def __init__(self, **_kwargs):
            self.l0_documents = []
            self.l2_documents = []

        def add_code_chunks(self, chunks, level):
            getattr(self, f"{level}_documents").extend(chunks)

        def save(self, path):
            _write_fake_vector_store(
                path,
                model_suffix="test-model",
                levels=("l2",),
            )
            raise RuntimeError("simulated staged save failure")

    monkeypatch.setattr(builders, "CodeChunker", FakeChunker)
    monkeypatch.setattr(builders, "CodeVectorStore", FailingStore)

    index = tmp_path / "index"
    _write_fake_vector_store(
        index,
        model_suffix="test-model",
        levels=("l0", "l2"),
    )
    before = _file_snapshot(index)

    with pytest.raises(RuntimeError, match="staged save failure"):
        builders.build_hierarchical_vector_store(
            repo_path=str(repo),
            index_path=str(index),
            languages=["python"],
            build_levels=["l2"],
            embedding_model="test-model",
            embedding_provider="huggingface",
            embedding_dimension=2,
            force_rebuild=True,
        )

    assert _file_snapshot(index) == before
    assert not list(tmp_path.glob(".index.vector-build-*"))


def test_bad_repository_preserves_existing_cache(monkeypatch, tmp_path):
    class FailingChunker:
        def __init__(self, **_kwargs):
            pass

        def chunk_repository(self, repo_path, *, strict=False):
            del repo_path, strict
            raise OSError("bad repository")

    monkeypatch.setattr(builders, "CodeChunker", FailingChunker)
    monkeypatch.setattr(
        builders,
        "CodeVectorStore",
        lambda **_kwargs: pytest.fail("store must not be built after chunk failure"),
    )

    index = tmp_path / "index"
    _write_fake_vector_store(
        index,
        model_suffix="test-model",
        levels=("l0", "l2"),
    )
    before = _file_snapshot(index)

    with pytest.raises(OSError, match="bad repository"):
        builders.build_hierarchical_vector_store(
            repo_path=str(tmp_path / "missing-repo"),
            index_path=str(index),
            languages=["python"],
            build_levels=["l2"],
            embedding_model="test-model",
            embedding_provider="huggingface",
            embedding_dimension=2,
            force_rebuild=True,
        )

    assert _file_snapshot(index) == before


def test_unrequested_symlink_level_rejects_rebuild_without_mutation(
    monkeypatch,
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.py"
    source.write_text("def example():\n    pass\n", encoding="utf-8")
    chunk = SimpleNamespace(
        file="source.py",
        _asdict=lambda: {"file": "source.py", "content": source.read_text()},
    )

    class FakeChunker:
        def __init__(self, **_kwargs):
            pass

        def chunk_repository(self, repo_path, *, strict=False):
            del repo_path, strict
            return [chunk]

    class FakeStore:
        def __init__(self, **_kwargs):
            self.l0_documents = []
            self.l2_documents = []

        def add_code_chunks(self, chunks, level):
            getattr(self, f"{level}_documents").extend(chunks)

        def save(self, path):
            _write_fake_vector_store(
                path,
                model_suffix="test-model",
                levels=("l2",),
            )

    monkeypatch.setattr(builders, "CodeChunker", FakeChunker)
    monkeypatch.setattr(builders, "CodeVectorStore", FakeStore)

    outside = tmp_path / "outside"
    _write_fake_vector_store(
        outside,
        model_suffix="test-model",
        levels=(),
    )
    outside_config = outside / "config_test-model.json"
    outside_config.unlink()
    (outside / "index_test-model.faiss").write_bytes(b"outside-index")
    (outside / "documents_test-model.pkl").write_bytes(b"outside-docs")
    before = _file_snapshot(outside)

    index = tmp_path / "index"
    index.mkdir()
    (index / "l0").symlink_to(outside, target_is_directory=True)

    with pytest.raises((ValueError, RuntimeError), match="link"):
        builders.build_hierarchical_vector_store(
            repo_path=str(repo),
            index_path=str(index),
            languages=["python"],
            build_levels=["l2"],
            embedding_model="test-model",
            embedding_provider="huggingface",
            embedding_dimension=2,
            force_rebuild=True,
        )

    assert (index / "l0").is_symlink()
    assert _file_snapshot(outside) == before


def test_requested_symlink_level_rejects_publish_without_mutation(
    monkeypatch,
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.py"
    source.write_text("def example():\n    pass\n", encoding="utf-8")
    chunk = SimpleNamespace(
        file="source.py",
        _asdict=lambda: {"file": "source.py", "content": source.read_text()},
    )

    class FakeChunker:
        def __init__(self, **_kwargs):
            pass

        def chunk_repository(self, repo_path, *, strict=False):
            del repo_path, strict
            return [chunk]

    class FakeStore:
        def __init__(self, **_kwargs):
            self.l0_documents = []
            self.l2_documents = []

        def add_code_chunks(self, chunks, level):
            getattr(self, f"{level}_documents").extend(chunks)

        def save(self, path):
            _write_fake_vector_store(
                path,
                model_suffix="test-model",
                levels=("l2",),
            )

    monkeypatch.setattr(builders, "CodeChunker", FakeChunker)
    monkeypatch.setattr(builders, "CodeVectorStore", FakeStore)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index_test-model.faiss").write_bytes(b"outside-index")
    before = _file_snapshot(outside)
    index = tmp_path / "index"
    index.mkdir()
    old_config = index / "config_test-model.json"
    old_config.write_bytes(b"old-config")
    (index / "l2").symlink_to(outside, target_is_directory=True)

    with pytest.raises((ValueError, RuntimeError), match="link"):
        builders.build_hierarchical_vector_store(
            repo_path=str(repo),
            index_path=str(index),
            languages=["python"],
            build_levels=["l2"],
            embedding_model="test-model",
            embedding_provider="huggingface",
            embedding_dimension=2,
            force_rebuild=True,
        )

    assert old_config.read_bytes() == b"old-config"
    assert (index / "l2").is_symlink()
    assert _file_snapshot(outside) == before


def test_interrupted_directory_switch_restores_old_cache(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.py"
    source.write_text("def example():\n    pass\n", encoding="utf-8")
    chunk = SimpleNamespace(
        file="source.py",
        _asdict=lambda: {"file": "source.py", "content": source.read_text()},
    )

    class FakeChunker:
        def __init__(self, **_kwargs):
            pass

        def chunk_repository(self, repo_path, *, strict=False):
            del repo_path, strict
            return [chunk]

    class FakeStore:
        def __init__(self, **_kwargs):
            self.l0_documents = []
            self.l2_documents = []

        def add_code_chunks(self, chunks, level):
            getattr(self, f"{level}_documents").extend(chunks)

        def save(self, path):
            _write_fake_vector_store(
                path,
                model_suffix="test-model",
                levels=("l2",),
            )

    monkeypatch.setattr(builders, "CodeChunker", FakeChunker)
    monkeypatch.setattr(builders, "CodeVectorStore", FakeStore)

    index = tmp_path / "index"
    _write_fake_vector_store(
        index,
        model_suffix="test-model",
        levels=("l0", "l2"),
    )
    (index / "incremental_state.json").write_bytes(b"old-incremental-state")
    before = _file_snapshot(index)
    real_rename = atomic_directory._rename_noreplace_at
    injected = False

    def interrupt_after_publish(source_name, destination_name, source_fd, dest_fd):
        nonlocal injected
        real_rename(source_name, destination_name, source_fd, dest_fd)
        if (
            source_name.startswith(".index.normalize-")
            and destination_name == "index"
            and not injected
        ):
            injected = True
            raise KeyboardInterrupt("simulated completed publication interrupt")

    monkeypatch.setattr(
        atomic_directory,
        "_rename_noreplace_at",
        interrupt_after_publish,
    )

    with pytest.raises(KeyboardInterrupt, match="publication interrupt"):
        builders.build_hierarchical_vector_store(
            repo_path=str(repo),
            index_path=str(index),
            languages=["python"],
            build_levels=["l2"],
            embedding_model="test-model",
            embedding_provider="huggingface",
            embedding_dimension=2,
            force_rebuild=True,
        )

    assert injected is True
    assert _file_snapshot(index) == before


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_private_build_root_replacement_cannot_publish_or_write_outside(
    monkeypatch,
    tmp_path,
    replacement,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.py"
    source.write_text("def example():\n    pass\n", encoding="utf-8")
    chunk = SimpleNamespace(
        file="source.py",
        _asdict=lambda: {"file": "source.py", "content": source.read_text()},
    )

    class FakeChunker:
        def __init__(self, **_kwargs):
            pass

        def chunk_repository(self, repo_path, *, strict=False):
            del repo_path, strict
            return [chunk]

    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.bin"
    victim.write_bytes(b"outside-original")

    class ReplacingStore:
        def __init__(self, **_kwargs):
            self.l0_documents = []
            self.l2_documents = []

        def add_code_chunks(self, chunks, level):
            getattr(self, f"{level}_documents").extend(chunks)

        def save(self, path):
            anchored_root = Path(path)
            named_root = anchored_root.resolve(strict=True)
            moved_root = named_root.with_name(f"{named_root.name}.moved")
            named_root.rename(moved_root)
            if replacement == "directory":
                named_root.mkdir()
                (named_root / "attacker-tree").write_bytes(b"attacker")
            else:
                named_root.symlink_to(outside, target_is_directory=True)

            # A writer that continues using the supplied path remains anchored
            # to the retained original inode, even after the public name is a
            # directory or a symlink to the victim tree.
            (anchored_root / "victim.bin").write_bytes(b"private-only")
            _write_fake_vector_store(
                anchored_root,
                model_suffix="test-model",
                levels=("l2",),
            )

    monkeypatch.setattr(builders, "CodeChunker", FakeChunker)
    monkeypatch.setattr(builders, "CodeVectorStore", ReplacingStore)

    index = tmp_path / "index"
    _write_fake_vector_store(
        index,
        model_suffix="test-model",
        levels=("l0", "l2"),
    )
    before = _file_snapshot(index)

    with pytest.raises(RuntimeError, match="private build root changed"):
        builders.build_hierarchical_vector_store(
            repo_path=str(repo),
            index_path=str(index),
            languages=["python"],
            build_levels=["l2"],
            embedding_model="test-model",
            embedding_provider="huggingface",
            embedding_dimension=2,
            force_rebuild=True,
        )

    assert _file_snapshot(index) == before
    assert victim.read_bytes() == b"outside-original"
