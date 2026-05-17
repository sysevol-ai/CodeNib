# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Integration test: AgentRunner + graph_expand skill + Vertex AI backend.

Uses the real httpie/cli repo (from conftest.py) with SCIP-python to
build a symbol graph, then lets the LLM agent explore it via graph_expand.

Run unit tests (no LLM, no SCIP)::

    conda run -n codeminer python -B -m pytest \
    test/agent/test_agent_graph_vertex.py::TestGraphExpandExecutor -xvs

Run integration tests (requires Vertex AI + scip-python)::

    conda run -n codeminer python -B -m pytest \
    test/agent/test_agent_graph_vertex.py -xvs -m integration
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeminer.agent.agent_types import AgentResult
from codeminer.agent.runner import AgentRunner
from codeminer.agent.skills.core import SkillInputSpec, SkillMetadata, SkillType
from codeminer.agent.skills.graph_expand.executor import create_executor
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.graph.code_graph import CodeGraph
from codeminer.llm.litellm_chat import LiteLLMChat
from codeminer.ops.expand import ExpandContext

pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# Real SCIP graph from httpie/cli repo
# ---------------------------------------------------------------------------

HTTPIE_GRAPH_DIR = Path("/tmp/httpie-cli-graph")
HTTPIE_GRAPH_FILE = HTTPIE_GRAPH_DIR / "graph.pkl"


@pytest.fixture(scope="session")
def httpie_graph(httpie_cli_repo) -> CodeGraph:
    """Build (or load cached) SCIP symbol graph for the httpie/cli repo.

    Uses ``LSIndexer`` (the unified language-server router) and
    ``skip_level="graph"`` so repeated runs reuse the cached graph.pkl.
    """
    from codeminer.ls_router import LSIndexer

    indexer = LSIndexer(
        project_root=str(httpie_cli_repo),
        output_dir=str(HTTPIE_GRAPH_DIR),
        language="python",
    )

    graph = indexer.run_pipeline(
        output_file=str(HTTPIE_GRAPH_FILE),
        skip_level="graph",
    )

    if graph is None:
        pytest.skip("SCIP pipeline failed — scip-python may not be available")

    print(f"\nhttpie graph: {len(graph.graph.vs)} nodes, {len(graph.graph.es)} edges")
    return graph


# ---------------------------------------------------------------------------
# Integration tests: real httpie graph + Vertex AI
# ---------------------------------------------------------------------------


def _register_graph_expand(registry: SkillRegistry, graph: CodeGraph) -> None:
    """Register graph_expand skill backed by a real CodeGraph.

    Mirrors the post-#147 graph_expand API: 1-hop LSP-aligned range/symbol
    query returning defined/callees/callers tagged by role. This test
    exercises the symbol-input path; comma-separated `str` is used here so
    the Vertex AI tool schema stays simple (`type_hint="str"`) — the
    wrapper splits it into the List[str] the executor expects.
    """
    ctx = ExpandContext(code_graph=graph)
    raw_executor = create_executor(ctx)

    registry.register(
        SkillMetadata(
            skill_id="graph_expand",
            skill_type=SkillType.EXPAND,
            inputs=[
                SkillInputSpec(
                    name="symbols",
                    type_hint="str",
                    required=True,
                    description=(
                        "Comma-separated qualified symbol names to expand "
                        "from (e.g. 'httpie.core.main, httpie.cli.argparser')."
                    ),
                ),
                SkillInputSpec(
                    name="mode",
                    type_hint="str",
                    required=False,
                    default="all",
                    description=(
                        'Which relationships to return: "defined" / "callees" / '
                        '"callers" / "all" (default).'
                    ),
                ),
                SkillInputSpec(
                    name="top_k",
                    type_hint="int",
                    required=False,
                    default=20,
                ),
            ],
            executor_fn=_wrap_executor_for_agent(raw_executor),
            description=(
                "1-hop graph query: given one or more symbol names, return "
                "the symbols defined at each, the symbols they call, and "
                "the symbols that call them. Each result carries the "
                "call-site anchor_line."
            ),
        )
    )


class TestAgentGraphVertexAI:
    """End-to-end: LLM agent uses graph_expand on the real httpie graph."""

    @pytest.fixture()
    def registry_with_graph(self, httpie_graph):
        SkillRegistry.reset()
        reg = SkillRegistry()
        _register_graph_expand(reg, httpie_graph)
        yield reg
        SkillRegistry.reset()

    @pytest.fixture()
    def vertex_llm(self):
        try:
            return LiteLLMChat(
                model="vertex_ai/gemini-2.5-flash",
                temperature=0.0,
                max_tokens=2048,
            )
        except Exception as e:
            pytest.skip(f"Vertex AI not available: {e}")

    def test_agent_explores_httpie_graph(
        self,
        vertex_llm,
        registry_with_graph,
        httpie_graph,
    ):
        """Agent should use graph_expand to discover httpie symbols."""
        # Pick some real symbols from the graph for the system prompt
        sample_symbols = _sample_symbol_names(httpie_graph, max_count=15)
        symbol_list = "\n".join(f"- {s}" for s in sample_symbols)

        system_prompt = (
            "You are a code exploration agent for the httpie/cli Python project. "
            "You have a graph_expand tool that, given one or more symbol names, "
            "returns the symbols defined at each (`defined`), the symbols they "
            "call (`callees`), and the symbols that call them (`callers`).\n\n"
            f"Here are some symbols in the codebase:\n{symbol_list}\n\n"
            "Use graph_expand to explore relationships, then summarize findings."
        )

        runner = AgentRunner(
            llm=vertex_llm,
            registry=registry_with_graph,
            system_prompt=system_prompt,
            max_turns=5,
        )

        # Ask about a core httpie symbol
        seed = sample_symbols[0] if sample_symbols else "httpie.core.main"
        result = runner.run(
            f"What symbols are related to {seed!r}? "
            f"Use graph_expand with symbols={seed!r} to find out."
        )

        assert isinstance(result, AgentResult)
        assert result.total_turns >= 1
        assert len(result.tool_calls) >= 1
        assert any(tc.skill_id == "graph_expand" for tc in result.tool_calls)
        assert len(result.answer) > 0

        # At least one tool call should have succeeded
        successful = [tc for tc in result.tool_calls if tc.error is None]
        assert len(successful) >= 1

        print(f"\n--- Agent Result (httpie graph) ---")
        print(f"Turns: {result.total_turns}")
        print(f"Tool calls: {len(result.tool_calls)}")
        for tc in result.tool_calls:
            n_results = len(tc.result) if isinstance(tc.result, list) else "N/A"
            print(
                f"  {tc.skill_id}({tc.arguments}) "
                f"-> {n_results} results, error={tc.error}"
            )
        print(f"Answer:\n{result.answer[:800]}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wrap_executor_for_agent(executor_fn):
    """Wrap the new graph_expand executor so it accepts a comma-separated
    string from the LLM (Vertex's tool schema doesn't have a clean way to
    express `List[str]`).

    Splits the string into ``symbols=[...]`` and forwards remaining kwargs
    untouched. ``mode`` / ``top_k`` arrive as native types from the schema.
    """

    def wrapped(symbols, mode="all", top_k=20, **kwargs):
        if isinstance(symbols, str):
            symbols = [s.strip() for s in symbols.split(",") if s.strip()]
        return executor_fn(
            symbols=symbols,
            mode=mode,
            top_k=int(top_k),
            **kwargs,
        )

    return wrapped


def _sample_symbol_names(graph: CodeGraph, max_count: int = 15) -> list[str]:
    """Pick representative symbol names from the graph for the system prompt."""
    from codeminer.types import is_symbol_node

    symbols = []
    for v in graph.graph.vs:
        if is_symbol_node(v) and v["name"].startswith("httpie."):
            symbols.append(v["name"])
        if len(symbols) >= max_count:
            break
    return symbols
