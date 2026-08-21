# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``query()``'s AoT manifest mode.

Verifies the wiring of ``CodeNibAgentOptions.manifest`` — that
``query()`` accepts both ``RepoManifest`` instances and path strings,
short-circuits the inline build, threads the resolved manifest into
``AgentRunner``, and fails loudly when the manifest doesn't carry the
indexes required by ``allowed_skills``.

Heavy machinery (real BM25 build, real LLM) is mocked — these tests
target the SDK plumbing only. End-to-end coverage with a real index
build lives in ``test_query_e2e.py``.
"""

from __future__ import annotations

import time
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from codenib.agent import CodeNibAgentOptions, query
from codenib.agent.skills.registry import SkillRegistry
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.llm.litellm_chat import LiteLLMChat
from codenib.repository_source_selection import RepositorySourceSelection

_SOURCE_COMMIT = "d" * 40
_SOURCE_FINGERPRINT = "sha256-v2:" + ("e" * 64)
_SOURCE_SELECTION = RepositorySourceSelection()


@pytest.fixture(autouse=True)
def _reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


def _mock_llm_final_answer(answer: str = "done") -> LiteLLMChat:
    """LLM that immediately produces a final answer (no tool call)."""
    llm = MagicMock(spec=LiteLLMChat)
    assistant = MagicMock()
    assistant.content = answer
    assistant.tool_calls = None
    assistant.model_dump = MagicMock(
        return_value={"role": "assistant", "content": answer}
    )
    choice = MagicMock()
    choice.message = assistant
    response = MagicMock()
    response.choices = [choice]
    llm._call_raw = MagicMock(return_value=response)
    return llm


def _fake_manifest(
    *, with_bm25: bool = True, repo_path: str = "/tmp/fake-repo"
) -> RepoManifest:
    """Construct a minimal in-memory RepoManifest for wiring tests."""
    indexes: Dict[str, IndexEntry] = {}
    if with_bm25:
        indexes["bm25"] = IndexEntry(
            index_type="bm25",
            path="/tmp/fake-bm25-dir",  # never actually loaded in these tests
            built_at="2026-01-01T00:00:00Z",
            built_at_epoch=time.time(),
            status="fresh",
            commit=_SOURCE_COMMIT,
            source_fingerprint=_SOURCE_FINGERPRINT,
            source_selection_digest=_SOURCE_SELECTION.digest,
        )
    manifest = RepoManifest(
        repo_path=repo_path,
        commit=_SOURCE_COMMIT,
        last_indexed_commit=_SOURCE_COMMIT,
        source_fingerprint=_SOURCE_FINGERPRINT,
        last_indexed_source_fingerprint=_SOURCE_FINGERPRINT,
        source_selection=_SOURCE_SELECTION,
        last_indexed_source_selection_digest=_SOURCE_SELECTION.digest,
        languages=["python"],
        file_count=1,
        indexes=indexes,
    )
    manifest.derive_capabilities()
    return manifest


# ---------------------------------------------------------------------------
# Exactly-one-mode precondition
# ---------------------------------------------------------------------------


class TestExactlyOneModeEnforced:
    """``query()`` rejects 0 or 2+ index-source modes at entry."""

    def test_zero_modes_raises(self):
        with pytest.raises(ValueError, match="All three are unset"):
            query(
                "anything",
                options=CodeNibAgentOptions(llm=_mock_llm_final_answer()),
            )

    def test_two_modes_raises_repo_path_and_contexts(self):
        with pytest.raises(ValueError, match="exactly one"):
            query(
                "anything",
                options=CodeNibAgentOptions(
                    repo_path="/tmp/x",
                    contexts={},
                    llm=_mock_llm_final_answer(),
                ),
            )

    def test_two_modes_raises_manifest_and_repo_path(self):
        with pytest.raises(ValueError, match="exactly one"):
            query(
                "anything",
                options=CodeNibAgentOptions(
                    repo_path="/tmp/x",
                    manifest=_fake_manifest(),
                    llm=_mock_llm_final_answer(),
                ),
            )

    def test_three_modes_raises(self):
        with pytest.raises(ValueError, match="exactly one"):
            query(
                "anything",
                options=CodeNibAgentOptions(
                    repo_path="/tmp/x",
                    contexts={},
                    manifest=_fake_manifest(),
                    llm=_mock_llm_final_answer(),
                ),
            )

    def test_native_token_and_resolver_are_mutually_exclusive(self):
        with pytest.raises(ValueError, match="either native_index_authorization"):
            query(
                "anything",
                options=CodeNibAgentOptions(
                    repo_path="/tmp/x",
                    llm=_mock_llm_final_answer(),
                    native_index_authorization=object(),
                    native_index_authorization_resolver=lambda _entry: object(),
                ),
            )

    @pytest.mark.parametrize(
        "authority_options",
        [
            {"native_index_authorization": object()},
            {"native_index_authorization_resolver": lambda _entry: object()},
        ],
    )
    def test_contexts_mode_rejects_unused_authority_options(
        self,
        authority_options,
    ):
        with pytest.raises(ValueError, match="not accepted in contexts mode"):
            query(
                "anything",
                options=CodeNibAgentOptions(
                    contexts={},
                    llm=_mock_llm_final_answer(),
                    **authority_options,
                ),
            )


# ---------------------------------------------------------------------------
# Manifest mode wiring
# ---------------------------------------------------------------------------


class TestManifestMode:
    def test_manifest_mode_threads_native_authorization_resolver(self, monkeypatch):
        from codenib import compiler as compiler_pkg

        captured = {}

        def resolver(_entry):
            return object()

        def fake_load(*_args, **kwargs):
            captured.update(kwargs)
            return {}

        monkeypatch.setattr(compiler_pkg, "load_contexts_from_manifest", fake_load)

        query(
            "noop",
            options=CodeNibAgentOptions(
                manifest=_fake_manifest(),
                llm=_mock_llm_final_answer(),
                allowed_skills=["bm25_search"],
                native_index_authorization_resolver=resolver,
            ),
        )

        assert captured["native_index_authorization"] is None
        assert captured["native_index_authorization_resolver"] is resolver

    def test_manifest_mode_skips_build_skill_contexts(self, monkeypatch):
        """When ``manifest`` is set, the inline build path must never run.

        Even one accidental call to ``build_skill_contexts`` would defeat
        the purpose of AoT — we'd pay for re-indexing on every query.
        """
        from codenib import compiler as compiler_mod

        def _boom(*a, **kw):
            raise AssertionError("build_skill_contexts must not run in manifest mode")

        monkeypatch.setattr(compiler_mod, "build_skill_contexts", _boom)

        # Also short-circuit the loader so we don't try to open the fake
        # bm25 path on disk. Returning an empty contexts dict still
        # exercises the rest of query()'s pipeline.
        # Patch on the compiler package namespace — that's what
        # ``query()`` imports from via ``from ..compiler import ...``.
        from codenib import compiler as compiler_pkg

        monkeypatch.setattr(
            compiler_pkg, "load_contexts_from_manifest", lambda *a, **kw: {}
        )

        result = query(
            "noop",
            options=CodeNibAgentOptions(
                manifest=_fake_manifest(),
                llm=_mock_llm_final_answer(answer="ok"),
                allowed_skills=["bm25_search"],
            ),
        )
        assert result.answer == "ok"

    def test_manifest_path_str_loads_from_disk(self, tmp_path, monkeypatch):
        """A ``str`` path to repo_manifest.json is auto-loaded."""
        manifest = _fake_manifest()
        manifest_path = tmp_path / "repo_manifest.json"
        manifest.save(manifest_path)

        # Capture what AgentRunner.__init__ is given as manifest=
        captured: Dict[str, Any] = {}
        from codenib import compiler as compiler_pkg
        from codenib.agent import runner as runner_mod

        original_init = runner_mod.AgentRunner.__init__

        def _spy_init(self, *args, **kwargs):
            captured["manifest"] = kwargs.get("manifest")
            return original_init(self, *args, **kwargs)

        monkeypatch.setattr(runner_mod.AgentRunner, "__init__", _spy_init)
        monkeypatch.setattr(
            compiler_pkg, "load_contexts_from_manifest", lambda *a, **kw: {}
        )

        query(
            "noop",
            options=CodeNibAgentOptions(
                manifest=str(manifest_path),
                llm=_mock_llm_final_answer(),
                allowed_skills=["bm25_search"],
            ),
        )

        assert isinstance(captured["manifest"], RepoManifest)
        assert captured["manifest"].repo_path == manifest.repo_path
        assert "bm25" in captured["manifest"].indexes

    def test_manifest_path_must_exist(self):
        """A non-existent manifest path raises FileNotFoundError early."""
        with pytest.raises(FileNotFoundError, match="manifest path does not exist"):
            query(
                "noop",
                options=CodeNibAgentOptions(
                    manifest="/tmp/this-path-does-not-exist/repo_manifest.json",
                    llm=_mock_llm_final_answer(),
                    allowed_skills=["bm25_search"],
                ),
            )

    def test_manifest_threads_into_agent_runner(self, monkeypatch):
        """Resolved manifest instance reaches ``AgentRunner(manifest=...)``."""
        manifest = _fake_manifest()
        captured: Dict[str, Any] = {}

        from codenib import compiler as compiler_pkg
        from codenib.agent import runner as runner_mod

        original_init = runner_mod.AgentRunner.__init__

        def _spy_init(self, *args, **kwargs):
            captured["manifest"] = kwargs.get("manifest")
            return original_init(self, *args, **kwargs)

        monkeypatch.setattr(runner_mod.AgentRunner, "__init__", _spy_init)
        monkeypatch.setattr(
            compiler_pkg, "load_contexts_from_manifest", lambda *a, **kw: {}
        )

        query(
            "noop",
            options=CodeNibAgentOptions(
                manifest=manifest,
                llm=_mock_llm_final_answer(),
                allowed_skills=["bm25_search"],
            ),
        )

        assert captured["manifest"] is manifest

    def test_manifest_missing_required_index_raises(self):
        """``allowed_skills`` requires bm25 but manifest has no bm25 → ValueError.

        Loud failure at query-entry beats the deferred 'Skill not
        available' that would otherwise surface at tool-call time.
        """
        manifest = _fake_manifest(with_bm25=False)
        with pytest.raises(ValueError, match="missing required index"):
            query(
                "noop",
                options=CodeNibAgentOptions(
                    manifest=manifest,
                    llm=_mock_llm_final_answer(),
                    allowed_skills=["bm25_search"],
                ),
            )

    def test_manifest_type_error_on_garbage_input(self):
        with pytest.raises(TypeError, match="must be a RepoManifest"):
            query(
                "noop",
                options=CodeNibAgentOptions(
                    manifest=12345,  # type: ignore[arg-type]
                    llm=_mock_llm_final_answer(),
                    allowed_skills=["bm25_search"],
                ),
            )
