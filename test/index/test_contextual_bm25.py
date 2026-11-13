# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Real local retrieval/persistence with a deterministic index-time chat fake."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codenib.code_chunker import CodeChunker
from codenib.compiler.index_builders import BM25IndexBuilder, IndexBuilderRegistry
from codenib.compiler.index_compiler import IndexCompiler, IndexCompilerConfig
from codenib.index.contextual import ContextualizationConfig, FileContextualizer
from codenib.index.sparse_idx.bm25_index import (
    SOURCE_MODE_PERSISTED_ONLY,
    BM25CodeIndexer,
)
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import RepositoryChangedError, capture_repository_source

_DEFAULT_RESPONSE = object()


class RecordingChat:
    cache_identity = "local-test-provider-v1"

    def __init__(
        self, *, target="opaque_handler", response=_DEFAULT_RESPONSE, effect=None
    ):
        self.target = target
        self.response = response
        self.effect = effect
        self.calls = []

    def complete(self, messages, **overrides):
        self.calls.append((messages, overrides))
        if self.effect is not None:
            self.effect()
        if self.response is not _DEFAULT_RESPONSE:
            return self.response
        payload = json.loads(messages[1]["content"])
        return (
            "Distinctive attestation functionality."
            if payload["node_id"].endswith(f":{self.target}()")
            else "Other arithmetic functionality."
        )


@pytest.fixture(
    params=[
        ("pkg/service.py", "opaque_handler"),
        ("nested/billing.py", "dispatch_event"),
    ]
)
def repository(tmp_path, request):
    relative, target = request.param
    root = tmp_path / "repository"
    source = root / relative
    source.parent.mkdir(parents=True)
    source.write_text(
        '"""Arithmetic helpers."""\n\n'
        f"def {target}(value):\n    return value + 1\n\n"
        "def second(value):\n    return value - 1\n\n"
        "def third(value):\n    return value * 2\n",
        encoding="utf-8",
    )
    return root, source, relative, target


def test_enriched_build_save_load_search_preserves_source(repository, tmp_path):
    root, source, relative, target = repository
    original = source.read_bytes()
    baseline_dir = tmp_path / "baseline"
    BM25IndexBuilder().build("repo", repo_path=str(root), output_dir=str(baseline_dir))
    baseline_documents = json.loads((baseline_dir / "documents.json").read_text())
    assert not any("attestation" in doc["page_content"] for doc in baseline_documents)

    chat = RecordingChat(target=target)
    builder = BM25IndexBuilder(contextualizer=FileContextualizer(chat))
    output = tmp_path / "contextual"
    status = builder.build("repo", repo_path=str(root), output_dir=str(output))
    assert status.metadata["contextualization_usage"]["completion_calls"] == 3
    assert status.metadata["contextualization_usage"]["totals"] is None
    assert (
        status.metadata["contextualization"]
        == builder.artifact_identity()["contextualization"]
    )

    loaded = BM25CodeIndexer()
    loaded.load_index(str(output))
    calls_before_search = len(chat.calls)
    hit = loaded.search(
        "attestation", top_k=1, return_code_content=True, wrap_with_ln=False
    )[0]
    assert hit.node_id == f"{relative}:{target}()"
    assert hit.content == f"def {target}(value):\n    return value + 1\n"
    assert (hit.start_line, hit.end_line) == (2, 3)
    assert "attestation" not in hit.content
    assert len(chat.calls) == calls_before_search
    assert source.read_bytes() == original
    documents = json.loads((output / "documents.json").read_text())
    target_document = next(
        d for d in documents if d["metadata"]["node_id"] == hit.node_id
    )
    receipt = target_document["metadata"]["contextualization"]
    assert "attestation" not in target_document["page_content"]
    assert "attestation" in receipt["index_prefix"]
    assert receipt["prefix"] == "Distinctive attestation functionality."
    assert receipt["input_digest"].startswith("sha256:")
    assert receipt["source_file_digest"].startswith("sha256:")
    assert receipt["file_context_truncated"] is False


def test_default_builder_never_generates_context(repository, tmp_path, monkeypatch):
    root, *_ = repository

    def unexpected(*_args, **_kwargs):
        raise AssertionError("ordinary indexing must not invoke contextualization")

    monkeypatch.setattr(FileContextualizer, "contextualize", unexpected)
    status = BM25IndexBuilder().build(
        "repo", repo_path=str(root), output_dir=str(tmp_path / "index")
    )
    assert status.metadata["builder_schema"] == 10
    assert status.metadata["contextualization"] is None
    assert "contextualization_usage" not in status.metadata


@pytest.mark.parametrize("parent", [False, True])
def test_output_cannot_overwrite_repository_or_parent(repository, parent):
    root, source, *_ = repository
    before = source.read_bytes()
    chat = RecordingChat()
    builder = BM25IndexBuilder(contextualizer=FileContextualizer(chat))
    with pytest.raises(ValueError, match="output cannot contain"):
        builder.build(
            "repo", repo_path=str(root), output_dir=str(root.parent if parent else root)
        )
    assert not chat.calls
    assert source.read_bytes() == before
    assert not (root / "documents.json").exists()


def test_generated_identifier_is_never_treated_as_persisted_source(
    repository, tmp_path
):
    root, *_ = repository
    chat = RecordingChat(response="fabricatedWitness supporting explanation")
    output = tmp_path / "index"
    BM25IndexBuilder(contextualizer=FileContextualizer(chat)).build(
        "repo", repo_path=str(root), output_dir=str(output)
    )
    documents = json.loads((output / "documents.json").read_text())
    metadata = json.loads((output / "bm25_metadata.json").read_text())
    loaded = BM25CodeIndexer()
    loaded.load_index_values(
        documents, metadata, source_mode=SOURCE_MODE_PERSISTED_ONLY
    )
    assert any(
        "fabricatedwitness"
        in d["metadata"].get("contextualization", {}).get("index_prefix", "")
        for d in documents
    )
    assert loaded.search_identifier_occurrences("fabricatedWitness") == []


@pytest.mark.parametrize(
    "context", [4, {}, {"index_prefix": []}, {"index_prefix": None}]
)
def test_malformed_ranking_metadata_is_rejected(context):
    loaded = BM25CodeIndexer()
    with pytest.raises(ValueError, match="contextualization metadata"):
        loaded.load_index_values(
            [
                {
                    "page_content": "ordinary source tokens",
                    "metadata": {"contextualization": context},
                }
            ],
            {},
        )


def test_language_selection_uses_existing_javascript_chunker(tmp_path):
    root = tmp_path / "javascript-repo"
    root.mkdir()
    (root / "utilities.mjs").write_text(
        "export function normalize(value) { return value + 1; }\n"
        "export function audit(value) { return value - 1; }\n"
    )
    chat = RecordingChat()
    builder = BM25IndexBuilder(
        languages=["javascript"], contextualizer=FileContextualizer(chat)
    )
    builder.build("repo", repo_path=str(root), output_dir=str(tmp_path / "index"))
    assert len(chat.calls) == 2
    payloads = [json.loads(messages[1]["content"]) for messages, _ in chat.calls]
    assert {payload["file"] for payload in payloads} == {"utilities.mjs"}
    assert all("export function" in payload["chunk"] for payload in payloads)


@pytest.mark.parametrize("limit", [900, 1_300])
def test_overrides_bound_actual_inputs_and_record_truncation(
    repository, tmp_path, limit
):
    root, source, _, target = repository
    source.write_text(source.read_text() + "\n# " + 'escaped \\" text ' * 500)
    chat = RecordingChat(target=target)
    config = ContextualizationConfig(
        max_input_chars=limit, max_prefix_chars=120, max_output_tokens=37
    )
    output = tmp_path / "index"
    BM25IndexBuilder(contextualizer=FileContextualizer(chat, config)).build(
        "repo", repo_path=str(root), output_dir=str(output)
    )
    for messages, options in chat.calls:
        assert sum(len(message["content"]) for message in messages) <= limit
        assert options["max_tokens"] == 37
        assert options["temperature"] == 0.0
        assert "usage_tracker" in options
        payload = json.loads(messages[1]["content"])
        assert payload["chunk"]
        assert not payload["file"].startswith("/")
    documents = json.loads((output / "documents.json").read_text())
    receipts = [
        d["metadata"]["contextualization"]
        for d in documents
        if "contextualization" in d["metadata"]
    ]
    assert receipts and all(r["file_context_truncated"] for r in receipts)


@pytest.mark.parametrize(
    "response", [None, "", " ", "```python\npass\n```", "x" * 31, 4]
)
def test_bad_prefix_preserves_previous_artifact(repository, tmp_path, response):
    root, _, _, target = repository
    output = tmp_path / "index"
    BM25IndexBuilder().build("repo", repo_path=str(root), output_dir=str(output))
    previous = {p.name: p.read_bytes() for p in output.iterdir()}
    chat = RecordingChat(target=target, response=response)
    builder = BM25IndexBuilder(
        contextualizer=FileContextualizer(
            chat, ContextualizationConfig(max_prefix_chars=30)
        )
    )
    with pytest.raises(ValueError, match="prefix"):
        builder.build("repo", repo_path=str(root), output_dir=str(output))
    assert {p.name: p.read_bytes() for p in output.iterdir()} == previous


def test_provider_failure_preserves_previous_artifact(repository, tmp_path):
    root, *_ = repository
    output = tmp_path / "index"
    BM25IndexBuilder().build("repo", repo_path=str(root), output_dir=str(output))
    previous = {p.name: p.read_bytes() for p in output.iterdir()}

    def timeout():
        raise TimeoutError("fixture provider timed out")

    builder = BM25IndexBuilder(
        contextualizer=FileContextualizer(RecordingChat(effect=timeout))
    )
    with pytest.raises(TimeoutError, match="timed out"):
        builder.build("repo", repo_path=str(root), output_dir=str(output))
    assert {p.name: p.read_bytes() for p in output.iterdir()} == previous


def test_source_mutation_during_generation_cannot_publish(repository, tmp_path):
    root, source, *_ = repository

    def edit():
        source.write_text("def changed():\n    return 999\n")

    builder = BM25IndexBuilder(
        contextualizer=FileContextualizer(RecordingChat(effect=edit))
    )
    output = tmp_path / "index"
    with pytest.raises(RepositoryChangedError):
        builder.build("repo", repo_path=str(root), output_dir=str(output))
    assert not output.exists()


def test_excluded_sources_never_reach_contextualizer(repository, tmp_path):
    root, _, _, target = repository
    private = root / "private"
    private.mkdir()
    (private / "credentials.py").write_text("CONFIDENTIAL_SENTINEL = 'do not send'\n")
    chat = RecordingChat(target=target)
    builder = BM25IndexBuilder(
        source_selection=RepositorySourceSelection(["private"]),
        contextualizer=FileContextualizer(chat),
    )
    builder.build("repo", repo_path=str(root), output_dir=str(tmp_path / "index"))
    assert chat.calls
    assert all("CONFIDENTIAL_SENTINEL" not in json.dumps(m) for m, _ in chat.calls)


def test_bound_source_build_and_cancellation(repository, tmp_path):
    root, _, _, target = repository
    chat = RecordingChat(target=target)
    builder = BM25IndexBuilder(contextualizer=FileContextualizer(chat))
    output = tmp_path / "bound-index"
    with capture_repository_source(root) as source:
        builder.build_from_repository_source(
            "repo", repository_source=source, output_dir=str(output)
        )
    assert len(chat.calls) == 3
    with capture_repository_source(root) as source:

        def cancelled():
            raise KeyboardInterrupt("fixture cancellation")

        with pytest.raises(KeyboardInterrupt, match="fixture cancellation"):
            builder.build_from_repository_source(
                "repo",
                repository_source=source,
                output_dir=str(tmp_path / "cancelled"),
                check_cancelled=cancelled,
            )
    assert not (tmp_path / "cancelled").exists()


def test_compiler_rejects_incompatible_profiles_in_both_directions():
    ordinary = BM25IndexBuilder()
    contextual = BM25IndexBuilder(contextualizer=FileContextualizer(RecordingChat()))
    ordinary_entry = SimpleNamespace(config=ordinary.artifact_identity())
    contextual_entry = SimpleNamespace(config=contextual.artifact_identity())
    assert IndexCompiler._entry_matches_builder(ordinary_entry, ordinary)
    assert IndexCompiler._entry_matches_builder(contextual_entry, contextual)
    assert not IndexCompiler._entry_matches_builder(ordinary_entry, contextual)
    assert not IndexCompiler._entry_matches_builder(contextual_entry, ordinary)
    legacy = dict(ordinary_entry.config)
    legacy.pop("contextualization")
    assert IndexCompiler._entry_matches_builder(
        SimpleNamespace(config=legacy), ordinary
    )


def test_configuration_and_model_changes_alter_compatibility():
    chat = RecordingChat()
    contextualizer = FileContextualizer(chat)
    original = contextualizer.artifact_identity()
    for config in (
        replace(contextualizer.config, max_input_chars=2_000),
        replace(contextualizer.config, max_prefix_chars=90),
        replace(contextualizer.config, max_output_tokens=45),
        replace(contextualizer.config, prompt="A different, versioned source prompt."),
        replace(contextualizer.config, temperature=None),
    ):
        assert FileContextualizer(chat, config).artifact_identity() != original
    chat.cache_identity = "different-model-revision"
    assert contextualizer.artifact_identity() != original


def test_compiler_switches_profiles_and_reuses_only_matching_generation(
    repository, tmp_path
):
    root, _, _, target = repository
    chat = RecordingChat(target=target)
    registry = IndexBuilderRegistry()
    contextual = BM25IndexBuilder(contextualizer=FileContextualizer(chat))
    registry.register("bm25", contextual)
    compiler = IndexCompiler(registry, IndexCompilerConfig(index_types=["bm25"]))
    cache = str(tmp_path / "compiled")
    first = compiler.compile_repo(str(root), cache_dir=cache)
    assert first.indexes["bm25"].status == "fresh"
    assert first.indexes["bm25"].config["contextualization"] is not None
    calls = len(chat.calls)
    reused = compiler.update_repo(str(root), cache_dir=cache)
    assert reused.indexes["bm25"].status == "fresh"
    assert len(chat.calls) == calls
    registry.register("bm25", BM25IndexBuilder())
    ordinary = compiler.update_repo(str(root), cache_dir=cache)
    assert ordinary.indexes["bm25"].status == "fresh"
    assert ordinary.indexes["bm25"].config["contextualization"] is None
    registry.register("bm25", contextual)
    rebuilt = compiler.update_repo(str(root), cache_dir=cache)
    assert rebuilt.indexes["bm25"].status == "fresh"
    assert len(chat.calls) == calls * 2


def test_surrounding_edit_changes_receipt_with_unchanged_target(repository, tmp_path):
    root, source, _, target = repository
    chat = RecordingChat(target=target)
    builder = BM25IndexBuilder(contextualizer=FileContextualizer(chat))

    def build_receipt(name):
        output = tmp_path / name
        builder.build("repo", repo_path=str(root), output_dir=str(output))
        documents = json.loads((output / "documents.json").read_text())
        return next(
            d["metadata"]["contextualization"]
            for d in documents
            if d["metadata"]["node_id"].endswith(f":{target}()")
        )

    before = build_receipt("before")
    source.write_text(
        source.read_text().replace(
            "Arithmetic helpers.", "Ledger reconciliation helpers."
        )
    )
    after = build_receipt("after")
    assert before["source_file_digest"] != after["source_file_digest"]
    assert before["input_digest"] != after["input_digest"]
    assert before["prefix_digest"] == after["prefix_digest"]


def test_model_mutation_during_generation_cannot_publish(repository, tmp_path):
    root, *_ = repository
    chat = RecordingChat()
    chat.effect = lambda: setattr(chat, "cache_identity", "changed-provider-revision")
    builder = BM25IndexBuilder(contextualizer=FileContextualizer(chat))
    output = tmp_path / "index"
    with pytest.raises(RuntimeError, match="identity changed"):
        builder.build("repo", repo_path=str(root), output_dir=str(output))
    assert not output.exists()


def test_builder_configuration_change_during_generation_cannot_publish(
    repository, tmp_path
):
    root, *_ = repository
    chat = RecordingChat()
    builder = BM25IndexBuilder(contextualizer=FileContextualizer(chat))
    chat.effect = lambda: setattr(builder, "max_lines_per_chunk", 12)
    output = tmp_path / "index"
    with pytest.raises(RuntimeError, match="configuration changed"):
        builder.build("repo", repo_path=str(root), output_dir=str(output))
    assert not output.exists()


def test_existing_chat_adapter_receives_bounds_and_retains_failed_usage(
    repository, monkeypatch
):
    from codenib.llm import litellm_chat, usage

    root, *_ = repository
    calls = []

    def completion(_self, options):
        calls.append(options)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="x" * 80))],
            usage=SimpleNamespace(
                prompt_tokens=11, completion_tokens=8, total_tokens=19
            ),
        )

    monkeypatch.setattr(litellm_chat.LiteLLMChat, "_completion_with_retry", completion)
    monkeypatch.setattr(usage, "_safe_completion_cost", lambda *_args: None)
    chat = litellm_chat.LiteLLMChat(
        model="fixture/model",
        api_base="https://fixture.invalid/v1",
        api_key="fixture-secret",
        max_tokens=999,
        extra_kwargs={"timeout": 12},
    )
    contextualizer = FileContextualizer(
        chat, ContextualizationConfig(max_output_tokens=23, max_prefix_chars=40)
    )
    chunks = CodeChunker(language="python").chunk_repository(str(root))
    with capture_repository_source(root) as source:
        with pytest.raises(ValueError, match="prefix"):
            contextualizer.contextualize(chunks, source)
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 23
    assert calls[0]["timeout"] == 12
    assert calls[0]["api_base"] == "https://fixture.invalid/v1"
    assert contextualizer.usage_tracker.totals().prompt_tokens == 11
    assert contextualizer.usage_tracker.totals().completion_tokens == 8
    assert "fixture-secret" not in json.dumps(contextualizer.artifact_identity())


def test_temperature_can_be_omitted_for_provider_compatibility(repository, monkeypatch):
    from codenib.llm import litellm_chat, usage

    root, *_ = repository
    calls = []

    def completion(_self, options):
        calls.append(options)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="Short source explanation.")
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=11, completion_tokens=8, total_tokens=19
            ),
        )

    monkeypatch.setattr(litellm_chat.LiteLLMChat, "_completion_with_retry", completion)
    monkeypatch.setattr(usage, "_safe_completion_cost", lambda *_args: None)
    chat = litellm_chat.LiteLLMChat(model="fixture/model", temperature=0.0)
    contextualizer = FileContextualizer(chat, ContextualizationConfig(temperature=None))
    chunks = CodeChunker(language="python").chunk_repository(str(root))
    with capture_repository_source(root) as source:
        contextualizer.contextualize(chunks, source)
    assert calls and all("temperature" not in options for options in calls)
    assert contextualizer.artifact_identity()["temperature"] is None


@pytest.mark.parametrize("value", [-1, True, "0", float("inf"), float("nan")])
def test_invalid_temperature_is_rejected(value):
    with pytest.raises(ValueError, match="temperature"):
        ContextualizationConfig(temperature=value)


def test_inline_fence_description_is_valid_search_metadata(repository, tmp_path):
    root, _, _, target = repository
    prefix = "Extracts JSON and unwraps a fenced ```json block if present."
    builder = BM25IndexBuilder(
        contextualizer=FileContextualizer(RecordingChat(target=target, response=prefix))
    )
    output = tmp_path / "contextual"
    builder.build("repo", repo_path=str(root), output_dir=str(output))
    documents = json.loads((output / "documents.json").read_text())
    receipts = [
        d["metadata"]["contextualization"]
        for d in documents
        if "contextualization" in d["metadata"]
    ]
    assert receipts and all(r["prefix"] == prefix for r in receipts)


@pytest.mark.parametrize(
    "prefix", ["A description.\n```json\n{}\n```", "~~~python\npass\n~~~"]
)
def test_actual_fenced_blocks_remain_invalid(prefix):
    with pytest.raises(ValueError, match="prefix"):
        ContextualizationConfig().validate_prefix(prefix)


@pytest.mark.parametrize(
    "field", ["max_input_chars", "max_prefix_chars", "max_output_tokens"]
)
@pytest.mark.parametrize("value", [0, -1, True, "128"])
def test_invalid_limits_fail_before_generation(field, value):
    with pytest.raises(ValueError, match="positive integer"):
        ContextualizationConfig(**{field: value})


def test_oversized_complete_target_fails_before_model_call(repository):
    root, *_ = repository
    chunks = CodeChunker(language="python").chunk_repository(str(root))
    target = next(c for c in chunks if c.chunk_type == "function")
    huge = target._replace(content="x" * 2_000)
    chat = RecordingChat()
    contextualizer = FileContextualizer(
        chat, ContextualizationConfig(max_input_chars=900)
    )
    with capture_repository_source(root) as source:
        with pytest.raises(ValueError, match="complete target"):
            contextualizer.contextualize([huge], source)
    assert not chat.calls
