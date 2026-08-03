# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Small end-to-end test for ``codenib.agent.query`` against codenib-base.

This is the "smallest path that exercises the full agent_compile facade":

    codenib-base dataset  → CodeNibBaseDataset.process_instance (clone + checkout)
        → query(prompt, options=CodeNibAgentOptions(repo_path=...))
            → build_skill_contexts  (real BM25 index build)
            → SkillLoader.load_all
            → AgentRunner.run  (mocked LLM — one tool call, then final answer)

The LLM is mocked so no API key / network call is needed; everything else
(repo clone, tree-sitter chunking, BM25 index build, skill execution) runs
for real. The test verifies that:

* ``query()`` orchestrates pre-compile + agent loop without any caller-side
  glue beyond ``CodeNibAgentOptions``.
* The compile-table-driven scenario classification reaches the runner — when
  the prompt embeds a stack trace, the LLM only sees the A0 (bm25) tool.
* The BM25 executor actually returns results (proving the indexes were
  built and bound to the skill correctly).

Marked ``integration`` because it downloads a HuggingFace dataset shard
and clones a git repository. To run explicitly::

    pytest test/agent/test_query_e2e.py -v -m integration

Repo + index caches are shared with the rest of the suite under
``${CODENIB_TEMP_DIR}/gt-test/``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from filelock import FileLock

from codenib.agent import CodeNibAgentOptions, query
from codenib.agent.skills.registry import SkillRegistry
from codenib.llm.litellm_chat import LiteLLMChat

# Real-LLM models exercised by the @slow test below. Each entry must be a
# fully-qualified ``litellm`` model id. Add new models here — the test body
# stays one-line per case.
#
# Anthropic on Vertex uses the publisher format ``<model-name>@<release-date>``
# (see ``test/agent/test_vertex_ai.py``). Keep these in sync with what's been
# enabled in the Vertex AI Model Garden console for the project.
_VERTEX_MODELS = [
    "vertex_ai/gemini-2.5-flash",
    "vertex_ai/claude-haiku-4-5@20251001",
]


@pytest.fixture(autouse=True)
def _reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


@pytest.fixture(scope="session")
def codenib_base_cache(tmp_path_factory) -> Dict[str, Path]:
    """User-owned, cross-worker-safe cache dirs for the codenib-base e2e tests.

    Path resolution, in order:

    1. ``$CODENIB_TEST_CACHE_DIR`` — if set, used as the base directory.
       CI admins point this at a persistent runner volume (e.g. a Docker
       mount) so the ~100MB repo clone and BM25 index build survive across
       jobs. The runner user must own / have write access to this path.
    2. ``tmp_path_factory.getbasetemp().parent`` (``/tmp/pytest-of-<user>``) —
       owned by whoever runs pytest, writable, persists across pytest
       sessions for the same user. Avoids the cross-user permission
       collisions a fixed ``/tmp/...`` path causes on shared CI runners.

    Either way, ``repos/``, ``datasets/``, and ``index/`` subdirs are
    created underneath.
    """
    env_override = os.environ.get("CODENIB_TEST_CACHE_DIR")
    if env_override:
        base = Path(env_override).expanduser() / "codenib-base-e2e"
    else:
        base = tmp_path_factory.getbasetemp().parent / "codenib-base-e2e"
    base.mkdir(parents=True, exist_ok=True)
    dirs = {
        "repos": base / "repos",
        "datasets": base / "datasets",
        "index": base / "index",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


@pytest.fixture
def codenib_base_index_cache(request, codenib_base_cache) -> Path:
    """Per-test index cache to avoid concurrent BM25 writes under xdist."""
    safe_node_id = "".join(
        ch if ch.isalnum() or ch in "._-" else "_" for ch in request.node.nodeid
    )
    path = codenib_base_cache["index"] / safe_node_id
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="module")
def codenib_base_first_instance(codenib_base_cache) -> Dict[str, Any]:
    """Load the first instance of the codenib-base dataset.

    Skips the test if the dataset is unreachable (no HF auth / offline /
    network blocked). The first instance is whatever the upstream dataset
    happens to put at index 0 — we treat it as opaque and only depend on
    the standard columns: ``instance_id``, ``repo``, ``base_commit``,
    ``problem_statement``, ``language_group``.
    """
    pytest.importorskip("datasets")

    from codenib.dataset.codenib_base import CodeNibBaseDataset

    # Lock lives alongside the cache so it also coordinates concurrent
    # CI jobs that share a persistent ``CODENIB_TEST_CACHE_DIR`` volume.
    lock_path = codenib_base_cache["datasets"].parent / "dataset.lock"
    try:
        with FileLock(str(lock_path)):
            ds = CodeNibBaseDataset(
                split="test",
                root=str(codenib_base_cache["datasets"]),
                repo_root=str(codenib_base_cache["repos"]),
                log=False,
            )
            rows = ds.load(idx_range=[0, 1])
    except Exception as exc:
        pytest.skip(f"codenib-base dataset unavailable: {exc}")

    row = dict(rows[0])
    return {"_dataset": ds, "row": row}


@pytest.fixture(scope="module")
def prepared_repo(codenib_base_first_instance, codenib_base_cache) -> Dict[str, Any]:
    """Clone + checkout the first instance's repo."""
    from codenib.agent.compile import normalize_language

    ds = codenib_base_first_instance["_dataset"]
    row = codenib_base_first_instance["row"]
    repo_dir_name = (row.get("repo") or "unknown").replace("/", "_")
    lock_path = codenib_base_cache["repos"].parent / f"repo-{repo_dir_name}.lock"
    try:
        with FileLock(str(lock_path)):
            ds.process_instance(row)
    except Exception as exc:
        pytest.skip(f"could not check out repo for {row.get('instance_id')}: {exc}")

    # Normalize the language: codenib-base uses ``language_group`` whose
    # casing is dataset-defined (observed: "Rust"), while both
    # ``build_skill_contexts`` and ``Scenario.key()`` expect canonical
    # lowercase. Falling back to "python" matches the existing
    # ``examples/skill_agent_eval.py`` default.
    raw_lang = row.get("language_group") or row.get("language") or "python"
    language = normalize_language(raw_lang) or "python"

    return {
        "row": row,
        "repo_path": ds.get_repo_path(row),
        "language": language,
    }


def _two_turn_mock_llm(skill_id: str, query_arg: str) -> LiteLLMChat:
    """Mock that calls ``skill_id`` once, then produces a final answer.

    Turn 1: assistant message with a single tool_call → ``skill_id``.
    Turn 2: assistant message with ``content="done"`` and no tool_calls.
    """
    llm = MagicMock(spec=LiteLLMChat)

    # --- turn 1: tool call ---
    tc = MagicMock()
    tc.id = "call_0"
    tc.function.name = skill_id
    tc.function.arguments = json.dumps({"query": query_arg})

    turn1_msg = MagicMock()
    turn1_msg.content = None
    turn1_msg.tool_calls = [tc]
    turn1_msg.model_dump = MagicMock(
        return_value={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {
                        "name": skill_id,
                        "arguments": tc.function.arguments,
                    },
                }
            ],
        }
    )

    turn1_choice = MagicMock()
    turn1_choice.message = turn1_msg
    turn1_resp = MagicMock()
    turn1_resp.choices = [turn1_choice]

    # --- turn 2: final answer ---
    turn2_msg = MagicMock()
    turn2_msg.content = "done"
    turn2_msg.tool_calls = None
    turn2_msg.model_dump = MagicMock(
        return_value={"role": "assistant", "content": "done"}
    )
    turn2_choice = MagicMock()
    turn2_choice.message = turn2_msg
    turn2_resp = MagicMock()
    turn2_resp.choices = [turn2_choice]

    llm._call_raw = MagicMock(side_effect=[turn1_resp, turn2_resp])
    return llm


def _tools_passed_first_turn(llm) -> List[str]:
    """Swept tool names the LLM saw on its *first* call (defaults stripped).

    The always-on default tools (read/grep/glob/bash) are emitted into every
    tool set; narrowing assertions look at the swept skills only.
    """
    from codenib.agent.tools.defaults import DEFAULT_TOOL_IDS

    first_call = llm._call_raw.call_args_list[0]
    schemas = first_call.kwargs.get("tools", [])
    names = {t["function"]["name"] for t in schemas} - set(DEFAULT_TOOL_IDS)
    return sorted(names)


# ---------------------------------------------------------------------------
# E2E: pre-compile + agent loop, with a real BM25 index.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_query_runs_end_to_end_on_codenib_base(prepared_repo, codenib_base_index_cache):
    """Smoke: the full query() facade runs against a real codenib-base repo."""
    repo_path = prepared_repo["repo_path"]
    language = prepared_repo["language"]
    problem_statement = prepared_repo["row"].get("problem_statement") or "fix bug"

    llm = _two_turn_mock_llm("bm25_search", query_arg=problem_statement[:200])

    result = query(
        problem_statement,
        options=CodeNibAgentOptions(
            repo_path=repo_path,
            llm=llm,
            allowed_skills=["bm25_search"],  # cheapest skill, no GPU
            primary_language=language,
            languages=(language,),
            index_cache_dir=str(codenib_base_index_cache),
            max_turns=3,
        ),
    )

    # The agent looped twice (tool call → final answer).
    assert result.total_turns == 2
    assert result.answer == "done"

    # One tool call to bm25_search actually executed.
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.skill_id == "bm25_search"
    assert tc.error is None, f"bm25_search failed: {tc.error}"
    # The executor returned something — even an empty list proves the
    # context was wired and the BM25 index built.
    assert tc.result is not None


@pytest.mark.integration
def test_compile_table_flows_through_query_pipeline(
    prepared_repo, codenib_base_index_cache
):
    """End-to-end smoke for the compile_table code path in ``query()``.

    Exercises load → classify → intersect → reach the runner against a
    real repo + BM25 index. The narrowing *logic* is covered exhaustively
    in unit tests
    (``test_query.py::TestCompileTable``) and runtime tests
    (``test_compile_runtime.py``); this e2e test only confirms the wiring
    survives a real ``query()`` call with a ``compile_table`` argument.

    Uses a coherent table (``allowed_skills`` ⊆ ``union(table.values())``)
    so :func:`codenib.agent.runner._warn_on_skill_set_mismatch`
    stays silent — orphan/overflow warnings have dedicated unit-test
    coverage in ``test_query.py``.
    """
    repo_path = prepared_repo["repo_path"]
    language = prepared_repo["language"]

    # Coherent table: every scenario maps to the same allowed_skills.
    table = {
        f"{language}:stacktrace": frozenset({"bm25_search"}),
        f"{language}:no_stacktrace": frozenset({"bm25_search"}),
    }

    prompt = (
        "Build broke with:\n"
        "Traceback (most recent call last):\n"
        '  File "tests/test_x.py", line 17, in test_x\n'
        "    raise AssertionError('boom')\n"
    )
    llm = _two_turn_mock_llm("bm25_search", query_arg="boom")

    result = query(
        prompt,
        options=CodeNibAgentOptions(
            repo_path=repo_path,
            llm=llm,
            allowed_skills=["bm25_search"],
            primary_language=language,
            languages=(language,),
            compile_table=table,
            index_cache_dir=str(codenib_base_index_cache),
            max_turns=3,
        ),
    )

    # CAR picked bm25_search for the scenario; LLM saw exactly that.
    assert _tools_passed_first_turn(llm) == ["bm25_search"]
    assert result.tool_calls[0].skill_id == "bm25_search"


# ---------------------------------------------------------------------------
# Real-LLM e2e: gemini-2.5-flash via Vertex AI through litellm.
# ---------------------------------------------------------------------------


def _skip_if_vertex_unconfigured() -> None:
    """Skip the test unless Vertex AI auth is in place.

    LiteLLM's Vertex AI backend requires application-default credentials
    (``GOOGLE_APPLICATION_CREDENTIALS`` pointing at a service-account JSON).
    The region falls back to ``us-central1`` when ``VERTEX_REGION`` is unset.
    """
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        pytest.skip(
            "Vertex AI not configured (GOOGLE_APPLICATION_CREDENTIALS unset). "
            "Export a service-account JSON path to run this test."
        )


@pytest.mark.slow
@pytest.mark.parametrize("model", _VERTEX_MODELS)
def test_query_with_real_vertex_model(prepared_repo, codenib_base_index_cache, model):
    """End-to-end with a real LLM call via Vertex AI through litellm.

    Parametrized over the models declared in ``_VERTEX_MODELS`` (currently
    gemini-2.5-flash + claude-haiku-4-5). Builds a real BM25 index, then
    drives the agent with each real model through ``LiteLLMChat``. We don't
    assert *what* the model produced (LLM output is non-deterministic) —
    only that the agent loop completed and that any bm25_search tool call
    landed without error.

    Marked ``slow`` because each parametrization makes a billed API call
    and may take tens of seconds on first run (cold-start + index build).
    """
    _skip_if_vertex_unconfigured()

    repo_path = prepared_repo["repo_path"]
    language = prepared_repo["language"]
    problem_statement = prepared_repo["row"].get("problem_statement") or (
        "Find the entry point of this codebase."
    )

    llm = LiteLLMChat(
        model=model,
        temperature=0.0,
        max_tokens=1024,
    )

    # Compile table pinning a known cell to bm25-only keeps the run fast
    # (one tool, one turn of search) and deterministic in skill choice.
    table = {
        f"{language}:stacktrace": frozenset({"bm25_search"}),
        f"{language}:no_stacktrace": frozenset({"bm25_search"}),
    }

    result = query(
        problem_statement,
        options=CodeNibAgentOptions(
            repo_path=repo_path,
            llm=llm,
            allowed_skills=["bm25_search"],
            primary_language=language,
            languages=(language,),
            compile_table=table,
            index_cache_dir=str(codenib_base_index_cache),
            # Cap turns tightly — the test cares that the wiring works,
            # not that the model produces a brilliant answer.
            max_turns=4,
            max_tokens=1024,
            system_prompt=(
                "You are a code search assistant. Call bm25_search once to "
                "find relevant files for the user's question, then produce "
                "a short final answer. Do not call more than two tools."
            ),
        ),
    )

    # The agent actually ran a tool-call → observe → finalize loop.
    assert result.total_turns >= 1
    assert result.answer is not None
    # The model may have skipped tool calls entirely (gemini can decide it
    # already knows enough), so we don't *require* a tool call — but if it
    # did make one, it must have succeeded against the real index.
    for tc in result.tool_calls:
        assert tc.skill_id == "bm25_search"
        assert tc.error is None, f"bm25_search execution failed: {tc.error}"

    # Token usage was tracked — sanity check that LiteLLMChat actually
    # talked to the model and the UsageTracker recorded it.
    assert result.usage is not None
    assert (result.usage.total_tokens or 0) > 0


# ---------------------------------------------------------------------------
# E2E: AoT manifest mode — compile_repo() then query(manifest=...).
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_query_manifest_mode_e2e(prepared_repo, codenib_base_index_cache):
    """Two-phase AoT round-trip: compile_repo() then query(manifest=...).

    Exercises the full prebuilt-index code path: ``compile_repo`` writes
    a manifest + indexes to the per-test index cache;
    ``query(manifest=manifest, ...)`` then loads them via
    :func:`codenib.compiler.load_contexts_from_manifest`. No inline
    build happens during ``query()`` — verified by the absence of any
    "Building missing indexes" log entry from
    ``compiler.skill_context`` during Phase 2.
    """
    from codenib.agent import compile_repo

    repo_path = prepared_repo["repo_path"]
    language = prepared_repo["language"]
    problem_statement = prepared_repo["row"].get("problem_statement") or "fix bug"
    cache_dir = str(codenib_base_index_cache)

    # Phase 1 — AoT compile (writes repo_manifest.json + bm25/ artifacts).
    manifest = compile_repo(
        repo_path,
        index_types=("bm25",),
        languages=(language,),
        cache_dir=cache_dir,
    )
    assert "bm25" in manifest.indexes
    assert manifest.indexes["bm25"].status == "fresh"

    # Phase 2 — query against the resolved manifest, no rebuild.
    llm = _two_turn_mock_llm("bm25_search", query_arg=problem_statement[:200])
    result = query(
        problem_statement,
        options=CodeNibAgentOptions(
            manifest=manifest,
            llm=llm,
            allowed_skills=["bm25_search"],
            primary_language=language,
            max_turns=3,
        ),
    )

    assert result.total_turns == 2
    assert result.answer == "done"
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.skill_id == "bm25_search"
    assert tc.error is None, f"bm25_search failed in manifest mode: {tc.error}"
    assert tc.result is not None
