# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``codeminer.compiler.skill_context``.

The function under test orchestrates: skill → index_type union → build/load
→ context dict. We mock the build + load steps so the tests stay pure
(no repo cloning, no real index construction). The contract these tests
pin down:

- ``required_index_types`` reads ``index_requirements`` disk-first from each
  skill's ``config.yaml`` (when ``skills_dir`` is given), falling back to the
  registered skills otherwise. The disk path works against an *empty*
  registry (#154).
- ``build_skill_contexts`` returns a dict with per-skill-type keys,
  populated only when at least one skill of that type is requested.
- Already-built indexes are loaded; missing types trigger a build.
- ``required_index_types`` excludes requirements marked ``required: false``;
  ``optional_index_types`` returns exactly those.
- Skills with no index requirements still get a context if a sibling
  skill of the same type needs one (e.g. ``embedding_search`` shares
  ``"retrieve"`` with ``bm25_search``).
"""

from __future__ import annotations

from typing import List

import pytest

from codeminer.agent.skills.core import (
    Cost,
    SkillInputSpec,
    SkillMetadata,
    SkillOutputSpec,
    SkillType,
)
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.compiler import resources as compiler_resources
from codeminer.compiler import skill_context
from codeminer.compiler.manifest import IndexEntry, RepoManifest


def _make_meta(
    skill_id: str,
    skill_type: SkillType,
    *,
    index_types: List[str] = (),
) -> SkillMetadata:
    reqs = [compiler_resources.IndexRequirement(index_type=t) for t in index_types]
    return SkillMetadata(
        skill_id=skill_id,
        skill_type=skill_type,
        inputs=[SkillInputSpec(name="query", type_hint="str")],
        outputs=SkillOutputSpec(type_hint="List[Any]"),
        cost=Cost.LOW,
        index_requirements=reqs,
    )


@pytest.fixture(autouse=True)
def _reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


@pytest.fixture
def registry():
    reg = SkillRegistry()
    reg.register(_make_meta("bm25_search", SkillType.RETRIEVAL, index_types=["bm25"]))
    reg.register(
        _make_meta("embedding_search", SkillType.RETRIEVAL, index_types=["vector"])
    )
    reg.register(_make_meta("hybrid_search", SkillType.RETRIEVAL))
    reg.register(
        _make_meta("find_callers", SkillType.EXPAND, index_types=["symbol_graph"])
    )
    reg.register(_make_meta("code_to_query", SkillType.TRANSFORM))
    return reg


# ---------------------------------------------------------------------------
# required_index_types
# ---------------------------------------------------------------------------


def test_required_types_empty(registry):
    assert skill_context.required_index_types([], skill_registry=registry) == set()


def test_required_types_single(registry):
    got = skill_context.required_index_types(["bm25_search"], skill_registry=registry)
    assert got == {"bm25"}


def test_required_types_union(registry):
    got = skill_context.required_index_types(
        ["bm25_search", "embedding_search", "find_callers"],
        skill_registry=registry,
    )
    assert got == {"bm25", "vector", "symbol_graph"}


def test_required_types_skips_no_requirement_skills(registry):
    """``hybrid_search`` and ``code_to_query`` declare no index_requirements;
    they must not contaminate the union."""
    got = skill_context.required_index_types(
        ["hybrid_search", "code_to_query"], skill_registry=registry
    )
    assert got == set()


def test_required_types_unknown_skill_silently_ignored(registry):
    """Callers that need to fail loudly should check membership themselves."""
    got = skill_context.required_index_types(
        ["bm25_search", "does_not_exist"], skill_registry=registry
    )
    assert got == {"bm25"}


# ---------------------------------------------------------------------------
# required vs optional split — required_index_types() drops required:false
# requirements; optional_index_types() returns exactly those.
# ---------------------------------------------------------------------------


def _make_mixed_meta(skill_id: str, skill_type: SkillType) -> SkillMetadata:
    """A skill that requires ``bm25`` but only *optionally* uses ``symbol_graph``.

    Mirrors ``bm25_search``'s real config, where ``symbol_graph`` is declared
    ``required: false`` (used to relabel names if present, never force-built).
    """
    reqs = [
        compiler_resources.IndexRequirement(index_type="bm25"),
        compiler_resources.IndexRequirement(index_type="symbol_graph", required=False),
    ]
    return SkillMetadata(
        skill_id=skill_id,
        skill_type=skill_type,
        inputs=[SkillInputSpec(name="query", type_hint="str")],
        outputs=SkillOutputSpec(type_hint="List[Any]"),
        cost=Cost.LOW,
        index_requirements=reqs,
    )


@pytest.fixture
def mixed_registry():
    reg = SkillRegistry()
    reg.register(_make_mixed_meta("mixed_search", SkillType.RETRIEVAL))
    return reg


def test_required_types_excludes_optional(mixed_registry):
    """``required: false`` requirements must NOT appear in the required union —
    those are "use if present, don't force a build"."""
    got = skill_context.required_index_types(
        ["mixed_search"], skill_registry=mixed_registry
    )
    assert got == {"bm25"}
    assert "symbol_graph" not in got


def test_optional_types_returns_only_optional(mixed_registry):
    """``optional_index_types`` returns exactly the ``required: false`` set,
    and never the required ones."""
    got = skill_context.optional_index_types(
        ["mixed_search"], skill_registry=mixed_registry
    )
    assert got == {"symbol_graph"}
    assert "bm25" not in got


def test_required_and_optional_are_disjoint(mixed_registry):
    required = skill_context.required_index_types(
        ["mixed_search"], skill_registry=mixed_registry
    )
    optional = skill_context.optional_index_types(
        ["mixed_search"], skill_registry=mixed_registry
    )
    assert required.isdisjoint(optional)
    assert required == {"bm25"}
    assert optional == {"symbol_graph"}


# ---------------------------------------------------------------------------
# build_skill_contexts — mock the build/load helpers so this stays pure.
# ---------------------------------------------------------------------------


@pytest.fixture
def mocked_build(monkeypatch):
    """Replace the build / load helpers with sentinel-returning fakes.

    Yields a dict that records which helpers fired, so tests can assert
    on the build/load call pattern (e.g. "missing types triggered build,
    already-built types did not").
    """
    calls = {"compile": [], "loaded": []}

    def fake_run_compiler(repo_path, index_types, cache_dir, **kwargs):
        calls["compile"].append(tuple(sorted(index_types)))

    def fake_load_bm25(cache_dir):
        calls["loaded"].append("bm25")
        return object()  # sentinel — RetrieveContext.bm25 just stores it

    def fake_load_vector(cache_dir, **kwargs):
        calls["loaded"].append("vector")
        return object()

    def fake_load_graph(cache_dir):
        calls["loaded"].append("symbol_graph")
        return object()

    monkeypatch.setattr(skill_context, "_run_compiler", fake_run_compiler)
    monkeypatch.setattr(skill_context, "_load_bm25", fake_load_bm25)
    monkeypatch.setattr(skill_context, "_load_vector", fake_load_vector)
    monkeypatch.setattr(skill_context, "_load_symbol_graph", fake_load_graph)
    return calls


def test_build_returns_retrieve_for_bm25(registry, mocked_build, tmp_path):
    contexts = skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["bm25_search"],
        cache_dir=str(tmp_path / "cache"),
        skill_registry=registry,
    )
    assert set(contexts.keys()) == {"retrieve"}
    rc = contexts["retrieve"]
    assert rc.bm25 is not None
    assert rc.vector_store is None


def test_build_returns_expand_for_find_callers(registry, mocked_build, tmp_path):
    contexts = skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["find_callers"],
        cache_dir=str(tmp_path / "cache"),
        skill_registry=registry,
    )
    assert set(contexts.keys()) == {"expand"}
    assert contexts["expand"].code_graph is not None


def test_build_returns_both_keys_for_mixed_skills(registry, mocked_build, tmp_path):
    contexts = skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["bm25_search", "embedding_search", "find_callers"],
        cache_dir=str(tmp_path / "cache"),
        skill_registry=registry,
    )
    assert set(contexts.keys()) == {"retrieve", "expand"}
    rc = contexts["retrieve"]
    assert rc.bm25 is not None
    assert rc.vector_store is not None
    assert contexts["expand"].code_graph is not None


def test_custom_composer_alone_gets_retrieve_and_expand(
    registry, mocked_build, tmp_path
):
    """A CUSTOM skill (e.g. ``codeminer_context``) that declares bm25 + vector
    + symbol_graph requirements must receive ``retrieve`` AND ``expand``
    contexts even though it isn't a RETRIEVAL/EXPAND skill type.

    Regression: packaging keyed only on skill_type silently dropped the
    loaded indexes for a lone custom composer, so it received
    ``retrieve=None``/``expand=None`` and returned nothing (n_results=0).
    """
    registry.register(
        _make_meta(
            "codeminer_context",
            SkillType.CUSTOM,
            index_types=["bm25", "vector", "symbol_graph"],
        )
    )
    contexts = skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["codeminer_context"],
        cache_dir=str(tmp_path / "cache"),
        skill_registry=registry,
    )
    assert set(contexts.keys()) == {"retrieve", "expand"}
    rc = contexts["retrieve"]
    assert rc.bm25 is not None
    assert rc.vector_store is not None
    assert contexts["expand"].code_graph is not None


def test_sibling_skill_with_no_requirement_still_gets_context(
    registry, mocked_build, tmp_path
):
    """``hybrid_search`` declares no index_requirements but is a RETRIEVAL
    skill. When requested alongside ``bm25_search``, it shares the
    ``"retrieve"`` context that bm25 forced into existence."""
    contexts = skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["bm25_search", "hybrid_search"],
        cache_dir=str(tmp_path / "cache"),
        skill_registry=registry,
    )
    assert "retrieve" in contexts


def test_transform_skill_alone_returns_empty_dict(registry, mocked_build, tmp_path):
    """``code_to_query`` is TRANSFORM type with no index requirements.
    Currently no transform context is plumbed through — the dict stays
    empty rather than fabricating a useless entry."""
    contexts = skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["code_to_query"],
        cache_dir=str(tmp_path / "cache"),
        skill_registry=registry,
    )
    assert contexts == {}


def test_already_built_index_is_not_rebuilt(registry, mocked_build, tmp_path):
    """If ``cache_dir/<index_type>`` already has a file, the compiler is
    not invoked for that type. Loading still happens."""
    cache = tmp_path / "cache"
    bm25_dir = cache / "bm25"
    bm25_dir.mkdir(parents=True)
    (bm25_dir / "documents.json").write_text("{}")

    skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["bm25_search"],
        cache_dir=str(cache),
        skill_registry=registry,
    )
    assert mocked_build["compile"] == []
    assert mocked_build["loaded"] == ["bm25"]


def test_missing_index_triggers_build(registry, mocked_build, tmp_path):
    """Empty cache → compiler is invoked for the missing type."""
    skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["bm25_search"],
        cache_dir=str(tmp_path / "cache"),
        skill_registry=registry,
    )
    assert mocked_build["compile"] == [("bm25",)]
    assert mocked_build["loaded"] == ["bm25"]


def test_rebuild_forces_full_recompile(registry, mocked_build, tmp_path):
    """Even with all dirs populated, ``rebuild=True`` rebuilds everything."""
    cache = tmp_path / "cache"
    for t in ("bm25", "vector"):
        d = cache / t
        d.mkdir(parents=True)
        (d / "marker").write_text("x")

    skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["bm25_search", "embedding_search"],
        cache_dir=str(cache),
        skill_registry=registry,
        rebuild=True,
    )
    assert mocked_build["compile"] == [("bm25", "vector")]


def test_partial_cache_only_rebuilds_missing(registry, mocked_build, tmp_path):
    """bm25 already built, vector missing → only vector is rebuilt."""
    cache = tmp_path / "cache"
    bm25_dir = cache / "bm25"
    bm25_dir.mkdir(parents=True)
    (bm25_dir / "documents.json").write_text("{}")

    skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["bm25_search", "embedding_search"],
        cache_dir=str(cache),
        skill_registry=registry,
    )
    assert mocked_build["compile"] == [("vector",)]
    # Both types are loaded after build (build happened, both dirs populated).
    assert sorted(mocked_build["loaded"]) == ["bm25", "vector"]


# ---------------------------------------------------------------------------
# optional index handling in build_skill_contexts — load-if-present only,
# never build.
# ---------------------------------------------------------------------------


def test_optional_index_loaded_when_already_present(
    mixed_registry, mocked_build, tmp_path
):
    """``mixed_search`` requires ``bm25`` and optionally uses ``symbol_graph``.
    When ``symbol_graph`` already exists on disk it is loaded (and packaged
    into an ``expand`` context) — but it is never handed to the compiler."""
    cache = tmp_path / "cache"
    graph_dir = cache / "symbol_graph"
    graph_dir.mkdir(parents=True)
    (graph_dir / "graph.pkl").write_text("x")

    contexts = skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["mixed_search"],
        cache_dir=str(cache),
        skill_registry=mixed_registry,
    )
    # bm25 is required & missing -> built; symbol_graph is optional -> NOT built.
    assert mocked_build["compile"] == [("bm25",)]
    assert "symbol_graph" not in [t for tup in mocked_build["compile"] for t in tup]
    # Present-on-disk optional index is loaded alongside the required bm25.
    assert sorted(mocked_build["loaded"]) == ["bm25", "symbol_graph"]
    assert contexts["retrieve"].bm25 is not None
    assert contexts["expand"].code_graph is not None


def test_optional_index_skipped_when_absent(mixed_registry, mocked_build, tmp_path):
    """When the optional ``symbol_graph`` is absent on disk, it is neither
    built nor loaded — and no ``expand`` context is fabricated for it."""
    contexts = skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["mixed_search"],
        cache_dir=str(tmp_path / "cache"),
        skill_registry=mixed_registry,
    )
    # Only the required bm25 is built; optional symbol_graph never triggers one.
    assert mocked_build["compile"] == [("bm25",)]
    assert mocked_build["loaded"] == ["bm25"]
    assert "expand" not in contexts
    assert contexts["retrieve"].bm25 is not None


def test_build_skill_contexts_without_reset_is_idempotent(
    registry, mocked_build, tmp_path
):
    """Regression test: build_skill_contexts should work without calling
    registry.reset() - SkillLoader.load_all is idempotent via has() guard.

    This verifies the fix for Issue #146 where HybridRetrievePipeline was
    incorrectly calling registry.reset(), which destroys the global singleton.
    """
    # Simulate skills already being registered (as would happen in production)
    # The registry fixture already registered skills via the autouse fixture

    # First call should work
    contexts1 = skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["bm25_search"],
        cache_dir=str(tmp_path / "cache1"),
        skill_registry=registry,
    )
    assert "retrieve" in contexts1
    assert contexts1["retrieve"].bm25 is not None

    # Second call should also work without reset()
    contexts2 = skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["embedding_search"],
        cache_dir=str(tmp_path / "cache2"),
        skill_registry=registry,
    )
    assert "retrieve" in contexts2
    assert contexts2["retrieve"].vector_store is not None

    # Verify registry still has skills (not destroyed by reset)
    assert registry.get("bm25_search") is not None
    assert registry.get("embedding_search") is not None


# ---------------------------------------------------------------------------
# required_index_types / _skill_types_for — disk-first resolution (#154)
#
# These pin the fix for the chicken-and-egg ordering bug: the resolvers must
# read each skill's ``index_requirements`` / ``skill_type`` straight from
# ``config.yaml`` on disk, WITHOUT a populated SkillRegistry.
# ---------------------------------------------------------------------------


def _write_skill_config(root, skill_id: str, body: str) -> None:
    """Create ``<root>/<skill_id>/config.yaml`` with *body* contents."""
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "config.yaml").write_text(body)


@pytest.fixture
def skills_dir(tmp_path):
    """A skills directory on disk mirroring the bundled config.yaml shape."""
    root = tmp_path / "skills"
    _write_skill_config(
        root,
        "bm25_search",
        "skill_id: bm25_search\n"
        "skill_type: retrieval\n"
        "index_requirements:\n"
        "  - type: bm25\n"
        "    max_age_seconds: 3600\n"
        "    scope: current_repo\n",
    )
    _write_skill_config(
        root,
        "embedding_search",
        "skill_id: embedding_search\n"
        "skill_type: retrieval\n"
        "index_requirements:\n"
        "  - type: vector\n",
    )
    _write_skill_config(
        root,
        "graph_expand",
        "skill_id: graph_expand\n"
        "skill_type: expand\n"
        "index_requirements:\n"
        "  - type: symbol_graph\n",
    )
    # No index_requirements — must contribute nothing to the union.
    _write_skill_config(
        root,
        "regex_search",
        "skill_id: regex_search\nskill_type: retrieval\n",
    )
    # A custom composer declaring all three indexes.
    _write_skill_config(
        root,
        "codeminer_context",
        "skill_id: codeminer_context\n"
        "skill_type: custom\n"
        "index_requirements:\n"
        "  - type: bm25\n"
        "  - type: vector\n"
        "  - type: symbol_graph\n",
    )
    return str(root)


@pytest.fixture(autouse=True)
def _clear_config_cache():
    """Drop the module-level config cache so per-test ``skills_dir`` fixtures
    (which live under unique tmp paths) never see a stale parse."""
    skill_context._SKILL_CONFIG_CACHE.clear()
    yield
    skill_context._SKILL_CONFIG_CACHE.clear()


def test_required_types_disk_first_with_empty_registry(skills_dir):
    """The core #154 fix: index types resolve from config.yaml on disk even
    though the SkillRegistry is empty (reset by the autouse fixture)."""
    assert SkillRegistry().get("bm25_search") is None  # registry really is empty

    got = skill_context.required_index_types(
        ["bm25_search", "embedding_search", "graph_expand"],
        skills_dir=skills_dir,
    )
    assert got == {"bm25", "vector", "symbol_graph"}


def test_required_types_disk_single_with_empty_registry(skills_dir):
    got = skill_context.required_index_types(["bm25_search"], skills_dir=skills_dir)
    assert got == {"bm25"}


def test_required_types_disk_skips_no_requirement_skills(skills_dir):
    got = skill_context.required_index_types(["regex_search"], skills_dir=skills_dir)
    assert got == set()


def test_required_types_disk_custom_composer(skills_dir):
    got = skill_context.required_index_types(
        ["codeminer_context"], skills_dir=skills_dir
    )
    assert got == {"bm25", "vector", "symbol_graph"}


def test_required_types_disk_takes_precedence_over_registry(skills_dir):
    """On-disk config is authoritative: if the registry disagrees with disk
    for a bundled skill, the disk value wins."""
    reg = SkillRegistry()
    # Registry claims bm25_search needs ``vector`` — disk says ``bm25``.
    reg.register(_make_meta("bm25_search", SkillType.RETRIEVAL, index_types=["vector"]))
    got = skill_context.required_index_types(
        ["bm25_search"], skills_dir=skills_dir, skill_registry=reg
    )
    assert got == {"bm25"}


def test_required_types_registry_fallback_for_off_disk_skill(skills_dir):
    """A skill not present on disk (e.g. @skill-decorator-registered) falls
    back to the registry."""
    reg = SkillRegistry()
    reg.register(
        _make_meta("decorator_only", SkillType.RETRIEVAL, index_types=["vector"])
    )
    got = skill_context.required_index_types(
        ["bm25_search", "decorator_only"],
        skills_dir=skills_dir,
        skill_registry=reg,
    )
    assert got == {"bm25", "vector"}


def test_required_types_unknown_skill_ignored_disk(skills_dir):
    got = skill_context.required_index_types(
        ["bm25_search", "does_not_exist"], skills_dir=skills_dir
    )
    assert got == {"bm25"}


def test_skill_types_for_disk_first_with_empty_registry(skills_dir):
    types = skill_context._skill_types_for(
        ["bm25_search", "graph_expand", "codeminer_context"],
        skills_dir=skills_dir,
    )
    assert types == {SkillType.RETRIEVAL, SkillType.EXPAND, SkillType.CUSTOM}


def test_skill_types_for_registry_fallback(skills_dir):
    reg = SkillRegistry()
    reg.register(_make_meta("decorator_only", SkillType.RERANK))
    types = skill_context._skill_types_for(
        ["bm25_search", "decorator_only"],
        skills_dir=skills_dir,
        skill_registry=reg,
    )
    assert types == {SkillType.RETRIEVAL, SkillType.RERANK}


def test_read_skill_configs_is_cached(skills_dir):
    """Second read returns the same cached object (no re-parse)."""
    first = skill_context._read_skill_configs(skills_dir)
    second = skill_context._read_skill_configs(skills_dir)
    assert first is second
    assert set(first) == {
        "bm25_search",
        "embedding_search",
        "graph_expand",
        "regex_search",
        "codeminer_context",
    }


# ---------------------------------------------------------------------------
# load_contexts_from_manifest — optional index loads when fresh, skips (no
# raise) when absent; a missing *required* index still raises.
# ---------------------------------------------------------------------------


def _fresh_entry(index_type: str, path: str) -> IndexEntry:
    return IndexEntry(
        index_type=index_type,
        path=path,
        built_at="2026-01-01T00:00:00Z",
        built_at_epoch=0.0,
        status="fresh",
    )


def test_manifest_loads_optional_index_when_fresh(mixed_registry, mocked_build):
    """``mixed_search`` requires ``bm25`` and optionally uses ``symbol_graph``.
    When the manifest has a fresh ``symbol_graph`` entry it is loaded and an
    ``expand`` context is packaged."""
    manifest = RepoManifest(
        indexes={
            "bm25": _fresh_entry("bm25", "/idx/bm25"),
            "symbol_graph": _fresh_entry("symbol_graph", "/idx/graph"),
        }
    )
    contexts = skill_context.load_contexts_from_manifest(
        manifest,
        skill_ids=["mixed_search"],
        skill_registry=mixed_registry,
    )
    assert sorted(mocked_build["loaded"]) == ["bm25", "symbol_graph"]
    assert contexts["retrieve"].bm25 is not None
    assert contexts["expand"].code_graph is not None


def test_manifest_skips_optional_index_when_absent(mixed_registry, mocked_build):
    """When the optional ``symbol_graph`` is absent from the manifest it is
    skipped silently (no raise) — only the required ``bm25`` is loaded and no
    ``expand`` context is created."""
    manifest = RepoManifest(
        indexes={
            "bm25": _fresh_entry("bm25", "/idx/bm25"),
        }
    )
    contexts = skill_context.load_contexts_from_manifest(
        manifest,
        skill_ids=["mixed_search"],
        skill_registry=mixed_registry,
    )
    assert mocked_build["loaded"] == ["bm25"]
    assert "expand" not in contexts
    assert contexts["retrieve"].bm25 is not None


def test_manifest_missing_required_index_raises(mixed_registry, mocked_build):
    """A *required* index that the manifest lacks must raise loudly, rather
    than deferring to a "Skill not available" error at tool-call time."""
    manifest = RepoManifest(
        indexes={
            "symbol_graph": _fresh_entry("symbol_graph", "/idx/graph"),
        }
    )
    with pytest.raises(ValueError, match="bm25"):
        skill_context.load_contexts_from_manifest(
            manifest,
            skill_ids=["mixed_search"],
            skill_registry=mixed_registry,
        )


def test_corrupt_optional_graph_degrades_not_crash(
    mixed_registry, monkeypatch, tmp_path
):
    """A present-but-stale/corrupt OPTIONAL symbol_graph must NOT crash the
    build — it degrades to "no graph" (the ``required: false`` contract).

    Regression: build_skill_contexts opportunistically loads an optional index
    when its dir is non-empty; a schema-bumped / partial graph.pkl makes
    CodeGraph.load_graph raise, which previously crashed a bm25-only pipeline.
    """
    cache = tmp_path / "cache"
    (cache / "bm25").mkdir(parents=True)
    (cache / "bm25" / "documents.json").write_text("{}")
    # symbol_graph dir present (non-empty) but its load raises.
    (cache / "symbol_graph").mkdir(parents=True)
    (cache / "symbol_graph" / "graph.pkl").write_text("corrupt")

    monkeypatch.setattr(skill_context, "_run_compiler", lambda *a, **k: None)
    monkeypatch.setattr(skill_context, "_load_bm25", lambda d: object())

    def boom(_dir):
        raise ValueError("schema_version mismatch")

    monkeypatch.setattr(skill_context, "_load_symbol_graph", boom)

    # Must not raise even though the optional graph load fails.
    contexts = skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["mixed_search"],
        cache_dir=str(cache),
        skill_registry=mixed_registry,
    )
    assert "retrieve" in contexts  # bm25 still loaded
    assert "expand" not in contexts  # graph degraded to absent
