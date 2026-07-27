# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the query-facing repository_search skill."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codenib.agent.skills.context import ComposerContexts
from codenib.agent.skills.loader import SkillLoader
from codenib.ops.retrieve import RetrieveContext
from codenib.types import NodeInfo


def _node(
    node_id: str,
    file: str,
    *,
    name: str | None = None,
    score: float = 1.0,
) -> NodeInfo:
    return NodeInfo(
        node_id=node_id,
        node_name=name or node_id,
        file=file,
        start_line=1,
        end_line=5,
        score=score,
        content=f"source for {node_id}",
    )


def _executor(*, sparse=None, dense=None):
    from codenib.agent.skills.repository_search.executor import create_executor

    bm25 = None
    if sparse is not None:
        bm25 = MagicMock()
        bm25.search.return_value = sparse
    vector = None
    if dense is not None:
        vector = MagicMock()
        vector.search_with_content.return_value = dense
    context = RetrieveContext(bm25=bm25, vector_store=vector, default_level="l2")
    return create_executor(ComposerContexts(retrieve=context)), bm25, vector


def test_fuses_sparse_and_dense_rankings_by_reciprocal_rank():
    shared = _node("shared", "codenib/runtime.py")
    execute, bm25, vector = _executor(
        sparse=[
            _node("lexical", "codenib/compiler.py"),
            shared,
        ],
        dense=[
            _node("semantic", "codenib/manifest.py"),
            shared,
        ],
    )

    results = execute("stale semantic index checks", top_k=3)

    assert [node.node_id for node in results] == [
        "shared",
        "lexical",
        "semantic",
    ]
    bm25.search.assert_called_once_with(
        query="stale semantic index checks",
        top_k=20,
        return_code_content=True,
        wrap_with_ln=True,
        filter_test=False,
    )
    vector.search_with_content.assert_called_once_with(
        query="stale semantic index checks",
        top_k=20,
        level="l2",
    )


def test_reserves_space_for_evidence_unique_to_each_retrieval_branch():
    consensus = [
        _node(f"shared-{index}", f"codenib/shared_{index}.py") for index in range(4)
    ]
    execute, _, _ = _executor(
        sparse=[
            *consensus,
            _node("lexical-only", "codenib/compiler.py"),
        ],
        dense=[
            *consensus,
            _node("dense-only", "codenib/web/repo_registry.py"),
        ],
    )

    results = execute("stale vector runtime loading", top_k=4)

    assert [node.node_id for node in results] == [
        "shared-0",
        "shared-1",
        "lexical-only",
        "dense-only",
    ]


def test_degrades_to_sparse_search_when_vector_view_is_absent():
    execute, _, vector = _executor(
        sparse=[_node("runtime", "src/runtime.py")],
        dense=None,
    )

    results = execute("runtime")

    assert [node.node_id for node in results] == ["runtime"]
    assert vector is None


def test_omits_supporting_evidence_by_default():
    execute, _, _ = _executor(
        sparse=[
            _node("test", "tests/test_runtime.py"),
            _node("docs", "docs/runtime.md"),
            _node("evaluation", "codenib/eval/runtime_benchmark.py"),
            _node("smoke", "scripts/smoke_release.py"),
            _node("script", "scripts/build_qa_index.py"),
            _node("generated", "dist/runtime.js"),
            _node("runtime", "codenib/runtime.py"),
        ]
    )

    results = execute("runtime")

    assert [node.node_id for node in results] == ["runtime"]


def test_can_include_supporting_evidence_explicitly():
    execute, _, _ = _executor(
        sparse=[
            _node("test", "tests/test_runtime.py"),
            _node("runtime", "codenib/runtime.py"),
        ]
    )

    results = execute("runtime tests", include_supporting=True)

    assert [node.node_id for node in results] == ["test", "runtime"]


def test_exact_symbol_query_adds_call_site_retrieval():
    definition = _node(
        "definition",
        "src/manifest.py",
        name="RepoManifest.index_is_current",
    )
    call_site = _node(
        "call-site",
        "src/runtime.py",
        name="Runtime.load_vector",
    )
    bm25 = MagicMock()
    bm25.search.return_value = [definition]
    bm25.search_identifier_occurrences.return_value = [call_site]
    vector = MagicMock()
    vector.search_with_content.return_value = [definition]
    from codenib.agent.skills.repository_search.executor import create_executor

    execute = create_executor(
        ComposerContexts(
            retrieve=RetrieveContext(
                bm25=bm25,
                vector_store=vector,
                default_level="l2",
            )
        )
    )

    results = execute("Where is RepoManifest.index_is_current() used?", top_k=2)

    assert [node.node_id for node in results] == ["definition", "call-site"]
    bm25.search_identifier_occurrences.assert_called_once_with(
        "index_is_current",
        context_query="Where is RepoManifest.index_is_current() used?",
        top_k=20,
        wrap_with_ln=True,
        filter_test=False,
    )


def test_exact_symbol_definition_precedes_wrappers_that_reference_it():
    definition = _node(
        "definition",
        "codenib/compiler/index_compiler.py",
        name="IndexCompiler._compile",
    )
    compile_wrapper = _node(
        "compile-wrapper",
        "codenib/compiler/index_compiler.py",
        name="IndexCompiler.compile_repo",
    )
    update_wrapper = _node(
        "update-wrapper",
        "codenib/compiler/index_compiler.py",
        name="IndexCompiler.update_repo",
    )
    bm25 = MagicMock()
    bm25.search.return_value = [compile_wrapper, update_wrapper, definition]
    bm25.search_identifier_occurrences.return_value = [
        compile_wrapper,
        update_wrapper,
    ]
    from codenib.agent.skills.repository_search.executor import create_executor

    execute = create_executor(
        ComposerContexts(
            retrieve=RetrieveContext(
                bm25=bm25,
                vector_store=None,
                default_level="l2",
            )
        )
    )

    results = execute("IndexCompiler._compile manifest.save", top_k=2)

    assert [node.node_id for node in results] == [
        "definition",
        "compile-wrapper",
    ]


def test_persistence_query_expands_to_save_definition_and_call_site():
    save_definition = _node(
        "save-definition",
        "codenib/compiler/manifest.py",
        name="RepoManifest.save",
    )
    save_call_site = _node(
        "save-call-site",
        "codenib/compiler/index_compiler.py",
        name="IndexCompiler._compile",
    )
    bm25 = MagicMock()
    bm25.search.return_value = [save_definition]
    bm25.search_identifier_occurrences.return_value = [save_call_site]
    from codenib.agent.skills.repository_search.executor import create_executor

    execute = create_executor(
        ComposerContexts(
            retrieve=RetrieveContext(
                bm25=bm25,
                vector_store=None,
                default_level="l2",
            )
        )
    )

    results = execute("How is the repository manifest persisted?", top_k=2)

    assert [node.node_id for node in results] == [
        "save-definition",
        "save-call-site",
    ]
    bm25.search.assert_called_once_with(
        query="How is the repository manifest persisted? save serialize write",
        top_k=20,
        return_code_content=True,
        wrap_with_ln=True,
        filter_test=False,
    )
    bm25.search_identifier_occurrences.assert_called_once_with(
        "save",
        context_query="How is the repository manifest persisted?",
        top_k=20,
        wrap_with_ln=True,
        filter_test=False,
    )


def test_mechanism_query_expands_a_returned_predicate_to_call_sites():
    predicate = _node(
        "predicate",
        "src/manifest.py",
        name="RepoManifest.index_is_current",
    )
    loader = _node(
        "loader",
        "src/runtime.py",
        name="Runtime.load_vector",
    )
    bm25 = MagicMock()
    bm25.search.return_value = [_node("generic", "src/status.py")]
    bm25.search_identifier_occurrences.return_value = [loader]
    vector = MagicMock()
    vector.search_with_content.return_value = [predicate]
    from codenib.agent.skills.repository_search.executor import create_executor

    execute = create_executor(
        ComposerContexts(
            retrieve=RetrieveContext(
                bm25=bm25,
                vector_store=vector,
                default_level="l2",
            )
        )
    )

    results = execute(
        "How does runtime prevent serving a stale semantic index?",
        top_k=3,
    )

    assert [node.node_id for node in results] == [
        "loader",
        "generic",
        "predicate",
    ]
    bm25.search_identifier_occurrences.assert_called_once_with(
        "index_is_current",
        context_query="How does runtime prevent serving a stale semantic index?",
        top_k=20,
        wrap_with_ln=True,
        filter_test=False,
    )


def test_projects_large_chunks_without_losing_query_matching_lines():
    lines = [f"{line:03d} | filler = {line}" for line in range(140)]
    lines[120] = "120 | if not manifest.index_is_current('vector'):"
    large = _node("large", "src/runtime.py").model_copy(
        update={"content": "\n".join(lines)}
    )
    execute, _, _ = _executor(sparse=[large])

    results = execute(
        "How does runtime check whether a vector index is current?",
        top_k=10,
    )

    assert len(results[0].content) <= 1000
    assert "000 | filler" in results[0].content
    assert "manifest.index_is_current" in results[0].content
    assert "source lines omitted" in results[0].content


@pytest.mark.parametrize("top_k", [0, 21, "many"])
def test_rejects_invalid_result_budget(top_k):
    execute, _, _ = _executor(sparse=[])

    with pytest.raises(ValueError, match="top_k"):
        execute("runtime", top_k=top_k)


def test_raises_when_no_repository_view_is_available():
    from codenib.agent.skills.repository_search.executor import create_executor

    execute = create_executor(
        ComposerContexts(retrieve=RetrieveContext(bm25=None, vector_store=None))
    )

    with pytest.raises(RuntimeError, match="indexes are not available"):
        execute("runtime")


def test_skill_package_declares_vector_as_optional():
    import codenib.agent.skills as skills_package

    skill_dir = Path(skills_package.__file__).parent / "repository_search"
    metadata = SkillLoader().load_skill(
        str(skill_dir),
        contexts={
            "retrieve": RetrieveContext(
                bm25=MagicMock(),
                vector_store=None,
            )
        },
    )

    assert metadata is not None
    assert metadata.skill_id == "repository_search"
    requirements = {req.index_type: req for req in metadata.index_requirements}
    assert requirements["bm25"].required is True
    assert requirements["vector"].required is False
