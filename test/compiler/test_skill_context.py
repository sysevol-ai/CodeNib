"""Unit tests for ``codeminer.compiler.skill_context``.

The function under test orchestrates: skill → index_type union → build/load
→ context dict. We mock the build + load steps so the tests stay pure
(no repo cloning, no real index construction). The contract these tests
pin down:

- ``required_index_types`` reads ``index_requirements`` straight off the
  registered skills (no I/O, no cache touch).
- ``build_skill_contexts`` returns a dict with per-skill-type keys,
  populated only when at least one skill of that type is requested.
- Already-built indexes are loaded; missing types trigger a build.
- Skills with no index requirements still get a context if a sibling
  skill of the same type needs one (e.g. ``regex_search`` shares
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


def _make_meta(
    skill_id: str,
    skill_type: SkillType,
    *,
    index_types: List[str] = (),
) -> SkillMetadata:
    reqs = [
        compiler_resources.IndexRequirement(index_type=t) for t in index_types
    ]
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
    reg.register(_make_meta("regex_search", SkillType.RETRIEVAL))
    reg.register(
        _make_meta("graph_expand", SkillType.EXPAND, index_types=["symbol_graph"])
    )
    reg.register(_make_meta("query_transform", SkillType.TRANSFORM))
    return reg


# ---------------------------------------------------------------------------
# required_index_types
# ---------------------------------------------------------------------------


def test_required_types_empty(registry):
    assert skill_context.required_index_types([], skill_registry=registry) == set()


def test_required_types_single(registry):
    got = skill_context.required_index_types(
        ["bm25_search"], skill_registry=registry
    )
    assert got == {"bm25"}


def test_required_types_union(registry):
    got = skill_context.required_index_types(
        ["bm25_search", "embedding_search", "graph_expand"],
        skill_registry=registry,
    )
    assert got == {"bm25", "vector", "symbol_graph"}


def test_required_types_skips_no_requirement_skills(registry):
    """``regex_search`` and ``query_transform`` declare no index_requirements;
    they must not contaminate the union."""
    got = skill_context.required_index_types(
        ["regex_search", "query_transform"], skill_registry=registry
    )
    assert got == set()


def test_required_types_unknown_skill_silently_ignored(registry):
    """Callers that need to fail loudly should check membership themselves."""
    got = skill_context.required_index_types(
        ["bm25_search", "does_not_exist"], skill_registry=registry
    )
    assert got == {"bm25"}


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


def test_build_returns_expand_for_graph_expand(registry, mocked_build, tmp_path):
    contexts = skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["graph_expand"],
        cache_dir=str(tmp_path / "cache"),
        skill_registry=registry,
    )
    assert set(contexts.keys()) == {"expand"}
    assert contexts["expand"].code_graph is not None


def test_build_returns_both_keys_for_mixed_skills(
    registry, mocked_build, tmp_path
):
    contexts = skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["bm25_search", "embedding_search", "graph_expand"],
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
    """``regex_search`` declares no index_requirements but is a RETRIEVAL
    skill. When requested alongside ``bm25_search``, it shares the
    ``"retrieve"`` context that bm25 forced into existence."""
    contexts = skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["bm25_search", "regex_search"],
        cache_dir=str(tmp_path / "cache"),
        skill_registry=registry,
    )
    assert "retrieve" in contexts


def test_transform_skill_alone_returns_empty_dict(
    registry, mocked_build, tmp_path
):
    """``query_transform`` is TRANSFORM type with no index requirements.
    Currently no transform context is plumbed through — the dict stays
    empty rather than fabricating a useless entry."""
    contexts = skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["query_transform"],
        cache_dir=str(tmp_path / "cache"),
        skill_registry=registry,
    )
    assert contexts == {}


def test_already_built_index_is_not_rebuilt(
    registry, mocked_build, tmp_path
):
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


def test_missing_index_triggers_build(
    registry, mocked_build, tmp_path
):
    """Empty cache → compiler is invoked for the missing type."""
    skill_context.build_skill_contexts(
        repo_path=str(tmp_path),
        skill_ids=["bm25_search"],
        cache_dir=str(tmp_path / "cache"),
        skill_registry=registry,
    )
    assert mocked_build["compile"] == [("bm25",)]
    assert mocked_build["loaded"] == ["bm25"]


def test_rebuild_forces_full_recompile(
    registry, mocked_build, tmp_path
):
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


def test_partial_cache_only_rebuilds_missing(
    registry, mocked_build, tmp_path
):
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
