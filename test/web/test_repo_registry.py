# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for per-repo skill-registry isolation, config, and the QA registry."""

from types import SimpleNamespace

import pytest

from codenib.agent.skills.registry import SkillRegistry
from codenib.compiler.artifact_fingerprints import bm25_artifact_file_fingerprints
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.web.config import (
    QAConfig,
    RepoEntry,
    load_config,
    load_registry,
    save_registry,
)
from codenib.web.repo_registry import (
    _DEMO_SYSTEM_PROMPT,
    RepoBundle,
    RepoRegistry,
    _fresh_registry,
)


def test_fresh_registry_is_isolated_from_singleton():
    singleton = SkillRegistry()
    reg_a = _fresh_registry()
    reg_b = _fresh_registry()

    assert reg_a is not singleton
    assert reg_b is not singleton
    assert reg_a is not reg_b
    assert reg_a._skills is not reg_b._skills
    assert reg_a._skills is not singleton._skills


def test_ask_prompt_requires_resolving_discovered_identifiers():
    assert "targeted search" in _DEMO_SYSTEM_PROMPT
    assert "exact identifier and defining file" in _DEMO_SYSTEM_PROMPT
    assert "unresolved candidate identifier" in _DEMO_SYSTEM_PROMPT


def test_bundle_loads_views_without_constructing_agent_runtime():
    calls = []
    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(),
        view_loader=lambda target: calls.append(("views", target)),
        runtime_loader=lambda target: calls.append(("runtime", target)),
    )

    bundle.ensure_views()
    bundle.ensure_views()

    assert calls == [("views", bundle)]
    assert bundle.runner is None

    bundle.ensure_runtime()
    bundle.ensure_runtime()

    assert calls == [("views", bundle), ("runtime", bundle)]


def test_bundle_reports_indexed_source_files_instead_of_repository_files():
    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(
            file_count=99,
            indexes={
                "bm25": SimpleNamespace(metadata={"source_file_count": 3}),
            },
        ),
    )

    assert bundle._file_count() == 3


def test_bundle_reports_partial_graph_language_coverage():
    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(
            indexes={
                "symbol_graph": SimpleNamespace(
                    metadata={
                        "available_languages": ["python", "ts"],
                        "failed_languages": {
                            "cpp": "compile database unavailable",
                            "rust": "manifest unavailable",
                        },
                        "partial": True,
                    }
                )
            }
        ),
    )

    coverage = bundle.graph_coverage()

    assert coverage is not None
    assert coverage.available_languages == ["python", "ts"]
    assert coverage.unavailable_languages == ["cpp", "rust"]
    assert coverage.partial is True


def test_repo_views_reject_bm25_that_no_longer_matches_manifest(tmp_path):
    bm25_dir = tmp_path / "bm25"
    bm25_dir.mkdir()
    documents = bm25_dir / "documents.json"
    documents.write_text('[{"content":"alpha"}]')
    (bm25_dir / "bm25_metadata.json").write_text('{"max_k":10}')
    entry = IndexEntry(
        index_type="bm25",
        path=str(bm25_dir),
        built_at="2026-08-06T00:00:00Z",
        built_at_epoch=0.0,
        status="fresh",
        config={
            "artifact_file_fingerprints": bm25_artifact_file_fingerprints(bm25_dir)
        },
    )
    manifest = RepoManifest(indexes={"bm25": entry})
    documents.write_text(documents.read_text().replace("alpha", "omega"))
    bundle = SimpleNamespace(manifest=manifest, bm25=None, vector_store=None)

    with pytest.raises(ValueError, match="manifest fingerprints"):
        RepoRegistry._load_repo_views(object(), bundle)

    assert bundle.bm25 is None


@pytest.mark.parametrize(
    ("status", "view_commit"),
    [
        ("stale", "old-commit"),
        ("failed", "new-commit"),
        ("fresh", "old-commit"),
    ],
)
def test_bundle_rejects_graphs_outside_the_manifest_snapshot(
    tmp_path,
    status,
    view_commit,
):
    graph_dir = tmp_path / "symbol_graph"
    graph_dir.mkdir()
    (graph_dir / "graph.pkl").write_bytes(b"stale graph")
    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(
            commit="new-commit",
            indexes={
                "symbol_graph": SimpleNamespace(
                    status=status,
                    commit=view_commit,
                    path=str(graph_dir),
                )
            },
        ),
    )

    assert bundle._graph_path() is None


def test_bundle_accepts_fresh_graph_for_manifest_snapshot(tmp_path):
    graph_dir = tmp_path / "symbol_graph"
    graph_dir.mkdir()
    graph_path = graph_dir / "graph.pkl"
    graph_path.write_bytes(b"current graph")
    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(
            commit="new-commit",
            indexes={
                "symbol_graph": SimpleNamespace(
                    status="fresh",
                    commit="new-commit",
                    path=str(graph_dir),
                )
            },
        ),
    )

    assert bundle._graph_path() == str(graph_path)


def test_bundle_accepts_legacy_graph_beside_current_vector_view(tmp_path):
    vector_dir = tmp_path / "vector"
    vector_dir.mkdir()
    graph_path = vector_dir / "graph.pkl"
    graph_path.write_bytes(b"legacy current graph")
    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(
            commit="new-commit",
            indexes={
                "vector": SimpleNamespace(
                    status="fresh",
                    commit="new-commit",
                    path=str(vector_dir),
                )
            },
        ),
    )

    assert bundle._graph_path() == str(graph_path)


def test_config_index_types_for_mode():
    assert QAConfig(mode="sparse").index_types() == ["bm25"]
    assert QAConfig(mode="hybrid").index_types() == ["bm25", "vector"]


def test_config_paths(tmp_path):
    cfg = QAConfig(data_dir=str(tmp_path / "data"))
    assert cfg.registry_path.endswith("/data/qa_registry.json")
    assert cfg.repo_dir("django__django-123").endswith("/data/repos/django__django-123")


def test_load_config_from_yaml(tmp_path):
    cfg_file = tmp_path / "qa.yaml"
    cfg_file.write_text(
        "model: my-model\n"
        "wiki_model: wiki-model\n"
        "wiki_api_base: http://wiki.example/v1\n"
        "model_api_base: http://ask.example/v1\n"
        "model_options:\n"
        "  timeout: 20\n"
        "  extra_body:\n"
        "    reasoning:\n"
        "      enabled: true\n"
        "wiki_model_options:\n"
        "  timeout: 45\n"
        "  extra_body:\n"
        "    reasoning:\n"
        "      enabled: false\n"
        "mode: hybrid\n"
        "embedding_provider: openai\n"
        "embedding_base_url: http://embed.example/v1\n"
        "dataset: foo/bar\n"
        "per_language: 2\n"
        "languages: [python, go]\n"
        "instances: [a__a-1, b__b-2]\n"
    )
    cfg = load_config(str(cfg_file))
    assert cfg.model == "my-model"
    assert cfg.wiki_generation_model == "wiki-model"
    assert cfg.wiki_generation_api_base == "http://wiki.example/v1"
    assert cfg.model_api_base == "http://ask.example/v1"
    assert cfg.model_options["timeout"] == 20
    assert cfg.wiki_generation_options == {
        "timeout": 45,
        "extra_body": {"reasoning": {"enabled": False}},
    }
    assert cfg.mode == "hybrid"
    assert cfg.embedding_provider == "openai"
    assert cfg.embedding_base_url == "http://embed.example/v1"
    assert cfg.dataset == "foo/bar"
    assert cfg.per_language == 2
    assert cfg.languages == ["python", "go"]
    assert cfg.instances == ["a__a-1", "b__b-2"]


def test_backend_environment_overrides_yaml(tmp_path, monkeypatch):
    cfg_file = tmp_path / "qa.yaml"
    cfg_file.write_text(
        "model: yaml-ask\n"
        "wiki_model: yaml-wiki\n"
        "embedding_provider: huggingface\n"
    )
    monkeypatch.setenv("CODENIB_DEMO_MODEL", "env-ask")
    monkeypatch.setenv("CODENIB_DEMO_WIKI_MODEL", "env-wiki")
    monkeypatch.setenv("CODENIB_DEMO_WIKI_API_BASE", "http://wiki.local/v1")
    monkeypatch.setenv("CODENIB_DEMO_WIKI_API_KEY", "wiki-secret")
    monkeypatch.setenv("CODENIB_DEMO_API_BASE", "http://ask.local/v1")
    monkeypatch.setenv(
        "CODENIB_DEMO_MODEL_OPTIONS",
        '{"timeout":30,"extra_body":{"reasoning":{"enabled":true}}}',
    )
    monkeypatch.setenv(
        "CODENIB_DEMO_WIKI_MODEL_OPTIONS",
        '{"extra_body":{"reasoning":{"enabled":false}}}',
    )
    monkeypatch.setenv("CODENIB_EMBEDDING_PROVIDER", "OPENAI")
    monkeypatch.setenv("CODENIB_EMBEDDING_BASE_URL", "http://embed.local/v1")

    cfg = load_config(str(cfg_file))

    assert cfg.model == "env-ask"
    assert cfg.wiki_generation_model == "env-wiki"
    assert cfg.wiki_generation_api_base == "http://wiki.local/v1"
    assert cfg.wiki_generation_api_key == "wiki-secret"
    assert cfg.model_api_base == "http://ask.local/v1"
    assert cfg.model_options == {
        "timeout": 30,
        "extra_body": {"reasoning": {"enabled": True}},
    }
    assert cfg.wiki_generation_options == {
        "timeout": 30,
        "extra_body": {"reasoning": {"enabled": False}},
    }
    assert cfg.embedding_provider == "openai"
    assert cfg.embedding_base_url == "http://embed.local/v1"


def test_wiki_backend_falls_back_to_ask_backend():
    cfg = QAConfig(
        model_api_base="http://ask.local/v1",
        model_api_key="ask-secret",
    )

    assert cfg.wiki_generation_api_base == "http://ask.local/v1"
    assert cfg.wiki_generation_api_key == "ask-secret"


def test_explicit_wiki_model_reuses_matching_ask_provider_backend():
    cfg = QAConfig(
        model="openai/ask-model",
        wiki_model="openai/wiki-model",
        model_api_base="http://shared.local/v1",
        model_api_key="shared-secret",
    )

    assert cfg.wiki_generation_api_base == "http://shared.local/v1"
    assert cfg.wiki_generation_api_key == "shared-secret"


def test_explicit_wiki_model_does_not_inherit_different_provider_backend():
    cfg = QAConfig(
        model="openai/ask-model",
        wiki_model="vertex_ai/wiki-model",
        model_api_base="http://ask.local/v1",
        model_api_key="ask-secret",
    )

    assert cfg.wiki_generation_api_base is None
    assert cfg.wiki_generation_api_key is None


def test_rejects_unknown_embedding_provider(tmp_path):
    cfg_file = tmp_path / "qa.yaml"
    cfg_file.write_text("embedding_provider: custom\n")

    with pytest.raises(ValueError, match="embedding_provider"):
        load_config(str(cfg_file))


def test_vector_store_uses_provider_config_and_reuses_client(monkeypatch):
    created = []

    class FakeVectorStore:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.embedding = kwargs.get("embedding") or object()
            self.loaded = None
            created.append(self)

        def load(self, path):
            self.loaded = path

    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: FakeVectorStore,
    )
    cfg = QAConfig(
        embedding_provider="openai",
        embedding_model="embed-model",
        embedding_dimension=768,
        embedding_base_url="http://embed.local/v1",
        embedding_api_key="secret",
    )
    registry = RepoRegistry(cfg)
    entry = SimpleNamespace(
        path="/tmp/vector",
        config={
            "embedding_model": "embed-model",
            "embedding_provider": "openai",
            "embedding_dimension": 768,
            "embedding_endpoint": "http://embed.local/v1",
        },
    )

    first = registry._load_vector_store(entry)
    second = registry._load_vector_store(entry)

    assert first.loaded == "/tmp/vector"
    assert first.kwargs["embedding_provider"] == "openai"
    assert first.kwargs["base_url"] == "http://embed.local/v1"
    assert first.kwargs["api_key"] == "secret"
    assert first.kwargs["dimension"] == 768
    assert second.kwargs["embedding"] is first.embedding


def test_vector_store_restores_manifest_embedding_identity(monkeypatch):
    created = []

    class FakeVectorStore:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.embedding = kwargs.get("embedding") or object()
            created.append(self)

        def load(self, _path):
            pass

    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: FakeVectorStore,
    )
    registry = RepoRegistry(QAConfig(embedding_provider="huggingface"))
    entry = SimpleNamespace(
        path="/tmp/vector",
        config={
            "embedding_model": "nomic-ai/CodeRankEmbed",
            "embedding_provider": "huggingface",
            "dimension": 768,
            "embedding_kwargs": {
                "model_kwargs": {"trust_remote_code": True},
                "revision": "immutable-model-revision",
            },
            "index_metric": "l2",
        },
    )

    registry._load_vector_store(entry)

    assert created[0].kwargs["embedding_model"] == "nomic-ai/CodeRankEmbed"
    assert created[0].kwargs["dimension"] == 768
    assert created[0].kwargs["index_metric"] == "l2"
    assert created[0].kwargs["model_kwargs"] == {"trust_remote_code": True}
    assert created[0].kwargs["revision"] == "immutable-model-revision"


def test_vector_store_supports_legacy_prebuilt_provider_fallback(monkeypatch):
    created = []

    class FakeVectorStore:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.embedding = object()
            created.append(self)

        def load(self, _path):
            pass

    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: FakeVectorStore,
    )
    registry = RepoRegistry(QAConfig(embedding_provider="huggingface"))
    entry = SimpleNamespace(
        path="/tmp/legacy-prebuilt",
        config={
            "embedding_model": "nomic-ai/CodeRankEmbed",
            "embedding_dimension": 768,
        },
    )

    registry._load_vector_store(entry)

    assert created[0].kwargs["embedding_provider"] == "huggingface"
    assert created[0].kwargs["embedding_model"] == "nomic-ai/CodeRankEmbed"
    assert created[0].kwargs["dimension"] == 768


def test_vector_store_cache_separates_model_revisions(monkeypatch):
    created = []

    class FakeVectorStore:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.embedding = kwargs.get("embedding") or object()
            created.append(self)

        def load(self, _path):
            pass

    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: FakeVectorStore,
    )
    registry = RepoRegistry(QAConfig(embedding_provider="huggingface"))

    def entry(revision):
        return SimpleNamespace(
            path=f"/tmp/vector-{revision}",
            config={
                "embedding_model": "vendor/model",
                "embedding_provider": "huggingface",
                "embedding_dimension": 384,
                "embedding_kwargs": {"revision": revision},
            },
        )

    first = registry._load_vector_store(entry("revision-a"))
    second = registry._load_vector_store(entry("revision-b"))

    assert first.embedding is not second.embedding
    assert second.kwargs["embedding"] is None


def test_remote_embedding_override_cannot_replace_artifact_route(
    monkeypatch,
):
    created = []

    class FakeVectorStore:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.embedding = object()
            created.append(self)

        def load(self, _path):
            pass

    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: FakeVectorStore,
    )
    registry = RepoRegistry(
        QAConfig(
            embedding_provider="openai",
            embedding_base_url="http://embed.local/v1",
            embedding_api_key="secret",
        )
    )
    entry = SimpleNamespace(
        path="/tmp/vector",
        config={
            "embedding_model": "vendor/model",
            "embedding_provider": "huggingface",
            "embedding_dimension": 384,
            "embedding_kwargs": {
                "model_kwargs": {"trust_remote_code": True},
                "revision": "local-revision",
            },
        },
    )

    with pytest.raises(ValueError, match="endpoint does not match"):
        registry._load_vector_store(entry)

    assert created == []


def test_ask_model_receives_its_own_endpoint(monkeypatch):
    captured = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("codenib.web.repo_registry._ask_llm_type", lambda: fake_chat)
    registry = RepoRegistry(
        QAConfig(
            model="openai/ask-model",
            wiki_model="vertex_ai/wiki-model",
            model_api_base="http://ask.local/v1",
            model_api_key="ask-secret",
            max_tokens=2048,
            model_options={"api_version": "2025-01-01"},
        )
    )

    registry._create_ask_llm()

    assert captured == {
        "model": "openai/ask-model",
        "temperature": 0.0,
        "max_tokens": 2048,
        "api_base": "http://ask.local/v1",
        "api_key": "ask-secret",
        "extra_kwargs": {"api_version": "2025-01-01"},
    }


def test_ask_runtime_exposes_only_query_facing_repository_search(monkeypatch):
    captured = {}

    class FakeRunner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("codenib.agent.runner.AgentRunner", FakeRunner)
    registry = RepoRegistry(QAConfig())
    monkeypatch.setattr(registry, "_create_ask_llm", lambda: object())
    bundle = RepoBundle(
        entry=SimpleNamespace(language="python"),
        manifest=SimpleNamespace(
            repo_path="/tmp/repository",
            file_count=12,
            languages=["python"],
        ),
        bm25=object(),
    )

    registry._load_repo_runtime(bundle)

    assert captured["allow_skills"] == {"repository_search"}
    assert captured["include_default_tools"] is False
    assert captured["force_final_answer"] is True
    assert captured["review_final_answer"] is True
    assert captured["registry"].has("repository_search")
    assert bundle.runner is not None


@pytest.mark.parametrize(
    ("model", "api_base", "options"),
    [
        ("anthropic/claude-sonnet-4-5", None, {"timeout": 60}),
        (
            "vertex_ai/gemini-2.5-flash",
            None,
            {"vertex_project": "project", "vertex_location": "us-central1"},
        ),
        ("ollama/qwen3", "http://localhost:11434", {}),
        (
            "openrouter/qwen/qwen3-coder",
            None,
            {"extra_headers": {"HTTP-Referer": "https://codenib.ai"}},
        ),
    ],
)
def test_ask_model_preserves_litellm_provider_configuration(
    monkeypatch,
    model,
    api_base,
    options,
):
    captured = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("codenib.web.repo_registry._ask_llm_type", lambda: fake_chat)
    RepoRegistry(
        QAConfig(
            model=model,
            model_api_base=api_base,
            model_options=options,
        )
    )._create_ask_llm()

    assert captured["model"] == model
    assert captured["api_base"] == api_base
    assert captured["extra_kwargs"] == options


def test_registry_round_trip(tmp_path):
    path = str(tmp_path / "qa_registry.json")
    entries = [
        RepoEntry(
            instance_id="django__django-11099",
            repo="django/django",
            base_commit="abcdef1234567890",
            language="python",
            repo_dir="/data/repos/django__django-11099/django_django",
            manifest_path="/data/.../repo_manifest.json",
            problem_statement="Something is broken.",
        )
    ]
    save_registry(path, entries)
    loaded = load_registry(path)
    assert len(loaded) == 1
    assert loaded[0].instance_id == "django__django-11099"
    assert loaded[0].repo == "django/django"
    assert loaded[0].commit_short == "abcdef12"


def test_load_registry_missing_returns_empty(tmp_path):
    assert load_registry(str(tmp_path / "nope.json")) == []
