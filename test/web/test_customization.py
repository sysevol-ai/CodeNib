# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for wiki customization: the store, the transform, and the endpoints."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import codenib.web.app as web_app
from codenib.web.customization_store import (
    SCOPE_PAGE,
    SCOPE_WIKI,
    CustomizationStore,
    Prior,
)
from codenib.wiki.customizer import Customizer, clean_structure

# --------------------------------------------------------------------------
# CustomizationStore
# --------------------------------------------------------------------------


class TestCustomizationStore:
    def test_resolve_none_without_priors(self):
        assert CustomizationStore().resolve("overview") is None

    def test_page_only_prior(self):
        s = CustomizationStore()
        s.set_prior(SCOPE_PAGE, "overview", Prior(instruction="blog"))
        assert s.resolve("overview").instruction == "blog"
        assert s.resolve("other") is None

    def test_wiki_prior_applies_to_every_page(self):
        s = CustomizationStore()
        s.set_prior(SCOPE_WIKI, "", Prior(instruction="house style"))
        assert s.resolve("overview").instruction == "house style"
        assert s.resolve("anything").instruction == "house style"

    def test_cascade_merges_wiki_then_page(self):
        s = CustomizationStore()
        s.set_prior(SCOPE_WIKI, "", Prior(instruction="house"))
        s.set_prior(SCOPE_PAGE, "overview", Prior(instruction="page tweak"))
        merged = s.resolve("overview")
        assert merged.instruction == "house\n\npage tweak"

    def test_page_structure_overrides_wiki_structure(self):
        s = CustomizationStore()
        s.set_prior(SCOPE_WIKI, "", Prior(structure=("A", "B")))
        s.set_prior(SCOPE_PAGE, "overview", Prior(structure=("X",)))
        assert s.resolve("overview").structure == ("X",)

    def test_empty_prior_clears(self):
        s = CustomizationStore()
        s.set_prior(SCOPE_PAGE, "overview", Prior(instruction="x"))
        s.set_prior(SCOPE_PAGE, "overview", Prior())  # empty -> clear
        assert s.resolve("overview") is None

    def test_drop_prior(self):
        s = CustomizationStore()
        s.set_prior(SCOPE_PAGE, "overview", Prior(instruction="x"))
        assert s.drop_prior(SCOPE_PAGE, "overview") is True
        assert s.drop_prior(SCOPE_PAGE, "overview") is False
        assert s.resolve("overview") is None

    def test_transformed_memoizes_by_prior(self):
        s = CustomizationStore()
        prior = Prior(instruction="blog")
        s.set_prior(SCOPE_PAGE, "overview", prior)
        calls = {"n": 0}

        def produce():
            calls["n"] += 1
            return "OUT"

        r1 = s.transformed("overview", "SRC", prior, produce)
        r2 = s.transformed("overview", "SRC", prior, produce)
        assert r1 == r2 == "OUT"
        assert calls["n"] == 1  # second read hits the memo

    def test_transformed_reruns_when_source_changes(self):
        s = CustomizationStore()
        prior = Prior(instruction="blog")
        s.set_prior(SCOPE_PAGE, "overview", prior)
        calls = {"n": 0}

        def produce():
            calls["n"] += 1
            return f"OUT{calls['n']}"

        s.transformed("overview", "SRC-A", prior, produce)
        s.transformed("overview", "SRC-B", prior, produce)
        assert calls["n"] == 2  # different source markdown -> re-transform

    def test_ttl_expiry(self):
        s = CustomizationStore(ttl_seconds=0)  # everything is immediately stale
        s.set_prior(SCOPE_PAGE, "overview", Prior(instruction="x"))
        # A subsequent access sweeps the expired entry.
        assert s.resolve("overview") is None

    def test_lru_cap_evicts_oldest(self):
        s = CustomizationStore(max_entries=2)
        s.set_prior(SCOPE_PAGE, "a", Prior(instruction="1"))
        s.set_prior(SCOPE_PAGE, "b", Prior(instruction="2"))
        s.set_prior(SCOPE_PAGE, "c", Prior(instruction="3"))
        assert s.size() == 2
        # "a" was least-recently-used and should be gone.
        assert s.resolve("a") is None
        assert s.resolve("c") is not None

    def test_unknown_scope_rejected(self):
        s = CustomizationStore()
        try:
            s.set_prior("nonsense", "x", Prior(instruction="y"))
        except ValueError:
            return
        raise AssertionError("expected ValueError for unknown scope")


# --------------------------------------------------------------------------
# Customizer
# --------------------------------------------------------------------------


class _FakeLLM:
    def __init__(self, out="# Rewritten [1]"):
        self.out = out
        self.last_prompt = None

    def complete(self, messages, **kw):
        self.last_prompt = messages[-1]["content"]
        return self.out


class _BoomLLM:
    def complete(self, *a, **k):
        raise RuntimeError("model down")


class TestCustomizer:
    def test_empty_prior_is_noop_without_llm_call(self):
        llm = _FakeLLM()
        assert Customizer(llm).apply("# Orig [1]") == "# Orig [1]"
        assert llm.last_prompt is None  # never called

    def test_transform_returns_model_output(self):
        out = Customizer(_FakeLLM("# New [1]")).apply("# Orig [1]", instruction="blog")
        assert out == "# New [1]"

    def test_fail_soft_returns_original(self):
        assert (
            Customizer(_BoomLLM()).apply("# Keep [1]", instruction="x") == "# Keep [1]"
        )

    def test_prompt_carries_rules_and_structure(self):
        llm = _FakeLLM()
        Customizer(llm).apply(
            "body [1]", instruction="blog", structure=["Intro", "Gotchas"]
        )
        assert "citation marker" in llm.last_prompt
        assert "Intro" in llm.last_prompt and "Gotchas" in llm.last_prompt

    def test_strips_markdown_code_fence(self):
        out = Customizer(_FakeLLM("```markdown\n# Fenced [1]\n```")).apply(
            "x", instruction="y"
        )
        assert out == "# Fenced [1]"

    def test_clean_structure_bounds(self):
        assert clean_structure(["  a ", "", "b"]) == ["a", "b"]
        assert clean_structure(None) == []


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


def _install_state(monkeypatch, *, page_markdown="# Default [1]"):
    """Wire app.state with a store + a fake customizer, and a fake wiki/bundle."""
    store = CustomizationStore()
    customizer = Customizer(_FakeLLM(f"# CUSTOM\n\n{page_markdown} restyled [1]"))
    monkeypatch.setattr(web_app.app.state, "customizations", store, raising=False)
    monkeypatch.setattr(web_app.app.state, "customizer", customizer, raising=False)

    class Wiki:
        def page(self, page_id):
            return {"id": page_id, "title": "Overview", "markdown": page_markdown}

    monkeypatch.setattr(web_app, "_wiki", lambda _r: Wiki())
    monkeypatch.setattr(
        web_app,
        "_bundle",
        lambda _r: SimpleNamespace(entry=SimpleNamespace(repo="o/r")),
    )

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(web_app.asyncio, "to_thread", fake_to_thread)
    return store


def test_customize_page_round_trip(monkeypatch):
    _install_state(monkeypatch)
    from codenib.web.schemas import CustomizeRequest

    res = asyncio.run(
        web_app.customize_wiki(
            "repo",
            CustomizeRequest(scope="page", target="overview", instruction="blog"),
        )
    )
    assert res.ok and res.customized
    assert "restyled" in res.markdown

    # The page endpoint now serves the customized markdown + flag.
    page = asyncio.run(web_app.wiki_page("repo", "overview"))
    assert page["customized"] is True
    assert "restyled" in page["markdown"]


def test_reset_restores_default(monkeypatch):
    _install_state(monkeypatch)
    from codenib.web.schemas import CustomizeRequest

    asyncio.run(
        web_app.customize_wiki(
            "repo",
            CustomizeRequest(scope="page", target="overview", instruction="blog"),
        )
    )
    asyncio.run(web_app.reset_customization("repo", scope="page", target="overview"))

    page = asyncio.run(web_app.wiki_page("repo", "overview"))
    assert page["customized"] is False
    assert page["markdown"] == "# Default [1]"


def test_empty_customize_clears(monkeypatch):
    store = _install_state(monkeypatch)
    from codenib.web.schemas import CustomizeRequest

    asyncio.run(
        web_app.customize_wiki(
            "repo",
            CustomizeRequest(scope="page", target="overview", instruction="blog"),
        )
    )
    # An empty instruction + no structure clears the prior.
    res = asyncio.run(
        web_app.customize_wiki(
            "repo", CustomizeRequest(scope="page", target="overview", instruction="")
        )
    )
    assert res.customized is False
    assert store.resolve("overview") is None


def test_wiki_scope_applies_lazily_on_view(monkeypatch):
    _install_state(monkeypatch)
    from codenib.web.schemas import CustomizeRequest

    res = asyncio.run(
        web_app.customize_wiki(
            "repo", CustomizeRequest(scope="wiki", instruction="house style")
        )
    )
    # Wiki scope stores the prior but returns no markdown (applies on view).
    assert res.ok and res.markdown == ""
    page = asyncio.run(web_app.wiki_page("repo", "overview"))
    assert page["customized"] is True
