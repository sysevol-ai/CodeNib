# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Lightweight agent runner using LLM tool calling.

Implements a think → act → observe loop that lets an LLM decide which
CodeMiner skills to invoke, execute them, and iterate until the LLM
produces a final answer.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Union

from ..compiler.manifest import RepoManifest
from ..llm.litellm_chat import LiteLLMChat, RetryConfig
from ..llm.usage import UsageTracker
from ..log_utils import get_logger
from .agent_types import AgentResult, ToolCallRecord
from .boundary import from_agent_repr_arg, is_line_bearing, to_agent_repr
from .history import PlainChatHistory, TokenBudgetedChatHistory
from .lsp_provider import (
    fingerprint_lsp_result,
    lsp_result_metadata,
    preview_lsp_result,
)
from .route_context import (
    build_lsp_route_context,
    canonical_lsp_route_args,
    fingerprint_lsp_route_nodes,
    normalize_lsp_route_seed_policy,
    summarize_lsp_route_nodes,
)
from .runtime.trace import AgentRunTrace
from .skills.loader import SkillLoader
from .skills.registry import SkillRegistry
from .skills.typecheck import coerce_args
from .tool_schema import registry_to_tools, tools_to_schemas
from .tools.defaults import (
    _SKIP_DIR_PREFIXES,
    DEFAULT_TOOL_IDS,
    ensure_default_tools_registered,
)
from .tools.spec import ToolRegistry

logger = get_logger(__name__)

_DEFAULT_MODEL = "claude-sonnet-4-6"

# Path to the bundled skill packages. ``query()`` defaults to this so
# callers don't have to know where skills live on disk.
_DEFAULT_SKILLS_DIR: Path = Path(__file__).parent / "skills"

CompileTableInput = Union[str, Path, Mapping[str, Any]]

# Single source of truth for the answer contract. The system prompt below and
# the forced final-answer turn (on budget exhaustion) both reference this, and
# the eval harness parses exactly these lines — so prompt and parser never drift.
LOCALIZATION_SCHEMA = (
    "Files: path/one.ext, path/two.ext\n"
    "Symbols: path/one.ext:symbol_name, ...\n"
    "Locations: path/one.ext:START-END, path/two.ext:START-END"
)

# Detect whether an answer already carries the full contract (markdown-tolerant,
# to match the eval parser): ``Files:``, ``**Files:**``, ``- files =`` all count.
_LABEL_PREFIX = r"(?im)^[\s>#*_`\-]*"
_HAS_FILES_CONTRACT = re.compile(rf"{_LABEL_PREFIX}files?[\s*_`]*[:=]")
_HAS_SYMBOLS_CONTRACT = re.compile(rf"{_LABEL_PREFIX}symbols?[\s*_`]*[:=]")
_HAS_LOCATIONS_CONTRACT = re.compile(rf"{_LABEL_PREFIX}locations?[\s*_`]*[:=]")


def has_localization_contract(answer: str) -> bool:
    """Return true when an answer carries the localization output contract."""
    return all(
        pattern.search(answer or "")
        for pattern in (
            _HAS_FILES_CONTRACT,
            _HAS_SYMBOLS_CONTRACT,
            _HAS_LOCATIONS_CONTRACT,
        )
    )


def _has_localization_contract(answer: str) -> bool:
    return has_localization_contract(answer)


def _looks_incomplete(text: str) -> bool:
    """True when *text* is mid-exploration narration, not a synthesized reply.

    Weak/agentic models often leave chatter like "Let me look at models.py:"
    when they run out of turns; the demo should replace that with a real
    forced answer rather than surface it to the user.
    """
    t = (text or "").strip()
    if len(t) < 200:
        return True
    low = t.lower()
    return low.startswith(
        ("let me", "let's", "i'll", "i will", "now i", "first, let", "next, ")
    ) or t.endswith(":")


_DEFAULT_SYSTEM_PROMPT = f"""\
You are a code localization agent. Find the code locations (files and \
symbols) relevant to the request, then give a concise answer naming them.

You always have a filesystem toolset for navigating the repository:
- glob(pattern, path) — list files by glob (explore layout)
- grep(pattern, path, glob, type, output_mode) — search file contents (regex)
- bash(command) — run a shell command (ls, find, git)
- read(file_path, offset, limit) — read a file or a line range

Depending on the request you may also have retrieval skills:
- bm25_search(query) — fast lexical search for exact names / identifiers
- embedding_search(query) — semantic search for concepts / intent
- hybrid_search(...) — combine retrievers for maximum recall
- find_callers(symbol) / find_callees(symbol) / trace(from_symbol, to_symbol) — \
call-graph navigation (who calls X, what X calls, the path from X to Y). Use \
for impact and to follow a bug across functions — the structural questions \
grep cannot answer. Compact results; read the ones you care about.

Accuracy comes first: you MUST end up having read the file that actually \
implements the behavior to change. Use codeminer_context / the graph as a \
*heuristic* to point you there fast — but grep and read are how you \
confirm you reached it.

Workflow — iterate EXPAND → READ → EXPAND until you've confirmed the file:
1. ORIENT — call codeminer_context(query) first: one call returns candidate \
entry-point symbols plus their callers/callees as a starting map.
2. READ — read the most promising candidate(s) to check whether the code \
to change is really there.
3. EXPAND — if it's not (e.g. you landed on a base class, a caller, or a near \
miss): follow the graph (find_callers / find_callees / trace) AND/OR grep for \
the symbol across the repo (grep pattern=...). A base-class method \
often has the real change in a SUBCLASS OVERRIDE — grep the method name to find \
all definitions, then read them. Loop back to READ.
4. ANSWER — only once you have READ a file and confirmed it contains the code \
to change. End with these three lines, repo-relative. The line numbers come \
straight from what `read` showed you — cite the exact range of the code to change:
{LOCALIZATION_SCHEMA}

Guidelines:
- The graph map is a hint, not the answer — always confirm by reading.
- If the obvious symbol is a base/abstract definition, grep its name to find \
overrides/subclasses and read those before answering.
- Prefer the heuristic map + targeted reads over blind grep fan-out, but do not \
stop until you have actually read the implementing file.
- You have a limited tool budget. Converge: once a file looks right, confirm \
it and answer rather than exhaustively reading every candidate.
"""

_LSP_ROUTE_TOOL_GUIDANCE = """\

Dynamic LSP route tool:
- If `lsp_route` is available, use it early when the request names a symbol, \
error code, config field, API, or behavior that likely crosses files.
- Call `lsp_route(symbols=[...], query=<original request>, top_k=5)` with the \
most specific symbol-like names you know. If you have no reliable symbol yet, \
call it with `symbols=[]` and the original request as `query`.
- Treat returned nodes as route hints only. Read the first one or two promising \
files/ranges before citing anything in the final answer.
"""

# Maximum characters for a single tool result to avoid context blowup.
_MAX_RESULT_CHARS = 16_000
_CHARS_PER_TOKEN_ESTIMATE = 4


class AgentRunner:
    """LLM-driven agent loop over the CodeMiner skill registry.

    Usage::

        from codeminer.agent.skills.registry import SkillRegistry

        runner = AgentRunner(model="gpt-4o", registry=SkillRegistry())
        result = runner.run("How does authentication work in this repo?")
        print(result.answer)
    """

    def __init__(
        self,
        llm: Optional[LiteLLMChat] = None,
        registry: Optional[SkillRegistry] = None,
        *,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 512,
        system_prompt: Optional[str] = None,
        max_turns: int = 10,
        max_context_tokens: Optional[int] = None,
        allow_skills: Optional[Set[str]] = None,
        exclude_skills: Optional[Set[str]] = None,
        manifest: Optional[Any] = None,
        session_ctx: Optional[Any] = None,
        compile_table: Optional[Any] = None,
        include_default_tools: bool = True,
        default_tool_ids: Optional[Set[str]] = None,
        retry: Optional[RetryConfig] = None,
        force_localization_contract: bool = False,
        first_turn_tool_choice: Optional[str] = None,
        force_first_turn_only: bool = False,
        compact_after_read: bool = False,
        compact_keep_reads: int = 0,
        enable_lsp_route_context: bool = False,
        lsp_route_seed_limit: int = 8,
        lsp_route_seed_policy: str = "all",
        lsp_route_query_fallback: bool = False,
        lsp_route_top_k: int = 12,
        lsp_route_include_neighbors: bool = True,
    ) -> None:
        if llm is not None:
            self.llm = llm
        elif model is not None:
            self.llm = LiteLLMChat(
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                retry=retry or RetryConfig(),
            )
        else:
            raise ValueError("Either 'llm' or 'model' must be provided")
        # Optional per-run context-window cap. When set, ``run()`` keeps the
        # conversation in a :class:`TokenBudgetedChatHistory` that evicts the
        # oldest non-system messages once the budget would be exceeded, so a
        # long tool-calling loop can't overflow the model context.
        self.max_context_tokens = max_context_tokens
        self.registry = registry or SkillRegistry()
        # Default tools (read + grep + glob + bash) are a SEPARATE type from
        # retrieval skills: they live in their own ``ToolRegistry`` and never
        # pass through the skill allow/exclude/compile_table funnel. Registered
        # unconditionally so every query — and every agent-compile subset — has
        # the filesystem primitives the model is pretrained on.
        # ``include_default_tools=False`` withholds them — used to force the
        # structured (retrieval + graph) path in cost-comparison experiments.
        self.tool_registry = ToolRegistry()
        self._include_defaults = include_default_tools
        self._default_ids: Set[str] = set()
        if include_default_tools:
            ensure_default_tools_registered(self.tool_registry)
            # Which of the registered defaults to actually expose. Defaults to
            # ALL (read + grep + glob + bash). Restricting to a subset — e.g.
            # ``{"read"}`` — yields a graph-primary / LocAgent-style harness
            # that can read code but has NO grep escape hatch, so the structured
            # (bm25 + call-graph) tools must carry navigation.
            allowed = set(DEFAULT_TOOL_IDS)
            # A non-empty restrictor narrows to that subset; ``None`` *or* an
            # empty set means "all defaults" (the off-switch is
            # ``include_default_tools=False``, not an empty restrictor) — this
            # mirrors the empty->all skills contract in ``_resolve_allow_set``.
            if default_tool_ids:
                allowed &= set(default_tool_ids)
            self._default_ids = allowed

        # Tools and skills share the model-facing namespace: a skill whose id
        # equals a default-tool id would emit a duplicate function name (which
        # providers reject) and shadow the tool at dispatch. Fail loudly here.
        collisions = self._default_ids & set(self.registry.list_skills())
        if collisions:
            raise ValueError(
                f"skill id(s) {sorted(collisions)} collide with default tool "
                f"id(s); rename the skill(s) — tools and skills share the "
                f"model-facing tool namespace."
            )
        self.max_turns = max_turns
        self.session_ctx = session_ctx
        # The localization eval needs answers to end in the Files:/Symbols:/
        # Locations: contract; QA callers (web demo) keep prose, so they turn
        # this off and skip the schema-forcing final turn entirely.
        self._force_contract = force_localization_contract
        self.first_turn_tool_choice = first_turn_tool_choice
        # When True, only turn 0 is forced (legacy single-turn behaviour);
        # default False = force until the agent reads a file.
        self._force_first_turn_only = force_first_turn_only
        # eager_compact: after the first successful read anchors the agent, stub
        # the bulky tool outputs already in history (full file/grep text) down to
        # short references so later turns stop re-carrying them — the dominant
        # prompt-token cost (prompt is ~97% of spend and grows every turn).
        # Loss-safe for localization (answers need path/symbol/line, not source).
        self._compact_after_read = compact_after_read
        # Seed richness for eager_compact: how many successful read outputs to
        # keep VERBATIM in the collapse seed. 0 still carries the latest read
        # transcript so the model can see the file it just requested; higher
        # values inline additional recent reads so weaker models don't re-read
        # after the collapse (the "re-read tax" observed on 4B). Set by a
        # model-strength router upstream.
        self._compact_keep_reads = compact_keep_reads
        # Optional startup context over the same static graph exposed by the
        # lsp_route skill. This is opt-in harness policy: it makes graph route
        # hints available on turn 1 without depending on the model to discover
        # the tool, while still requiring normal reads before answering.
        self._enable_lsp_route_context = bool(enable_lsp_route_context)
        self._lsp_route_seed_limit = max(1, int(lsp_route_seed_limit or 8))
        self._lsp_route_seed_policy = normalize_lsp_route_seed_policy(
            lsp_route_seed_policy
        )
        self._lsp_route_query_fallback = bool(lsp_route_query_fallback)
        self._lsp_route_top_k = max(1, int(lsp_route_top_k or 12))
        self._lsp_route_include_neighbors = bool(lsp_route_include_neighbors)

        # Resource guard: filter unavailable skills and collect warnings.
        # The "base" allow / exclude are stored so we can recompute the
        # tool list per-query when a compile_table is in play.
        self._base_allow: Optional[Set[str]] = (
            set(allow_skills) if allow_skills is not None else None
        )
        self._base_exclude: Set[str] = set(exclude_skills) if exclude_skills else set()
        resource_warnings: List[str] = []

        if manifest is not None:
            from .resource_guard import ResourceGuard

            guard = ResourceGuard(manifest, self.registry)
            report = guard.preflight()
            self._base_exclude |= report.unavailable
            resource_warnings = report.warnings

        # ``_base_exclude`` applies to *skills* only. Default tools live in a
        # separate ``ToolRegistry`` (and are gated by ``_default_ids``), so no
        # skill exclude — caller's ``exclude_skills`` or the ResourceGuard's
        # ``report.unavailable`` — can ever reach them.

        self._compile_table = compile_table

        # Pre-compute the static tool list. When a compile_table is set,
        # ``run()`` recomputes per-query against the resolved allow set.
        self.tools = self._tools_for(self._base_allow)

        # Build system prompt: base + environment block + resource warnings.
        base_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT
        tool_guidance = self._build_tool_guidance_block(self.tools)
        if tool_guidance:
            base_prompt += tool_guidance
        env_block = self._build_environment_block(self.session_ctx)
        if env_block:
            base_prompt += f"\n{env_block}\n"
        if resource_warnings:
            warnings_text = "\n".join(f"- {w}" for w in resource_warnings)
            base_prompt += f"\nIndex warnings:\n{warnings_text}\n"
        self.system_prompt = base_prompt

    @staticmethod
    def _build_tool_guidance_block(tools: Sequence[Mapping[str, Any]]) -> str:
        """Render prompt guidance for optional dynamic tools that need adoption."""

        names = {
            str((tool.get("function") or {}).get("name") or "")
            for tool in tools or []
            if isinstance(tool, Mapping)
        }
        if "lsp_route" in names:
            return _LSP_ROUTE_TOOL_GUIDANCE
        return ""

    @staticmethod
    def _build_environment_block(session_ctx: Optional[Any]) -> str:
        """Render an <environment> block giving the agent a starting point.

        Mirrors OpenCode's environment-info layer: the agent sees the repo
        path, primary language, and a depth-1 listing so it can orient
        before searching. Returns "" when no repo_path is available (keeps
        prompt-free tests unchanged).
        """
        repo_path = getattr(session_ctx, "repo_path", None) if session_ctx else None
        if not repo_path:
            return ""
        lines = ["<environment>", f"repo_path: {repo_path}"]
        lang = getattr(session_ctx, "primary_language", None)
        if lang:
            lines.append(f"primary_language: {lang}")
        size = getattr(session_ctx, "repo_size", None)
        if size:
            lines.append(f"repo_size: {size} files")
        try:
            entries = []
            for child in sorted(Path(repo_path).iterdir(), key=lambda p: p.name):
                if child.name.startswith(".") or child.name in _SKIP_DIR_PREFIXES:
                    continue
                entries.append(child.name + ("/" if child.is_dir() else ""))
            if entries:
                lines.append("top-level entries: " + ", ".join(entries[:40]))
        except OSError as exc:
            # Top-level listing is best-effort context; skip it if the repo dir
            # can't be read (missing / perms). The environment block is still
            # returned without it.
            logger.debug("env block: could not list %s: %s", repo_path, exc)
        lines.append("</environment>")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Allow-set resolution (issue #149)
    # ------------------------------------------------------------------

    def _tools_for(self, allow: Optional[Set[str]]) -> List[Dict[str, Any]]:
        """Combined schemas for the swept *skills* plus the always-on *tools*.

        Two registries, two passes: the skill allow/exclude funnel governs the
        swept retrieval skills (``allow=None`` exposes the whole skill
        registry), while the default tools are emitted independently from the
        ``ToolRegistry`` gated by ``self._default_ids``. The default primitives
        thus remain available in every subset regardless of any
        allow/compile_table narrowing, without being mixed into the skill
        allowlist.
        """
        resolved = self._resolve_allow_set(allow)
        skill_tools = registry_to_tools(
            self.registry,
            allow=resolved,
            exclude=self._base_exclude,
        )
        default_tools = tools_to_schemas(self.tool_registry, allow=self._default_ids)
        return skill_tools + default_tools

    def _resolve_allow_set(self, allow: Optional[Set[str]]) -> Optional[Set[str]]:
        """Pin the empty-allowlist contract: empty → full registry + WARN.

        ``allow_skills=None`` means "no filter — expose the whole
        registry". An *empty* set (``frozenset()`` or ``set()``) would
        otherwise expose zero tools and stall the agent; #149 pins this
        case to fall back to the full registry with a warning so the
        caller can debug.
        """
        if allow is None:
            return None
        if len(allow) == 0:
            logger.warning(
                "AgentRunner: empty allow_skills → falling back to full "
                "registry (no compile_table hit for this scenario)"
            )
            return None
        return set(allow)

    def run(
        self,
        query: str,
        *,
        max_turns: Optional[int] = None,
        chat_history: Optional[List[Dict[str, str]]] = None,
    ) -> AgentResult:
        """Execute the agent loop and return the result.

        ``chat_history`` seeds prior conversation turns (text-only
        ``{"role": "user"|"assistant", "content": ...}`` dicts, no tool
        messages) between the system prompt and *query*, so follow-up
        questions can reference earlier answers.
        """
        max_turns = max_turns or self.max_turns

        # CAR / agent_compile: when a compile_table is set, classify the
        # query and intersect the table-resolved subset with the
        # constructor-time allow set. The table can *narrow* allowed
        # skills, never broaden them, so the user's explicit
        # ``allow_skills`` remains the upper bound.
        tools = self.tools
        if self._compile_table is not None:
            from .compile import agent_compile

            table_allow = agent_compile(query, self.session_ctx, self._compile_table)
            if table_allow is not None:
                effective: Optional[Set[str]]
                if self._base_allow is None:
                    effective = set(table_allow)
                else:
                    effective = set(table_allow) & self._base_allow
                    # A compile_table subset that is disjoint from a
                    # non-empty allow_skills upper bound must NOT broaden
                    # back to the full registry via the empty→full
                    # fallback. The table can only narrow, never broaden,
                    # so fall back to the upper bound itself.
                    if not effective and self._base_allow:
                        logger.warning(
                            "AgentRunner: compile_table scenario is disjoint "
                            "from the allow_skills upper bound %s; keeping "
                            "allow_skills (table narrows, never broadens)",
                            sorted(self._base_allow),
                        )
                        effective = set(self._base_allow)
                tools = self._tools_for(effective)

        all_tool_calls: List[ToolCallRecord] = []
        usage_tracker = UsageTracker()
        start = time.monotonic()
        trace = AgentRunTrace()
        trace.add(
            "run_start",
            0,
            query_chars=len(query or ""),
            max_turns=max_turns,
            tool_count=len(tools or []),
        )
        user_query = query
        if self._enable_lsp_route_context:
            route_start = time.monotonic()
            route_context, route_skip_reason = self._initial_lsp_route_context(query)
            route_duration_ms = (time.monotonic() - route_start) * 1000
            if route_context is not None and route_context.text:
                user_query = f"{query}\n\n{route_context.text}"
                seeds = list(route_context.seeds)
                seed_source = "symbol" if seeds else "query"
                trace.add(
                    "lsp_route_context",
                    0,
                    status="offered",
                    seeds=seeds,
                    seed_source=seed_source,
                    seed_policy=self._lsp_route_seed_policy,
                    route_count=len(route_context.nodes),
                    context_chars=len(route_context.text),
                    duration_ms=route_duration_ms,
                    route_args=dict(route_context.arguments or {}),
                    route_fingerprint=route_context.route_fingerprint,
                    route_preview=summarize_lsp_route_nodes(route_context.nodes),
                )
                trace.add_context(
                    "lsp_route",
                    "offered",
                    0,
                    summary=(
                        "initial static LSP route context from "
                        + ("seeds: " + ", ".join(seeds) if seeds else "query text")
                    ),
                    provenance={
                        "kind": "initial_context",
                        "tool": "lsp_route",
                    },
                    freshness="fresh",
                    token_estimate=_estimate_context_tokens(route_context.text),
                    metadata={
                        "seeds": seeds,
                        "seed_source": seed_source,
                        "seed_policy": self._lsp_route_seed_policy,
                        "route_count": len(route_context.nodes),
                        "top_k": self._lsp_route_top_k,
                        "include_neighbors": self._lsp_route_include_neighbors,
                        "query_fallback": self._lsp_route_query_fallback,
                        "duration_ms": route_duration_ms,
                        "route_args": dict(route_context.arguments or {}),
                        "route_fingerprint": route_context.route_fingerprint,
                    },
                )
            else:
                trace.add(
                    "lsp_route_context",
                    0,
                    status="skipped",
                    reason=route_skip_reason or "empty_route_context",
                    duration_ms=route_duration_ms,
                )

        history = self._new_history()
        history.add_message({"role": "system", "content": self.system_prompt})
        for msg in chat_history or []:
            history.add_message({"role": msg["role"], "content": msg["content"]})
        history.add_message({"role": "user", "content": user_query})
        has_read = False  # has the agent read a file yet (gates forced tools)
        read_paths: List[str] = []  # files the agent read (for schema salvage)
        read_outputs: List[Dict[str, str]] = []  # successful read transcripts
        compacted = False  # eager_compact: collapsed to direction seed yet?

        def _finish_result(
            answer: str,
            *,
            total_turns: int,
            answer_source: str,
        ) -> AgentResult:
            elapsed = (time.monotonic() - start) * 1000
            trace.add(
                "final_answer",
                total_turns,
                source=answer_source,
                answer_chars=len(answer or ""),
                has_contract=_has_localization_contract(answer or ""),
            )
            trace.add(
                "run_end",
                total_turns,
                duration_ms=elapsed,
                tool_calls=len(all_tool_calls),
                usage_records=len(usage_tracker.records),
            )
            return AgentResult(
                answer=answer,
                tool_calls=all_tool_calls,
                messages=history.get_messages(),
                total_turns=total_turns,
                total_duration_ms=elapsed,
                usage=usage_tracker.totals(),
                usage_records=list(usage_tracker.records),
                trace=trace,
            )

        for turn in range(max_turns):
            logger.debug("agent turn %d/%d", turn + 1, max_turns)

            call_kwargs: Dict[str, Any] = {
                "usage_tracker": usage_tracker,
                "usage_turn": turn + 1,
            }
            # Last-turn salvage: if this is the final allowed turn and the agent
            # still hasn't emitted the Files:/Symbols:/Locations: contract, spend
            # it producing a formatted answer instead of one more (truncated)
            # tool call. This is contract preservation: a useful localization can
            # be lost if the run ends mid-search or in unstructured prose.
            is_last_turn = self._force_contract and turn == max_turns - 1
            last_assistant = ""
            for _m in reversed(history.get_messages()):
                if _m.get("role") == "assistant" and _m.get("content"):
                    last_assistant = _m["content"]
                    break
            if is_last_turn and not _has_localization_contract(last_assistant):
                history.add_message(
                    {
                        "role": "user",
                        "content": (
                            "Stop exploring. Give your final answer NOW in exactly "
                            f"this format:\n{LOCALIZATION_SCHEMA}"
                        ),
                    }
                )
                if tools:
                    call_kwargs["tools"] = tools
                    call_kwargs["tool_choice"] = "none"
            elif tools:
                call_kwargs["tools"] = tools
                # Force tool calls until the agent has actually READ a file.
                # The localization contract requires citing inspected code, so
                # keep tool_choice forced until a read happens; then drop to
                # "auto" so the model is free to commit. Bounded by
                # first_turn_only for the old single-turn behaviour.
                if self.first_turn_tool_choice and not has_read:
                    if not (self._force_first_turn_only and turn > 0):
                        call_kwargs["tool_choice"] = self.first_turn_tool_choice

            trace.add(
                "llm_call",
                turn + 1,
                message_count=len(history.get_messages()),
                tool_count=len(call_kwargs.get("tools") or []),
                tool_choice=call_kwargs.get("tool_choice", "auto"),
            )
            response = self.llm._call_raw(history.get_messages(), **call_kwargs)
            choice = response.choices[0]
            assistant_msg = choice.message

            # Append assistant message to conversation (may evict oldest
            # non-system messages when a context-token budget is set).
            history.add_message(_message_to_dict(assistant_msg))

            # Check for tool calls
            tool_calls = getattr(assistant_msg, "tool_calls", None)
            trace.add(
                "llm_response",
                turn + 1,
                content_chars=len(getattr(assistant_msg, "content", None) or ""),
                tool_call_count=len(tool_calls or []),
            )
            if not tool_calls:
                # Terminal: LLM stopped calling tools. If it answered in prose
                # without the contract (it explored but didn't format), force one
                # schema turn so a genuine localization isn't lost to formatting.
                answer = getattr(assistant_msg, "content", None) or ""
                answer_source = "assistant"
                if (
                    self._force_contract
                    and all_tool_calls
                    and not _has_localization_contract(answer)
                ):
                    trace.add(
                        "final_answer_forced",
                        turn + 1,
                        reason="missing_contract",
                        read_paths=list(dict.fromkeys(read_paths)),
                    )
                    answer = (
                        self._force_schema_answer(
                            history, usage_tracker, turn + 2, read_paths
                        )
                        or answer
                    )
                    answer_source = "forced_schema"
                elif (
                    not self._force_contract
                    and all_tool_calls
                    and _looks_incomplete(answer)
                ):
                    forced = self._force_final_answer(history, usage_tracker, turn + 2)
                    if forced.strip():
                        answer = forced
                        answer_source = "forced_answer"
                return _finish_result(
                    answer=answer,
                    total_turns=turn + 1,
                    answer_source=answer_source,
                )

            # Execute each tool call
            for tc in tool_calls:
                record = self._execute_tool_call(tc)
                all_tool_calls.append(record)
                tool_content = _serialize_result(
                    record.result if record.error is None else record.error
                )
                trace.add(
                    "tool_call",
                    turn + 1,
                    **_trace_tool_call_data(record, tool_content),
                )
                # A successful read satisfies the "read before answering"
                # contract; once it happens, stop forcing tool calls.
                successful_read = (
                    record.skill_id == "read"
                    and record.error is None
                    and not tool_content.lstrip().startswith("Error")
                )
                if record.skill_id == "read":
                    trace.add(
                        "read",
                        turn + 1,
                        tool_call_id=record.tool_call_id,
                        path=str((record.arguments or {}).get("file_path") or ""),
                        offset=(record.arguments or {}).get("offset"),
                        limit=(record.arguments or {}).get("limit"),
                        status="ok" if successful_read else "error",
                    )
                trace.add_context(
                    record.skill_id,
                    _context_state(record, tool_content, successful_read),
                    turn + 1,
                    **_context_ledger_data(record, tool_content),
                )
                if successful_read:
                    has_read = True
                    _rp = (record.arguments or {}).get("file_path")
                    if _rp:
                        read_paths.append(str(_rp))
                    read_outputs.append(
                        {"path": str(_rp or "(unknown)"), "content": tool_content}
                    )

                # Append tool response message
                history.add_message(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_content,
                    }
                )

            # eager_compact: once the eager phase has read a candidate and judged
            # the direction, COLLAPSE the bulky preload dump + the whole
            # exploration trace to a small distilled seed
            # ([system, clean_query + judged direction]) and let the
            # "correct-path" continuation finish from there. We do NOT carry the
            # preload candidate snippets or the exploration history into every
            # later turn (re-sending them is the dominant prompt-token cost).
            # Once only — this is the explore→commit boundary, not a rolling trim.
            if self._compact_after_read and has_read and not compacted:
                compacted = True
                if self._compact_history(
                    history,
                    read_paths,
                    self._compact_keep_reads,
                    read_outputs=read_outputs,
                ):
                    trace.add(
                        "context_compacted",
                        turn + 1,
                        read_paths=list(dict.fromkeys(read_paths)),
                        keep_reads=self._compact_keep_reads,
                    )
                    trace.add_context(
                        "runtime.compaction",
                        "summarized",
                        turn + 1,
                        summary=(
                            "Compacted conversation after reads: "
                            + (", ".join(dict.fromkeys(read_paths)) or "(none)")
                        ),
                        metadata={
                            "read_paths": list(dict.fromkeys(read_paths)),
                            "keep_reads": self._compact_keep_reads,
                        },
                    )
                    logger.debug("eager_compact: collapsed to distilled direction seed")

        # Max turns exhausted. Prefer the last textual assistant message.
        trace.add(
            "max_turns_exhausted",
            max_turns,
            tool_calls=len(all_tool_calls),
            has_read=has_read,
        )
        last_content = ""
        for msg in reversed(history.get_messages()):
            if msg.get("role") == "assistant" and msg.get("content"):
                last_content = msg["content"]
                break

        # Budget exhausted. A capped agent usually left mid-exploration chatter
        # ("Let me check…"), so force a schema-conforming final answer unless it
        # already emitted the contract.
        answer_source = "max_turns"
        if (
            self._force_contract
            and all_tool_calls
            and not _has_localization_contract(last_content)
        ):
            trace.add(
                "final_answer_forced",
                max_turns,
                reason="max_turns_exhausted",
                read_paths=list(dict.fromkeys(read_paths)),
            )
            last_content = (
                self._force_schema_answer(
                    history, usage_tracker, max_turns + 1, read_paths
                )
                or last_content
            )
            answer_source = "forced_schema"
        elif (
            not self._force_contract
            and all_tool_calls
            and _looks_incomplete(last_content)
        ):
            trace.add("final_answer_forced", max_turns, reason="max_turns_exhausted")
            forced = self._force_final_answer(history, usage_tracker, max_turns + 1)
            if forced.strip():
                last_content = forced
                answer_source = "forced_answer"

        return _finish_result(
            answer=last_content,
            total_turns=max_turns,
            answer_source=answer_source,
        )

    def _initial_lsp_route_context(self, query: str) -> tuple[Any | None, str | None]:
        """Build optional static graph route context for the opening prompt."""

        meta = self.registry.get("lsp_route")
        if meta is None or meta.executor_fn is None:
            return None, "lsp_route_unavailable"
        try:
            context = build_lsp_route_context(
                meta.executor_fn,
                query,
                seed_limit=self._lsp_route_seed_limit,
                seed_policy=self._lsp_route_seed_policy,
                query_fallback=self._lsp_route_query_fallback,
                top_k=self._lsp_route_top_k,
                include_neighbors=self._lsp_route_include_neighbors,
            )
        except Exception as exc:  # noqa: BLE001 - startup hints are best-effort
            logger.warning("initial lsp_route context failed: %s", exc)
            return None, f"lsp_route_error:{type(exc).__name__}"
        if not context.seeds and not self._lsp_route_query_fallback:
            return None, "no_symbol_seeds"
        if not context.nodes:
            return None, "no_route_nodes"
        if not context.text:
            return None, "empty_route_context"
        return context, None

    def _compact_history(
        self,
        history: Any,
        read_paths: Optional[List[str]] = None,
        keep_reads: int = 0,
        read_outputs: Optional[List[Dict[str, str]]] = None,
    ) -> int:
        """Collapse eager-phase exploration to a distilled direction seed.

        The eager phase looks at the preload candidates and reads 1-2 to JUDGE
        the correct direction. Once anchored we reset the context to
        ``[system, clean_query + judged direction]`` — we do NOT carry the bulky
        preload candidate snippets or the whole exploration trace into the rest
        of the run (re-sending them every turn is the dominant prompt cost, ~97%
        of spend). The continuation works the "correct path" from a small seed.

        ``keep_reads`` is the seed-richness dial set by a model-strength router.
        The latest successful read is always inlined because this collapse runs
        immediately after the read result enters history; without that transcript
        the next model turn would never see the file it just asked to inspect.
        N>0 inlines up to the last N successful read outputs (not grep/glob/bash
        output) so a WEAK model does not re-read them after the collapse (the
        "re-read tax" seen on 4B). It trades some token saving for fewer re-read
        turns.

        Loss-safe for localization: the seed names the files judged relevant plus
        the agent's own assessment; answers need path/symbol/line, not source.
        Returns 1 if it collapsed, else 0.
        """
        msgs = history.get_messages()
        system = next((m for m in msgs if m.get("role") == "system"), None)
        # Recover the clean task query: the first user message minus the appended
        # preload preamble (which begins at the "# Candidate locations" marker).
        first_user = next((m for m in msgs if m.get("role") == "user"), None)
        clean_q = (first_user or {}).get("content", "") or ""
        cut = clean_q.find("# Candidate locations")
        if cut > 0:
            clean_q = clean_q[:cut].rstrip()
        # The judged direction: the agent's latest reasoning + the files it read.
        last_assistant = ""
        for m in reversed(msgs):
            if m.get("role") == "assistant" and m.get("content"):
                last_assistant = m["content"]
                break
        reads = ", ".join(dict.fromkeys(read_paths or [])) or "(none)"
        # Seed-richness: inline recent successful read outputs, never incidental
        # tool output from grep/glob/bash that happened to run later in the turn.
        kept = ""
        if read_outputs:
            n_keep = max(1, int(keep_reads or 0))
            kept_reads = [r for r in read_outputs[-n_keep:] if r.get("content")]
            if kept_reads:
                kept = (
                    "\n# Content you already read (do NOT re-read these):\n"
                    + "\n---\n".join(
                        f"# read {r.get('path') or '(unknown)'}\n{r['content']}"
                        for r in kept_reads
                    )
                    + "\n"
                )
        seed = (
            f"{clean_q}\n\n"
            "# Triage complete — focus, do not re-explore from scratch\n"
            f"Files you already read and judged relevant: {reads}\n"
            f"{kept}"
            f"Your assessment so far:\n{last_assistant[:600]}\n\n"
            "Now give the final Files:/Symbols:/Locations: answer. Only read again "
            "if a needed location is genuinely missing above; do not reopen the "
            "other candidates."
        )
        history.clear()
        if system:
            history.add_message(system)
        history.add_message({"role": "user", "content": seed})
        return 1

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _force_final_answer(self, history, usage_tracker, usage_turn: int) -> str:
        """Tool-free turn that extracts a synthesized natural-language answer.

        Demo counterpart to ``_force_schema_answer``: when the agent runs out of
        turns still exploring (last message is chatter like "Let me look at ..."),
        spend one extra tool-free turn asking it to answer from what it already
        gathered, so the user gets a real answer instead of mid-search narration.
        These are extra turns; they do NOT consume the exploration budget.
        """
        history.add_message(
            {
                "role": "user",
                "content": (
                    "Stop searching and do not call any more tools. Using "
                    "everything you have already read, write your complete final "
                    "answer to the user's question now, citing the relevant files "
                    "and symbols."
                ),
            }
        )
        try:
            overrides: Dict[str, Any] = {
                "usage_tracker": usage_tracker,
                "usage_turn": usage_turn,
            }
            if self.tools:
                overrides["tools"] = self.tools
                overrides["tool_choice"] = "none"
            final = self.llm._call_raw(history.get_messages(), **overrides)
            forced_msg = final.choices[0].message
            history.add_message(_message_to_dict(forced_msg))
            return getattr(forced_msg, "content", None) or ""
        except Exception as exc:  # noqa: BLE001 - best-effort; keep the partial run
            logger.warning("forced final answer failed: %s", exc)
            return ""

    def _force_schema_answer(
        self, history, usage_tracker, usage_turn: int, read_paths=None
    ) -> str:
        """Tool-free turn(s) that extract a contract-conforming final answer.

        Used when the agent stopped (budget hit, or terminated in prose) without
        emitting the ``Files:/Symbols:/Locations:`` contract — so a genuine
        localization is not lost to formatting / an unfinished answer. These are
        extra turns; they do NOT consume the exploration budget. Anthropic rejects
        a tool-history conversation unless ``tools=`` is passed, so we pass it
        with ``tool_choice="none"`` to forbid further calls.

        Some runs end in prose or emit a partial contract (e.g. ``Files:`` but no
        ``Locations:``) even after the prompt requested structured localization.
        So we (a) remind the model which files it actually read — the answer must
        come from those — and (b) retry until the full 3-line contract is present,
        up to a small bound.
        """
        files_hint = ""
        uniq = list(dict.fromkeys(read_paths or []))
        if uniq:
            files_hint = (
                "\nYou read these files — your answer MUST cite line ranges from "
                "them:\n" + "\n".join(f"- {p}" for p in uniq[:10])
            )
        out = ""
        for attempt in range(2):
            nudge = (
                ""
                if attempt == 0
                else (
                    "\nYour previous answer was missing the Locations: line with "
                    "explicit start-end line numbers. Add ALL three lines now."
                )
            )
            history.add_message(
                {
                    "role": "user",
                    "content": (
                        "Stop. Do not call any more tools. Give your final answer "
                        "NOW in EXACTLY this format — all three lines, "
                        "repo-relative paths, Locations with start-end line "
                        f"numbers as shown by `read`:\n{LOCALIZATION_SCHEMA}"
                        f"{files_hint}{nudge}"
                    ),
                }
            )
            try:
                overrides: Dict[str, Any] = {
                    "usage_tracker": usage_tracker,
                    "usage_turn": usage_turn + attempt,
                }
                if self.tools:
                    overrides["tools"] = self.tools
                    overrides["tool_choice"] = "none"
                final = self.llm._call_raw(history.get_messages(), **overrides)
                forced_msg = final.choices[0].message
                history.add_message(_message_to_dict(forced_msg))
                out = getattr(forced_msg, "content", None) or out
            except Exception as exc:  # best-effort; keep the partial run
                logger.warning("forced final-answer turn failed: %s", exc)
                break
            if _has_localization_contract(out):
                break
            # Only retry to COMPLETE a partial contract (e.g. Files: present but
            # Locations: missing). If the model emitted no contract line at all
            # (pure prose), retrying won't help — stop and avoid a wasted call.
            if not (
                _HAS_FILES_CONTRACT.search(out)
                or _HAS_SYMBOLS_CONTRACT.search(out)
                or _HAS_LOCATIONS_CONTRACT.search(out)
            ):
                break
        return out

    def _new_history(self):
        """Build the chat-history container for one ``run()``.

        Returns a :class:`~codeminer.agent.history.TokenBudgetedChatHistory`
        when ``max_context_tokens`` is set (so a long loop can't overflow the
        model context), otherwise an unbounded
        :class:`~codeminer.agent.history.PlainChatHistory` that behaves like
        the original bare message list.
        """
        if self.max_context_tokens:
            model = getattr(self.llm, "model", None)
            return TokenBudgetedChatHistory(self.max_context_tokens, model=model)
        return PlainChatHistory()

    def _execute_tool_call(self, tc: Any) -> ToolCallRecord:
        """Execute a single tool call (a skill or a default tool)."""
        skill_id = tc.function.name
        try:
            arguments = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, TypeError):
            arguments = {}

        # Skills and default tools live in separate registries; check both.
        meta = self.registry.get(skill_id) or self.tool_registry.get(skill_id)
        if meta is None or meta.executor_fn is None:
            return ToolCallRecord(
                tool_call_id=tc.id,
                skill_id=skill_id,
                arguments=arguments,
                error=f"Tool {skill_id!r} not available",
            )

        # Apply parameter scaling (config defaults + session), then validate +
        # coerce the LLM's JSON arguments against the declared input types.
        resolved_args = self._resolve_params(meta, arguments)
        # Agent boundary (#153): inputs declared ``is_line_number`` arrive
        # 1-based from the LLM; convert to 0-based BEFORE coercion so
        # ``coerce_args`` validates/coerces the internal 0-based values.
        resolved_args = self._apply_input_boundary(meta, resolved_args)
        coerced_args, type_error = coerce_args(meta.inputs, resolved_args)
        if type_error is not None:
            return ToolCallRecord(
                tool_call_id=tc.id,
                skill_id=skill_id,
                arguments=arguments,
                resolved_arguments=resolved_args,
                error=f"invalid arguments for {skill_id!r}: {type_error}",
            )

        start = time.monotonic()
        try:
            result = meta.executor_fn(**coerced_args)
            elapsed = (time.monotonic() - start) * 1000
            logger.debug(
                "tool %s completed in %.0fms",
                skill_id,
                elapsed,
            )
            return ToolCallRecord(
                tool_call_id=tc.id,
                skill_id=skill_id,
                arguments=arguments,
                resolved_arguments=coerced_args,
                result=result,
                duration_ms=elapsed,
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            logger.warning("tool %s failed: %s", skill_id, exc)
            return ToolCallRecord(
                tool_call_id=tc.id,
                skill_id=skill_id,
                arguments=arguments,
                resolved_arguments=coerced_args,
                error=str(exc),
                duration_ms=elapsed,
            )

    def _resolve_params(self, meta: Any, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Merge config defaults + session adjustments + LLM arguments."""
        if self.session_ctx is None:
            return arguments

        from ..compiler.params import resolve_params

        resolved = resolve_params(
            defaults=meta.defaults or {},
            session_ctx=self.session_ctx,
            query_params=arguments,
        )
        return resolved.params

    def _apply_input_boundary(self, meta: Any, args: Dict[str, Any]) -> Dict[str, Any]:
        """Convert ``is_line_number`` inputs from 1-based (LLM) to 0-based.

        No-op for skills that declare no line-number inputs, so the common
        path is untouched. See ``codeminer/agent/boundary.py`` (#153).
        """
        line_params = {
            i.name
            for i in getattr(meta, "inputs", [])
            if getattr(i, "is_line_number", False)
        }
        if not line_params:
            return args
        converted = dict(args)
        for name in line_params:
            if name in converted:
                converted[name] = from_agent_repr_arg(converted[name])
        return converted


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _message_to_dict(msg: Any) -> Dict[str, Any]:
    """Convert a litellm response message to a raw dict."""
    if hasattr(msg, "model_dump"):
        d = msg.model_dump(exclude_none=True)
    elif hasattr(msg, "to_dict"):
        d = msg.to_dict()
    else:
        d = {
            "role": getattr(msg, "role", "assistant"),
            "content": getattr(msg, "content", None),
        }
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            d["tool_calls"] = [
                tc.model_dump(exclude_none=True) if hasattr(tc, "model_dump") else tc
                for tc in tool_calls
            ]
    return d


def _serialize_result(result: Any) -> str:
    """Serialize a tool result to a string for the LLM."""
    if isinstance(result, str):
        text = result
    elif isinstance(result, (list, tuple)):
        items = []
        for item in result:
            # Agent boundary (#153): emit line numbers 1-based to the LLM so a
            # result's structured ``start_line`` agrees with its rendered
            # ``content`` gutter (and round-trips through ``graph_expand``).
            if is_line_bearing(item):
                items.append(to_agent_repr(item))
            elif hasattr(item, "model_dump"):
                items.append(item.model_dump(exclude_none=True))
            elif hasattr(item, "__dict__"):
                items.append(item.__dict__)
            else:
                items.append(item)
        text = json.dumps(items, default=str, ensure_ascii=False)
    elif is_line_bearing(result):
        text = json.dumps(to_agent_repr(result), default=str, ensure_ascii=False)
    elif hasattr(result, "model_dump"):
        text = json.dumps(result.model_dump(exclude_none=True), default=str)
    else:
        try:
            text = json.dumps(result, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(result)

    if len(text) > _MAX_RESULT_CHARS:
        text = text[:_MAX_RESULT_CHARS] + "\n... (truncated)"
    return text


def _trace_tool_call_data(
    record: ToolCallRecord,
    serialized_result: str,
) -> Dict[str, Any]:
    """Summarize a tool call for runtime trace events."""

    status = "error" if record.error is not None else "ok"
    result = record.result
    result_count: Optional[int] = None
    if isinstance(result, (list, tuple)):
        result_type = "sequence"
        result_count = len(result)
    elif isinstance(result, Mapping):
        result_type = "mapping"
    elif result is None:
        result_type = "none"
    else:
        result_type = type(result).__name__

    data: Dict[str, Any] = {
        "tool_call_id": record.tool_call_id,
        "tool": record.skill_id,
        "arguments": dict(record.arguments or {}),
        "status": status,
        "duration_ms": record.duration_ms,
        "result_type": "error" if status == "error" else result_type,
        "result_chars": len(serialized_result or ""),
        "truncated": bool(serialized_result.endswith("\n... (truncated)")),
    }
    if record.resolved_arguments is not None:
        data["resolved_arguments"] = dict(record.resolved_arguments or {})
    if result_count is not None:
        data["result_count"] = result_count
    provider_metadata = lsp_result_metadata(result)
    if provider_metadata:
        data["lsp_provider"] = provider_metadata
        if status == "ok" and isinstance(result, (list, tuple)):
            data["lsp_result_fingerprint"] = fingerprint_lsp_result(result)
            data["lsp_result_preview"] = preview_lsp_result(result)
    if record.skill_id == "lsp_route":
        route_args = _lsp_route_trace_args(record)
        data["route_args"] = route_args
        if status == "ok" and isinstance(result, (list, tuple)):
            data["route_fingerprint"] = data.get(
                "lsp_result_fingerprint"
            ) or fingerprint_lsp_route_nodes(result)
            data["route_preview"] = data.get("lsp_result_preview") or (
                summarize_lsp_route_nodes(result)
            )
    if record.error is not None:
        data["error"] = record.error
    return data


def _lsp_route_trace_args(record: ToolCallRecord) -> Dict[str, Any]:
    args = record.resolved_arguments or record.arguments or {}
    return canonical_lsp_route_args(
        symbols=args.get("symbols") or [],
        query=args.get("query"),
        top_k=args.get("top_k", 12),
        include_neighbors=args.get("include_neighbors", True),
    )


def _context_state(
    record: ToolCallRecord,
    serialized_result: str,
    successful_read: bool,
) -> str:
    if record.skill_id == "read":
        return "read" if successful_read else "rejected"
    if record.error is not None:
        return "rejected"
    if serialized_result.lstrip().startswith("Error"):
        return "rejected"
    return "offered"


def _context_ledger_data(
    record: ToolCallRecord,
    serialized_result: str,
) -> Dict[str, Any]:
    event_data = _trace_tool_call_data(record, serialized_result)
    path = None
    if record.skill_id == "read":
        path_arg = (record.arguments or {}).get("file_path")
        path = str(path_arg) if path_arg is not None else None

    metadata = {
        key: value
        for key, value in event_data.items()
        if key
        not in {
            "tool_call_id",
            "tool",
            "arguments",
        }
    }
    metadata["arguments"] = dict(record.arguments or {})

    summary = _context_summary(record, event_data, serialized_result)
    return {
        "summary": summary,
        "path": path,
        "tool_call_id": record.tool_call_id,
        "provenance": {
            "kind": "tool_result",
            "tool": record.skill_id,
            "tool_call_id": record.tool_call_id,
        },
        "freshness": "fresh",
        "token_estimate": _estimate_context_tokens(serialized_result),
        "metadata": metadata,
    }


def _estimate_context_tokens(text: str) -> int:
    """Cheap token estimate for ledger accounting, matching history fallback."""

    if not text:
        return 0
    return max(
        1, (len(text) + _CHARS_PER_TOKEN_ESTIMATE - 1) // _CHARS_PER_TOKEN_ESTIMATE
    )


def _context_summary(
    record: ToolCallRecord,
    event_data: Mapping[str, Any],
    serialized_result: str,
) -> str:
    tool = record.skill_id
    result_chars = event_data.get("result_chars", 0)
    if record.error is not None:
        return f"{tool} error: {record.error}"
    if serialized_result.lstrip().startswith("Error"):
        first_line = serialized_result.strip().splitlines()[0]
        return f"{tool} rejected: {first_line[:160]}"
    if tool == "read":
        path = (record.arguments or {}).get("file_path") or "(unknown)"
        return f"read {path} ({result_chars} chars)"
    if "result_count" in event_data:
        return (
            f"{tool} returned {event_data['result_count']} item(s) "
            f"({result_chars} chars)"
        )
    return f"{tool} returned {event_data.get('result_type', 'result')} ({result_chars} chars)"


# ---------------------------------------------------------------------------
# Public facade: query() + CodeMinerAgentOptions
# ---------------------------------------------------------------------------
#
# A repo-manifest-aware entry point that bundles the three things an
# outside caller currently has to wire by hand:
#
#   1. Pre-compile  — building / loading the BM25, FAISS, and symbol-graph
#      indexes the requested skills need (``build_skill_contexts``).
#   2. Skill loading — registering skill packages from
#      ``codeminer/agent/skills``.
#   3. Agent loop   — constructing ``AgentRunner`` with the right
#      ``SessionContext`` and (optional) ``compile_table`` and running it.
#
# Shape mirrors the Claude Agent SDK's ``query(prompt, options=...)``
# ergonomics. ``query()`` is sync today — ``AgentRunner.run()`` is sync,
# and this facade keeps that contract.


@dataclass
class CodeMinerAgentOptions:
    """Configuration for a single ``query()`` invocation.

    **Exactly one** of ``repo_path``, ``contexts``, or ``manifest`` must
    be set — these are the three mutually-exclusive ways to tell
    ``query()`` where its indexes come from:

    * ``repo_path`` → ``query()`` calls
      :func:`~codeminer.compiler.build_skill_contexts` itself and caches
      indexes under ``index_cache_dir`` (or ``<repo>/.codeminer_cache``).
      The "build once at first query" path.
    * ``contexts`` → caller pre-built the contexts dict (advanced; see
      :func:`~codeminer.compiler.build_skill_contexts` /
      :func:`~codeminer.compiler.load_contexts_from_manifest` for what
      shape to pass).
    * ``manifest`` → caller compiled indexes ahead of time via
      :func:`~codeminer.agent.compile_repo` (or
      :class:`~codeminer.compiler.IndexCompiler` directly) and passes
      either a loaded :class:`~codeminer.compiler.RepoManifest` or a
      path string to ``repo_manifest.json``. The AoT (ahead-of-time)
      path: ``query()`` loads the artifacts named in the manifest and
      threads the manifest itself into ``AgentRunner`` so
      :class:`~codeminer.agent.resource_guard.ResourceGuard` can run
      freshness checks. No inline build happens.

    Skill selection forms a three-layer funnel::

        registry  ⊇  allowed_skills  ⊇  compile_table[scenario]

    ``compile_table`` *narrows* ``allowed_skills`` per query but never
    broadens it (see :func:`codeminer.agent.compile.agent_compile`).

    ``compile_table`` also operates at the **index-build stage** (for
    ``repo_path`` mode only): when set, only indexes for skills it ever
    names are compiled. Formally::

        index_skills = allowed_skills  ∩  union(compile_table.values())

    A vector index isn't built if every scenario in the table maps to
    bm25-only, even when ``embedding_search`` is in ``allowed_skills`` —
    CAR couldn't route to it at runtime anyway. (In ``manifest`` mode
    this rule is moot — the manifest dictates what exists.)
    """

    # --- index source: exactly one of these three must be set ---
    repo_path: Optional[str] = None
    contexts: Optional[Dict[str, Any]] = None
    manifest: Optional[Union["RepoManifest", str, Path]] = None

    # --- pre-compile knobs (only consulted in repo_path mode) ---
    languages: Sequence[str] = ("python",)
    primary_language: Optional[str] = None
    repo_size: Optional[int] = None
    index_cache_dir: Optional[str] = None
    embedding_model: str = "nomic-ai/CodeRankEmbed"
    embedding_dimension: int = 768
    default_top_k: int = 10
    default_level: str = "l2"
    rebuild_indexes: bool = False
    skills_dir: Optional[str] = None

    # --- skill gating ---
    allowed_skills: Optional[List[str]] = None
    excluded_skills: Optional[List[str]] = None
    compile_table: Optional[CompileTableInput] = None
    skill_params: Optional[Dict[str, Dict[str, Any]]] = None

    # --- LLM / agent loop ---
    llm: Optional[LiteLLMChat] = None
    model: Optional[str] = None
    temperature: float = 0.0
    max_tokens: int = 512
    system_prompt: Optional[str] = None
    max_turns: int = 10
    max_context_tokens: Optional[int] = None
    enable_lsp_route_context: bool = False
    lsp_route_seed_limit: int = 8
    lsp_route_seed_policy: str = "all"
    lsp_route_query_fallback: bool = False
    lsp_route_top_k: int = 12
    lsp_route_include_neighbors: bool = True
    retry: Optional[RetryConfig] = None

    # --- extras for SessionContext.extras ---
    session_extras: Dict[str, Any] = field(default_factory=dict)


def query(
    prompt: str,
    *,
    options: Optional[CodeMinerAgentOptions] = None,
) -> AgentResult:
    """Run one agent turn over a repo and return the result.

    See :class:`CodeMinerAgentOptions` for the full options surface,
    including the three mutually-exclusive index-source modes
    (``repo_path``, ``contexts``, ``manifest``).

    Raises:
        ValueError: if not exactly one of ``options.repo_path``,
            ``options.contexts``, or ``options.manifest`` is set; or if a
            manifest is supplied but is missing an index required by
            ``options.allowed_skills``.
    """
    opts = options or CodeMinerAgentOptions()

    _check_exactly_one_mode(opts)

    if opts.llm is None and opts.model is None:
        model_for_llm: Optional[str] = _DEFAULT_MODEL
    else:
        model_for_llm = opts.model

    # --- 1. Resolve the compile_table (path → dict if needed) ---
    table = _load_compile_table_if_path(opts.compile_table)

    # --- 1b. Resolve the manifest (path → loaded RepoManifest if needed).
    # We do this early so the resolved manifest can be threaded into both
    # ``load_contexts_from_manifest`` (loading) and ``AgentRunner`` (for
    # ``ResourceGuard`` freshness checks).
    manifest = _load_manifest_if_path(opts.manifest)

    # --- 1c. Surface allowed_skills ↔ compile_table mismatches before any
    # work happens. Two failure modes the caller almost certainly didn't
    # intend; both fail at runtime ("Skill 'X' not available") deep in
    # the agent loop, which is a miserable debugging path.
    _warn_on_skill_set_mismatch(
        set(opts.allowed_skills) if opts.allowed_skills else None,
        table,
    )

    # --- 2. Reset the singleton registry so this query's contexts win.
    # SkillLoader.load_all() skips already-registered skills, so without a
    # reset we'd run against whatever the previous query loaded.
    SkillRegistry.reset()
    registry = SkillRegistry()
    skills_dir = opts.skills_dir or str(_DEFAULT_SKILLS_DIR)
    loader = SkillLoader()

    # --- 3. Resolve contexts according to the selected mode.
    # ``build_skill_contexts`` (repo_path mode) reads each skill's
    # index_requirements straight off ``config.yaml`` on disk (see #154), so
    # it no longer needs a metadata-only pre-pass of SkillLoader. The manifest
    # mode still reads requirements from the registry, so it keeps its
    # pre-pass + reset. The ``contexts`` mode skips both — the caller already
    # did the equivalent.
    if opts.contexts is not None:
        contexts = opts.contexts
    elif manifest is not None:
        # AoT mode: load indexes directly from the manifest's recorded
        # paths. No build step.
        loader.load_all(skills_dir, contexts={}, registry=registry)
        skill_ids = (
            list(opts.allowed_skills)
            if opts.allowed_skills
            else _discover_skill_ids(skills_dir)
        )
        from ..compiler import load_contexts_from_manifest

        contexts = load_contexts_from_manifest(
            manifest,
            skill_ids=skill_ids,
            skill_registry=registry,
            default_top_k=opts.default_top_k,
            default_level=opts.default_level,
        )
        SkillRegistry.reset()
        registry = SkillRegistry()
    else:
        # repo_path mode: inline build via build_skill_contexts. It resolves
        # index_requirements from config.yaml on disk, so no metadata pre-pass
        # (and no reset to undo it) is needed before the real load in step 4.
        contexts = _build_contexts(opts, table=table)

    # --- 4. Load (or reload) the skill packages with the real contexts ---
    loaded = loader.load_all(skills_dir, contexts=contexts, registry=registry)
    logger.debug("query(): loaded %d skills from %s", len(loaded), skills_dir)

    # Apply caller-supplied per-skill default overrides (Layer 1+2 of
    # ``resolve_params``). We mutate the metadata in place because the
    # registry holds references; this only affects the current process,
    # and ``SkillRegistry.reset()`` on the next call clears the slate.
    if opts.skill_params:
        for skill_id, overrides in opts.skill_params.items():
            meta = registry.get(skill_id)
            if meta is None:
                logger.warning(
                    "skill_params: skill %r not in registry, ignoring %r",
                    skill_id,
                    sorted(overrides),
                )
                continue
            merged = dict(meta.defaults or {})
            merged.update(overrides)
            meta.defaults = merged

    # --- 5. Build the SessionContext for CAR + parameter scaling ---
    from ..compiler.params import SessionContext

    session_ctx = SessionContext(
        repo_path=opts.repo_path,
        repo_size=opts.repo_size,
        primary_language=opts.primary_language,
        extras=dict(opts.session_extras),
    )

    # --- 6. Construct the runner and execute ---
    # Pass the *resolved* manifest (RepoManifest | None) so ResourceGuard
    # sees the loaded object — never a raw path string.
    runner = AgentRunner(
        llm=opts.llm,
        registry=registry,
        model=model_for_llm,
        temperature=opts.temperature,
        max_tokens=opts.max_tokens,
        system_prompt=opts.system_prompt,
        max_turns=opts.max_turns,
        max_context_tokens=opts.max_context_tokens,
        enable_lsp_route_context=opts.enable_lsp_route_context,
        lsp_route_seed_limit=opts.lsp_route_seed_limit,
        lsp_route_seed_policy=opts.lsp_route_seed_policy,
        lsp_route_query_fallback=opts.lsp_route_query_fallback,
        lsp_route_top_k=opts.lsp_route_top_k,
        lsp_route_include_neighbors=opts.lsp_route_include_neighbors,
        retry=opts.retry,
        allow_skills=set(opts.allowed_skills) if opts.allowed_skills else None,
        exclude_skills=set(opts.excluded_skills) if opts.excluded_skills else None,
        manifest=manifest,
        session_ctx=session_ctx,
        compile_table=table,
    )
    return runner.run(prompt)


def _check_exactly_one_mode(opts: CodeMinerAgentOptions) -> None:
    """Enforce exactly one of {repo_path, contexts, manifest} is set.

    The three modes are conceptually disjoint (build now / consume
    pre-built dict / consume pre-built manifest); silent precedence
    ordering would be a footgun. Raise loudly at ``query()`` entry
    instead of letting one mode silently mask another.
    """
    set_modes = []
    if opts.repo_path is not None:
        set_modes.append("repo_path")
    if opts.contexts is not None:
        set_modes.append("contexts")
    if opts.manifest is not None:
        set_modes.append("manifest")

    if len(set_modes) == 1:
        return

    if not set_modes:
        raise ValueError(
            "CodeMinerAgentOptions requires exactly one of 'repo_path' "
            "(inline build), 'contexts' (pre-built dict), or 'manifest' "
            "(AoT-compiled RepoManifest or path). All three are unset."
        )
    raise ValueError(
        f"CodeMinerAgentOptions requires exactly one of 'repo_path', "
        f"'contexts', or 'manifest' — got {len(set_modes)} set: "
        f"{sorted(set_modes)}."
    )


def _load_manifest_if_path(
    manifest: Optional[Union["RepoManifest", str, Path]],
) -> Optional["RepoManifest"]:
    """Coerce a path-or-instance manifest argument into a ``RepoManifest``.

    Returns ``None`` if ``manifest is None``. A string or ``Path`` is
    treated as a filesystem path to a ``repo_manifest.json`` file; the
    file must exist. An already-loaded :class:`RepoManifest` is returned
    unchanged.
    """
    if manifest is None:
        return None
    if isinstance(manifest, RepoManifest):
        return manifest
    if isinstance(manifest, (str, Path)):
        p = Path(manifest)
        if not p.exists():
            raise FileNotFoundError(f"options.manifest path does not exist: {p}")
        return RepoManifest.load(p)
    raise TypeError(
        f"options.manifest must be a RepoManifest, path, or None — "
        f"got {type(manifest).__name__}"
    )


def _load_compile_table_if_path(
    table: Optional[CompileTableInput],
) -> Optional[Dict[str, Any]]:
    """Coerce a path-or-dict ``compile_table`` argument into a dict."""
    if table is None:
        return None
    if isinstance(table, (str, Path)):
        from .compile import load_compile_table

        loaded = load_compile_table(Path(table))
        return dict(loaded)
    if isinstance(table, Mapping):
        return dict(table)
    raise TypeError(
        f"options.compile_table must be a path or mapping, got {type(table).__name__}"
    )


def _warn_on_skill_set_mismatch(
    allowed: Optional[Set[str]],
    table: Optional[Dict[str, Any]],
) -> None:
    """Warn when ``allowed_skills`` and ``compile_table`` don't line up.

    Two asymmetries are worth surfacing because both fail deep in the
    agent loop rather than at config time:

    * **orphans in allowed_skills** (``allowed - union(table.values())``):
      CAR can never pick these for any scenario in the table. They become
      reachable only on a *table miss* (CAR fallback to base_allow), and
      because ``_build_contexts`` correctly skips them at pre-compile,
      their executors will be unbound — the LLM will be offered tools
      that crash with "Skill 'X' not available".
    * **overflow in table** (``union(table.values()) - allowed``): CAR
      will compute these for matching scenarios, but
      ``AgentRunner.run`` silently drops them via
      ``table_allow ∩ base_allow`` before reaching the LLM. The table
      entry is dead.

    Warn only — caller might intentionally keep orphans as a safety net
    on table miss. Empty inputs short-circuit (nothing to compare).
    """
    if not allowed or not table:
        return
    table_skills: Set[str] = set()
    for v in table.values():
        table_skills.update(v)

    orphans = allowed - table_skills
    if orphans:
        logger.warning(
            "allowed_skills contains %d skill(s) compile_table never names: "
            "%s. These are only reachable on a table miss (CAR fallback); "
            "their indexes are NOT pre-built, so the LLM will fail with "
            "\"Skill 'X' not available\" if it picks one. Fix: add them "
            "to a compile_table scenario, or drop them from allowed_skills.",
            len(orphans),
            sorted(orphans),
        )

    overflow = table_skills - allowed
    if overflow:
        logger.warning(
            "compile_table names %d skill(s) outside allowed_skills: %s. "
            "AgentRunner intersects table_allow with allowed_skills before "
            "exposing tools to the LLM, so these entries are silently "
            "dropped per-query. Fix: add them to allowed_skills, or "
            "remove them from compile_table.",
            len(overflow),
            sorted(overflow),
        )


def _build_contexts(
    opts: CodeMinerAgentOptions,
    *,
    table: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the index union the requested skills need.

    Skill set used for pre-compile:

        index_skills = allowed_skills  ∩  union(table.values())   (if table)
                     = allowed_skills                             (otherwise)

    ``allowed_skills`` is the *agent's* upper bound (what the LLM can ever
    pick); ``compile_table`` is the *index's* upper bound — if a skill is
    never named on the right-hand side of any scenario, CAR can't route to
    it, so its index never needs to exist. Intersecting the two avoids
    building indexes that the runtime would never read (e.g. a vector
    index when every compile_table scenario maps to bm25-only).

    Delegates to ``codeminer.compiler.build_skill_contexts`` — the same
    pre-compile entry point used by ``examples/skill_agent_eval.py``.
    """
    from ..compiler import build_skill_contexts

    if opts.allowed_skills:
        skill_ids: Set[str] = set(opts.allowed_skills)
    else:
        skill_ids = set(
            _discover_skill_ids(opts.skills_dir or str(_DEFAULT_SKILLS_DIR))
        )

    if table:
        table_skills: Set[str] = set()
        for v in table.values():
            table_skills.update(v)
        skill_ids = skill_ids & table_skills
        logger.debug(
            "query(): compile_table narrows index-build set to %d skill(s): %s",
            len(skill_ids),
            sorted(skill_ids),
        )

    return build_skill_contexts(
        repo_path=opts.repo_path,  # checked non-None by ``query``
        skill_ids=sorted(skill_ids),
        languages=tuple(opts.languages),
        cache_dir=opts.index_cache_dir,
        skills_dir=opts.skills_dir or str(_DEFAULT_SKILLS_DIR),
        embedding_model=opts.embedding_model,
        embedding_dimension=opts.embedding_dimension,
        default_top_k=opts.default_top_k,
        default_level=opts.default_level,
        rebuild=opts.rebuild_indexes,
    )


def _discover_skill_ids(skills_dir: str) -> List[str]:
    """Enumerate skill IDs from a packages directory by reading config.yaml."""
    import yaml

    out: List[str] = []
    root = Path(skills_dir)
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        cfg = entry / "config.yaml"
        if not cfg.exists():
            continue
        try:
            with open(cfg) as f:
                data = yaml.safe_load(f) or {}
            sid = data.get("skill_id")
            if sid:
                out.append(sid)
        except Exception as exc:
            logger.warning("skipping malformed skill config %s: %s", cfg, exc)
    return out


# ---------------------------------------------------------------------------
# Public AoT helper: compile_repo()
# ---------------------------------------------------------------------------


def compile_repo(
    repo_path: str,
    *,
    index_types: Sequence[str] = ("bm25",),
    languages: Sequence[str] = ("python",),
    cache_dir: Optional[str] = None,
    embedding_model: str = "nomic-ai/CodeRankEmbed",
    embedding_dimension: int = 768,
) -> "RepoManifest":
    """Compile indexes for *repo_path* ahead of time and return the manifest.

    Thin convenience wrapper over :class:`~codeminer.compiler.IndexCompiler`
    that registers the default index builders for the requested
    ``languages`` and writes ``<cache_dir>/repo_manifest.json``.

    Pair with :func:`query` to run the agent against the result without
    re-indexing on every call::

        manifest = compile_repo(
            "/path/to/repo",
            index_types=("bm25", "vector"),
            languages=("python",),
        )
        result = query(
            "where is auth wired up?",
            options=CodeMinerAgentOptions(
                manifest=manifest,
                allowed_skills=["bm25_search", "embedding_search"],
            ),
        )

    For advanced cases — custom builder registries, partial rebuilds —
    use :class:`~codeminer.compiler.IndexCompiler` directly.
    """
    from ..compiler.index_builders import (
        IndexBuilderRegistry,
        register_default_builders,
    )
    from ..compiler.index_compiler import IndexCompiler, IndexCompilerConfig

    if cache_dir is None:
        cache_dir = str(Path(repo_path).resolve() / ".codeminer_cache")

    builder_registry = IndexBuilderRegistry()
    register_default_builders(
        builder_registry,
        languages=list(languages),
        embedding_model=embedding_model,
        embedding_dimension=embedding_dimension,
    )

    cfg = IndexCompilerConfig(
        cache_dir_name=Path(cache_dir).name,
        index_types=list(index_types),
        languages=list(languages),
    )
    compiler = IndexCompiler(builder_registry, cfg)
    return compiler.compile_repo(
        repo_path,
        index_types=list(index_types),
        cache_dir=cache_dir,
    )
