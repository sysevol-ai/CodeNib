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

from ..compiler.manifest import RepoManifest
from ..llm.litellm_chat import LiteLLMChat
from ..llm.usage import UsageTracker
from ..log_utils import get_logger
from .agent_types import AgentResult, ToolCallRecord
from .skills.defaults import (
    _SKIP_DIR_PREFIXES,
    DEFAULT_SKILL_IDS,
    ensure_defaults_registered,
)
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
You are a code localization agent. Find the code locations (files and \
symbols) relevant to the request, then give a concise answer naming them.

You always have a filesystem toolset for navigating the repository:
- file_search(mode="files", pattern=...) — list files by glob (explore layout)
- file_search(mode="content", pattern=...) — grep file contents (regex/literal)
- file_search(mode="shell", command=...) — run a shell command (ls, find, git)
- file_read(path, start_line, end_line) — read a file or a line range

Depending on the request you may also have retrieval skills:
- bm25_search(query) — fast lexical search for exact names / identifiers
- embedding_search(query) — semantic search for concepts / intent
- hybrid_search(...) — combine retrievers for maximum recall
- find_callers(symbol) / find_callees(symbol) / trace(from_symbol, to_symbol) — \
call-graph navigation (who calls X, what X calls, the path from X to Y). Use \
for impact and to follow a bug across functions — the structural questions \
grep cannot answer. Compact results; file_read the ones you care about.

Fastest path: call **codeminer_context(query)** FIRST — one call searches for \
the relevant entry-point symbols and expands them along the call graph \
(callers + callees), returning a compact map. It usually gives you the edit \
location (or a tight shortlist) without manual grep/read fan-out.

Workflow:
1. ORIENT — start with codeminer_context(query). Only fall back to file_search \
(files/shell) to browse layout if you still need it; the <environment> block \
below lists the top-level entries.
2. LOCATE — if you need more, search for the target: bm25_search for exact \
names, embedding_search for conceptual queries, file_search(mode="content") to \
grep for a literal string or pattern.
3. EXPAND — once you have a relevant symbol, use find_callers / find_callees / \
trace to follow the call graph (who calls it, what it calls, how X reaches Y). \
This answers impact and caller/callee questions far more cheaply than grepping \
for usages by hand.
4. READ — open the most promising files with file_read to confirm.
5. ANSWER — as soon as you can name the location(s) to change, STOP calling \
tools and reply. End your reply with two lines, repo-relative:
   Files: path/one.ext, path/two.ext
   Symbols: path/one.ext:symbol_name, ...
Do not keep exploring once you can name the relevant file(s) — a few \
confirming reads are enough.

Guidelines:
- Start broad, then narrow. Prefer lower-cost tools first.
- Do not loop on the same query; if a tool returns nothing useful, switch \
strategy (grep, read a file, or a different retriever).
- You have a limited tool budget. Converge: once a file looks right, confirm \
it and answer rather than exhaustively reading every candidate.
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
        include_default_tools: bool = True,
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
        # Always-on default tool layer (file_read + file_search): registered
        # unconditionally so every query — and every agent-compile subset —
        # has the filesystem primitives (read / grep / glob / shell) the model
        # is pretrained on. These sit *outside* the allow/compile_table funnel.
        # ``include_default_tools=False`` withholds them — used to force the
        # structured (retrieval + graph) path in cost-comparison experiments.
        self._include_defaults = include_default_tools
        if include_default_tools:
            ensure_defaults_registered(self.registry)
        self._default_ids: Set[str] = (
            set(DEFAULT_SKILL_IDS) if include_default_tools else set()
        )
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

        # Defaults are always-on: no exclude (guard or caller) may drop them.
        self._base_exclude -= self._default_ids

        self._compile_table = compile_table

        # Pre-compute the static tool list. When a compile_table is set,
        # ``run()`` recomputes per-query against the resolved allow set.
        self.tools = self._tools_for(self._base_allow)

        # Build system prompt: base + environment block + resource warnings.
        base_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT
        env_block = self._build_environment_block(self.session_ctx)
        if env_block:
            base_prompt += f"\n{env_block}\n"
        if resource_warnings:
            warnings_text = "\n".join(f"- {w}" for w in resource_warnings)
            base_prompt += f"\nIndex warnings:\n{warnings_text}\n"
        self.system_prompt = base_prompt

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
        except OSError:
            pass
        lines.append("</environment>")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Allow-set resolution (issue #149)
    # ------------------------------------------------------------------

    def _tools_for(self, allow: Optional[Set[str]]) -> List[Dict[str, Any]]:
        """Tool schemas for an allow set, with defaults always unioned in.

        The always-on default layer (``self._default_ids``) is added *after*
        any allow/compile_table narrowing, so the funnel's "table narrows,
        never broadens" rule still governs the swept skills while file_read /
        file_search remain available in every subset. ``allow=None`` exposes
        the full registry (defaults already registered there).
        """
        resolved = self._resolve_allow_set(allow)
        if resolved is not None:
            resolved = resolved | self._default_ids
        return registry_to_tools(
            self.registry,
            allow=resolved,
            exclude=self._base_exclude,
        )

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
    # The two on-disk paths (manifest, repo_path) need a metadata-only
    # pre-pass of SkillLoader so the registry exposes index_requirements
    # to the loader/compiler. The ``contexts`` mode skips that — the
    # caller already did the equivalent.
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
        # repo_path mode: inline build via build_skill_contexts.
        loader.load_all(skills_dir, contexts={}, registry=registry)
        contexts = _build_contexts(opts, table=table)
        SkillRegistry.reset()
        registry = SkillRegistry()

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
