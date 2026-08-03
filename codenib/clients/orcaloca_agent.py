# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""OrcaLoca SearchAgent adapter for the generic localization contract.

The supported boundary starts at SearchAgent. The default runner intentionally
uses an empty TraceAnalysisOutput and does not execute OrcaLoca's upstream
trace-analysis stage.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from codenib.clients._manifest import (
    ManifestResolver,
    resolve_checkout_manifest,
    select_checkout_manifest,
)
from codenib.eval.baseline import BaselineLocation, BaselineRunResult
from codenib.eval.benchmarks.orcaloca import OrcaLocaLocation, parse_orcaloca_locations

OrcaLocaPayload = str | Mapping[str, Any] | Sequence[Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class OrcaLocaExecution:
    """Raw output and optional accounting returned by an OrcaLoca run."""

    raw_output: OrcaLocaPayload
    usage: Optional[Mapping[str, Any]] = None
    tool_calls: Optional[int] = None


OrcaLocaSearchRunner = Callable[
    [str, str, Path],
    OrcaLocaExecution | OrcaLocaPayload,
]
OrcaLocaManifestResolver = ManifestResolver


def resolve_orcaloca_manifest(repo_path: str | Path) -> Path:
    """Resolve the canonical or legacy CodeNib manifest for one checkout."""

    return resolve_checkout_manifest(repo_path)


def orcaloca_locations_to_baseline(
    locations: Sequence[OrcaLocaLocation],
) -> tuple[BaselineLocation, ...]:
    """Translate OrcaLoca final locations to the shared benchmark schema."""

    output: list[BaselineLocation] = []
    seen: set[tuple[str, str, str]] = set()
    for location in locations:
        if location.method_name:
            method_name = _canonical_callable_name(location.method_name)
            name = (
                f"{location.class_name}.{method_name}"
                if location.class_name
                else method_name
            )
            location_type = "method" if location.class_name else "function"
        elif location.class_name:
            name = location.class_name
            location_type = "class"
        else:
            name = ""
            location_type = "file"

        key = (location.file_path, name, location_type)
        if not location.file_path or key in seen:
            continue
        seen.add(key)
        output.append(
            BaselineLocation(
                name=name,
                type=location_type,
                file_path=location.file_path,
            )
        )
    return tuple(output)


class OrcaLocaAgent:
    """Run OrcaLoca SearchAgent over CodeNib views through ``locate_code``.

    The class imports OrcaLoca, LlamaIndex, and tiktoken only when the default
    search runner executes. Tests and alternative runtimes can inject a
    dependency-free ``search_runner`` with the same raw-output contract.

    Trace-analysis generation is outside this adapter. The default runner
    supplies an empty ``TraceAnalysisOutput`` so repository-provider behavior
    can be evaluated without changing an upstream issue-analysis input.
    """

    def __init__(
        self,
        *,
        model: str,
        manifest_path: str | Path | None = None,
        manifest_resolver: OrcaLocaManifestResolver | None = None,
        max_iterations: int = 10,
        temperature: float = 1.0,
        config_path: str | Path | None = None,
        context_window: int | None = None,
        tokenizer_encoding: str | None = None,
        base_url: str | None = None,
        search_runner: OrcaLocaSearchRunner | None = None,
    ) -> None:
        if not str(model).strip():
            raise ValueError("OrcaLoca model must not be empty")
        if manifest_path is not None and manifest_resolver is not None:
            raise ValueError(
                "Specify either manifest_path or manifest_resolver, not both"
            )
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if context_window is not None and context_window < 1:
            raise ValueError("context_window must be positive")

        self.model = str(model)
        self.max_iterations = int(max_iterations)
        self.temperature = float(temperature)
        self.config_path = (
            Path(config_path).expanduser().resolve()
            if config_path is not None
            else None
        )
        self.context_window = context_window
        self.tokenizer_encoding = tokenizer_encoding
        self.base_url = str(base_url).strip() if base_url else None
        self._manifest_path = (
            Path(manifest_path).expanduser().resolve()
            if manifest_path is not None
            else None
        )
        self._manifest_resolver = manifest_resolver
        self._search_runner = search_runner or self._run_upstream

    async def locate_code(
        self,
        query_text: str,
        repo_path: str,
        context: Optional[Mapping[str, Any]] = None,
    ) -> BaselineRunResult:
        """Localize one task using the shared external-agent interface."""

        repo = Path(repo_path).expanduser().resolve()
        if not repo.is_dir():
            return BaselineRunResult(
                success=False,
                repo_path=str(repo),
                error_message=f"Repository path does not exist: {repo}",
            )

        problem_statement = str(query_text or "").strip()
        if not problem_statement and context:
            problem_statement = str(context.get("issue_body") or "").strip()
        if not problem_statement:
            return BaselineRunResult(
                success=False,
                repo_path=str(repo),
                error_message="OrcaLoca requires a non-empty problem statement",
            )

        try:
            manifest_path = self._resolve_manifest(repo)
            raw_execution = await asyncio.to_thread(
                self._search_runner,
                problem_statement,
                str(repo),
                manifest_path,
            )
            execution = (
                raw_execution
                if isinstance(raw_execution, OrcaLocaExecution)
                else OrcaLocaExecution(raw_output=raw_execution)
            )
            locations = orcaloca_locations_to_baseline(
                parse_orcaloca_locations(execution.raw_output)
            )
            usage = dict(execution.usage or {})
            if execution.tool_calls is not None:
                usage.setdefault("tool_calls", execution.tool_calls)
            return BaselineRunResult(
                success=True,
                repo_path=str(repo),
                locations=locations,
                usage=usage or None,
                raw_output=execution.raw_output,
            )
        except Exception as exc:  # noqa: BLE001 - external baseline boundary
            return BaselineRunResult(
                success=False,
                repo_path=str(repo),
                error_message=f"OrcaLoca localization failed: {exc}",
            )

    def _resolve_manifest(self, repo: Path) -> Path:
        return select_checkout_manifest(
            repo,
            integration_name="OrcaLoca",
            manifest_path=self._manifest_path,
            manifest_resolver=self._manifest_resolver,
        )

    def _run_upstream(
        self,
        problem_statement: str,
        repo_path: str,
        manifest_path: Path,
    ) -> OrcaLocaExecution:
        self._configure_model_metadata()

        from llama_index.llms.openai import OpenAI
        from Orcar.search_agent import SearchAgent
        from Orcar.types import SearchInput, TraceAnalysisOutput

        from codenib.integrations.orcaloca import make_orcaloca_search_manager_factory

        # Trace analysis is a distinct upstream stage. Keep its output empty so
        # this adapter changes only the repository provider used by SearchAgent.
        search_input = SearchInput(
            problem_statement=problem_statement,
            trace_analysis_output=TraceAnalysisOutput(),
        )
        config_path = self.config_path or Path(inspect.getfile(SearchAgent)).with_name(
            "search.cfg"
        )
        llm_options = {"api_base": self.base_url} if self.base_url else {}
        llm = OpenAI(
            model=self.model,
            max_tokens=None,
            temperature=self.temperature,
            **llm_options,
        )
        agent = SearchAgent(
            llm=llm,
            search_input=search_input,
            repo_path=repo_path,
            max_iterations=self.max_iterations,
            config_path=str(config_path),
            search_manager_factory=make_orcaloca_search_manager_factory(manifest_path),
        )
        response = agent.chat(search_input.get_content())
        manager = getattr(agent, "_search_manager", None)
        history = getattr(manager, "history", None)
        tool_calls = len(history) if isinstance(history, Sequence) else None
        return OrcaLocaExecution(
            raw_output=getattr(response, "response", response),
            tool_calls=tool_calls,
        )

    def _configure_model_metadata(self) -> None:
        if self.tokenizer_encoding:
            import tiktoken.model

            tiktoken.model.MODEL_TO_ENCODING[self.model] = self.tokenizer_encoding
        if self.context_window is not None:
            import llama_index.llms.openai.utils as openai_utils

            for name in ("CHAT_MODELS", "ALL_AVAILABLE_MODELS"):
                models = getattr(openai_utils, name, None)
                if isinstance(models, dict):
                    models[self.model] = self.context_window


def _canonical_callable_name(value: str) -> str:
    name = str(value).strip().split("(", 1)[0].strip()
    return f"{name}()" if name else ""


__all__ = [
    "OrcaLocaAgent",
    "OrcaLocaExecution",
    "OrcaLocaManifestResolver",
    "OrcaLocaPayload",
    "OrcaLocaSearchRunner",
    "orcaloca_locations_to_baseline",
    "resolve_orcaloca_manifest",
]
