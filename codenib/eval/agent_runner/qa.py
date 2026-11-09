# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Shared repository-QA profile over CodeNib's existing agent and native skills."""

from __future__ import annotations

import json
import re
from pathlib import Path

from codenib.agent.runtime.explorer import (
    RepositoryContextExplorer,
    repository_explorer_build_views,
)

_SKILLS = {
    "bm25": ("bm25_search",),
    "dense": ("embedding_search",),
    "hybrid": ("bm25_search", "embedding_search"),
    "graph": ("bm25_search", "find_callers", "find_callees"),
}
_PROMPT = """Answer the question about this repository. Use the available search,
graph, and source-reading tools to find the relevant code and explain its behavior.
Graph tools expose static references between code symbols.
"""


def repository_relative_paths(value, root):
    """Represent this checkout's paths relative to its root in messages/results."""
    paths = {str(Path(root).absolute()), str(Path(root).resolve())}
    prefixes = paths | {json.dumps(path)[1:-1] for path in paths}

    def relative(item):
        if isinstance(item, str):
            for prefix in sorted(prefixes, key=len, reverse=True):
                item = item.replace(prefix + "/", "")
                item = re.sub(
                    re.escape(prefix) + r"(?=$|[\s\"'`:;,()\[\]{}<>]|\.(?=$|\s))",
                    ".",
                    item,
                )
            return item
        if isinstance(item, dict):
            return {relative(key): relative(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return type(item)(relative(val) for val in item)
        return item

    return relative(value)


class _RepositoryChat:
    """Present checkout-relative observations at the native model-call boundary."""

    def __init__(self, chat, root):
        self.chat, self.root = chat, root

    def __getattr__(self, name):
        return getattr(self.chat, name)

    def _call_raw(self, messages, **kwargs):
        return self.chat._call_raw(
            repository_relative_paths(messages, self.root),
            **repository_relative_paths(kwargs, self.root),
        )


def _bound_source_tool(execute, root, argument):
    def bound(**kwargs):
        target = (root / str(kwargs.get(argument) or ".")).resolve()
        if not target.is_relative_to(root) or any(
            part in {".git", ".hg", ".svn"} for part in target.relative_to(root).parts
        ):
            raise ValueError("tool path is outside the repository source view")
        kwargs[argument] = str(target)
        return execute(**kwargs)

    return bound


def run_qa_agent(explorer, question, *, model=None, llm=None, max_steps=10):
    """Run AgentRunner with the selected repository tools and retrieval skills.

    Model messages and returned records use checkout-relative paths. The caller
    owns the explorer; the process-global skill registry makes execution serial.
    """
    from codenib.agent.runner import AgentRunner
    from codenib.agent.skills.loader import SkillLoader
    from codenib.agent.skills.registry import SkillRegistry
    from codenib.compiler.params import SessionContext
    from codenib.ops.expand import ExpandContext
    from codenib.ops.retrieve import RetrieveContext

    from .contexts import default_agent_skills_dir

    if max_steps <= 0 or not isinstance(question, str) or not question.strip():
        raise ValueError("question must be nonempty and max_steps positive")
    skills = _SKILLS[explorer.policy]
    views = repository_explorer_build_views(explorer.policy)
    context = explorer.context
    context.load_views(views)
    missing = [name for name in views if getattr(context, name, None) is None]
    if missing:
        raise ValueError(f"required CodeNib views failed to load: {missing}")
    contexts = {
        "retrieve": RetrieveContext(bm25=context.bm25, vector_store=context.vector),
        "expand": ExpandContext(code_graph=context.symbol_graph),
    }
    root = Path(context.manifest.repo_path).resolve()
    SkillRegistry.reset()
    registry = SkillRegistry()
    try:
        for skill_id in skills:
            registry.register(
                SkillLoader().load_skill(
                    str(default_agent_skills_dir() / skill_id),
                    contexts,
                )
            )
        runner = AgentRunner(
            model=model,
            llm=llm,
            registry=registry,
            allow_skills=set(skills),
            manifest=context.manifest,
            session_ctx=SessionContext(repo_path=str(root)),
            default_tool_ids={"read", "grep", "glob"},
            system_prompt=_PROMPT,
            max_tokens=4096,
            max_turns=max_steps,
            force_final_answer=True,
        )
        runner.llm = _RepositoryChat(runner.llm, root)
        for tool_id, argument in (
            ("read", "file_path"),
            ("grep", "path"),
            ("glob", "path"),
        ):
            tool = runner.tool_registry.get(tool_id)
            tool.executor_fn = _bound_source_tool(tool.executor_fn, root, argument)
        result = runner.run(question)
        return repository_relative_paths(
            {
                "answer": result.answer,
                "steps": result.total_turns,
                "latency": result.total_duration_ms / 1000,
                "history": result.messages,
                "usage": result.usage.to_dict() if result.usage is not None else None,
                "integration": {
                    "agent_class": "codenib.agent.runner.AgentRunner",
                    "policy": explorer.policy,
                    "skills": list(skills),
                    "repo": ".",
                    "base_commit": context.manifest.commit,
                    "views": list(views),
                },
            },
            root,
        )
    finally:
        SkillRegistry.reset()


def answer_repository_question(repo, question, *, model, policy="graph", max_steps=10):
    """Validate the existing repository index, then use the shared QA profile."""
    with RepositoryContextExplorer.from_repository(repo, policy=policy) as explorer:
        return run_qa_agent(explorer, question, model=model, max_steps=max_steps)
