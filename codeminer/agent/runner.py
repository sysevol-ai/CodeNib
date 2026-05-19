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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Union

from ..llm.litellm_chat import LiteLLMChat
from ..llm.usage import UsageTracker
from ..log_utils import get_logger
from .agent_types import AgentResult, ToolCallRecord
from .skills.loader import SkillLoader
from .skills.registry import SkillRegistry
from .tool_schema import registry_to_tools

logger = get_logger(__name__)

_DEFAULT_MODEL = "claude-sonnet-4-6"

# Path to the bundled skill packages. ``query()`` defaults to this so
# callers don't have to know where skills live on disk.
_DEFAULT_SKILLS_DIR: Path = Path(__file__).parent / "skills"

CompileTableInput = Union[str, Path, Mapping[str, Any]]

_DEFAULT_SYSTEM_PROMPT = """\
You are a code search agent. You have access to tools that search a \
codebase and retrieve relevant code snippets. Use the tools iteratively \
to find the information needed, then provide a concise answer.

Guidelines:
- Start with broad searches, then narrow down.
- Use graph_expand to find structurally related code after an initial search.
- When you have enough context, provide a final answer directly.
- Prefer lower-cost skills unless the query clearly requires semantic understanding.
- For simple exact-name lookups use bm25_search; \
for conceptual / intent queries use embedding_search; \
for maximum coverage use hybrid_search.
"""

# Maximum characters for a single tool result to avoid context blowup.
_MAX_RESULT_CHARS = 16_000


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
        allow_skills: Optional[Set[str]] = None,
        exclude_skills: Optional[Set[str]] = None,
        manifest: Optional[Any] = None,
        session_ctx: Optional[Any] = None,
        compile_table: Optional[Any] = None,
    ) -> None:
        if llm is not None:
            self.llm = llm
        elif model is not None:
            self.llm = LiteLLMChat(
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        else:
            raise ValueError("Either 'llm' or 'model' must be provided")
        self.registry = registry or SkillRegistry()
        self.max_turns = max_turns
        self.session_ctx = session_ctx

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

        self._compile_table = compile_table

        # Pre-compute the static tool list. When a compile_table is set,
        # ``run()`` recomputes per-query against the resolved allow set.
        resolved_allow = self._resolve_allow_set(self._base_allow)
        self.tools = registry_to_tools(
            self.registry,
            allow=resolved_allow,
            exclude=self._base_exclude,
        )

        # Build system prompt with optional resource warnings
        base_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT
        if resource_warnings:
            warnings_text = "\n".join(f"- {w}" for w in resource_warnings)
            base_prompt += f"\nIndex warnings:\n{warnings_text}\n"
        self.system_prompt = base_prompt

    # ------------------------------------------------------------------
    # Allow-set resolution (issue #149)
    # ------------------------------------------------------------------

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
    ) -> AgentResult:
        """Execute the agent loop and return the result."""
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
                resolved = self._resolve_allow_set(effective)
                tools = registry_to_tools(
                    self.registry,
                    allow=resolved,
                    exclude=self._base_exclude,
                )

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": query},
        ]
        all_tool_calls: List[ToolCallRecord] = []
        usage_tracker = UsageTracker()
        start = time.monotonic()

        for turn in range(max_turns):
            logger.debug("agent turn %d/%d", turn + 1, max_turns)

            call_kwargs: Dict[str, Any] = {
                "usage_tracker": usage_tracker,
                "usage_turn": turn + 1,
            }
            if tools:
                call_kwargs["tools"] = tools

            response = self.llm._call_raw(messages, **call_kwargs)
            choice = response.choices[0]
            assistant_msg = choice.message

            # Append assistant message to conversation
            messages.append(_message_to_dict(assistant_msg))

            # Check for tool calls
            tool_calls = getattr(assistant_msg, "tool_calls", None)
            if not tool_calls:
                # Terminal: LLM produced a final answer
                answer = getattr(assistant_msg, "content", None) or ""
                elapsed = (time.monotonic() - start) * 1000
                return AgentResult(
                    answer=answer,
                    tool_calls=all_tool_calls,
                    messages=messages,
                    total_turns=turn + 1,
                    total_duration_ms=elapsed,
                    usage=usage_tracker.totals(),
                    usage_records=list(usage_tracker.records),
                )

            # Execute each tool call
            for tc in tool_calls:
                record = self._execute_tool_call(tc)
                all_tool_calls.append(record)

                # Append tool response message
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": _serialize_result(
                            record.result if record.error is None else record.error
                        ),
                    }
                )

        # Max turns exhausted — return whatever we have
        elapsed = (time.monotonic() - start) * 1000
        last_content = ""
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                last_content = msg["content"]
                break

        return AgentResult(
            answer=last_content,
            tool_calls=all_tool_calls,
            messages=messages,
            total_turns=max_turns,
            total_duration_ms=elapsed,
            usage=usage_tracker.totals(),
            usage_records=list(usage_tracker.records),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _execute_tool_call(self, tc: Any) -> ToolCallRecord:
        """Execute a single tool call from the LLM response."""
        skill_id = tc.function.name
        try:
            arguments = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, TypeError):
            arguments = {}

        meta = self.registry.get(skill_id)
        if meta is None or meta.executor_fn is None:
            return ToolCallRecord(
                tool_call_id=tc.id,
                skill_id=skill_id,
                arguments=arguments,
                error=f"Skill {skill_id!r} not available",
            )

        # Apply parameter scaling if session context is available
        resolved_args = self._resolve_params(meta, arguments)

        start = time.monotonic()
        try:
            result = meta.executor_fn(**resolved_args)
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
            if hasattr(item, "model_dump"):
                items.append(item.model_dump(exclude_none=True))
            elif hasattr(item, "__dict__"):
                items.append(item.__dict__)
            else:
                items.append(item)
        text = json.dumps(items, default=str, ensure_ascii=False)
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

    Either ``repo_path`` *or* ``contexts`` must be set:

    * ``repo_path`` set → ``query()`` calls ``build_skill_contexts`` itself
      and caches indexes under ``index_cache_dir`` (or ``<repo>/.codeminer``).
    * ``contexts`` set → caller has already built the contexts dict; the
      pre-compile step is skipped. Useful for sharing one index across many
      queries.

    Skill selection forms a three-layer funnel::

        registry  ⊇  allowed_skills  ⊇  compile_table[scenario]

    ``compile_table`` *narrows* ``allowed_skills`` per query but never
    broadens it (see :func:`codeminer.agent.compile.agent_compile`).
    """

    # --- repo / pre-compile ---
    repo_path: Optional[str] = None
    contexts: Optional[Dict[str, Any]] = None
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

    # --- ResourceGuard manifest passthrough ---
    manifest: Optional[Any] = None

    # --- extras for SessionContext.extras ---
    session_extras: Dict[str, Any] = field(default_factory=dict)


def query(
    prompt: str,
    *,
    options: Optional[CodeMinerAgentOptions] = None,
) -> AgentResult:
    """Run one agent turn over a repo and return the result.

    See :class:`CodeMinerAgentOptions` for the full options surface.

    Raises:
        ValueError: if neither ``options.repo_path`` nor ``options.contexts``
            is set.
    """
    opts = options or CodeMinerAgentOptions()

    if opts.contexts is None and opts.repo_path is None:
        raise ValueError(
            "CodeMinerAgentOptions requires either 'repo_path' (to pre-compile "
            "indexes) or 'contexts' (a pre-built skill-context dict). Both are unset."
        )
    if opts.llm is None and opts.model is None:
        model_for_llm: Optional[str] = _DEFAULT_MODEL
    else:
        model_for_llm = opts.model

    # --- 1. Resolve the compile_table (path → dict if needed) ---
    table = _load_compile_table_if_path(opts.compile_table)

    # --- 2. Reset the singleton registry so this query's contexts win.
    # SkillLoader.load_all() skips already-registered skills, so without a
    # reset we'd run against whatever the previous query loaded.
    SkillRegistry.reset()
    registry = SkillRegistry()

    # --- 3. Pre-compile indexes if the caller gave us a repo_path ---
    if opts.contexts is not None:
        contexts = opts.contexts
    else:
        contexts = _build_contexts(opts)

    # --- 4. Load the bundled skill packages with these contexts ---
    skills_dir = opts.skills_dir or str(_DEFAULT_SKILLS_DIR)
    loader = SkillLoader()
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
    runner = AgentRunner(
        llm=opts.llm,
        registry=registry,
        model=model_for_llm,
        temperature=opts.temperature,
        max_tokens=opts.max_tokens,
        system_prompt=opts.system_prompt,
        max_turns=opts.max_turns,
        allow_skills=set(opts.allowed_skills) if opts.allowed_skills else None,
        exclude_skills=set(opts.excluded_skills) if opts.excluded_skills else None,
        manifest=opts.manifest,
        session_ctx=session_ctx,
        compile_table=table,
    )
    return runner.run(prompt)


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


def _build_contexts(opts: CodeMinerAgentOptions) -> Dict[str, Any]:
    """Build the index union the requested skills need.

    Delegates to ``codeminer.compiler.build_skill_contexts`` — the same
    pre-compile entry point used by ``examples/skill_agent_eval.py``.
    """
    from ..compiler import build_skill_contexts

    if opts.allowed_skills:
        skill_ids = list(opts.allowed_skills)
    else:
        skill_ids = _discover_skill_ids(opts.skills_dir or str(_DEFAULT_SKILLS_DIR))

    return build_skill_contexts(
        repo_path=opts.repo_path,  # checked non-None by ``query``
        skill_ids=skill_ids,
        languages=tuple(opts.languages),
        cache_dir=opts.index_cache_dir,
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
