# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Classic Agentless localization over CodeNib manifest views."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from codenib.clients._manifest import ManifestResolver, select_checkout_manifest
from codenib.clients.agentless_protocol import (
    AgentlessProtocol,
    build_agentless_protocol,
)
from codenib.eval.baseline import BaselineLocation, BaselineRunResult
from codenib.eval.benchmarks.agentless import (
    AgentlessLocation,
    parse_agentless_files,
    parse_agentless_locations,
)
from codenib.integrations._repository import RepositoryEntity
from codenib.integrations.agentless import AgentlessRepositoryProvider
from codenib.types import NODE_TYPE_CLASS, NODE_TYPE_FUNCTION, NODE_TYPE_METHOD


@dataclass(frozen=True, slots=True)
class AgentlessExecution:
    """Outputs and accounting from the three localization stages."""

    file_output: str = ""
    symbol_output: str = ""
    line_output: str = ""
    usage: Optional[Mapping[str, Any]] = None
    trajectory: Sequence[Mapping[str, Any]] = ()

    @property
    def final_output(self) -> str:
        return self.line_output or self.symbol_output or self.file_output


AgentlessPolicyRunner = Callable[
    [str, str, AgentlessRepositoryProvider, Mapping[str, Any]],
    AgentlessExecution | str,
]
AgentlessProviderFactory = Callable[[Path], AgentlessRepositoryProvider]
AgentlessManifestResolver = ManifestResolver


def _entity_location(
    entity: RepositoryEntity,
    parsed: AgentlessLocation,
) -> BaselineLocation:
    qualified = entity.qualified_name or entity.simple_name
    if entity.kind in {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}:
        name = qualified.split("(", 1)[0].strip()
        name = f"{name}()" if name else ""
        location_type = "method" if entity.kind == NODE_TYPE_METHOD else "function"
    elif entity.kind == NODE_TYPE_CLASS:
        name = qualified
        location_type = "class"
    else:
        name = qualified
        location_type = parsed.kind if parsed.kind != "line" else "symbol"
    return BaselineLocation(
        name=name,
        type=location_type,
        file_path=entity.file_path,
        line_start=(
            entity.start_line + 1
            if entity.start_line is not None
            else parsed.line_start
        ),
        line_end=(
            entity.end_line + 1 if entity.end_line is not None else parsed.line_end
        ),
    )


def agentless_locations_to_baseline(
    locations: Sequence[AgentlessLocation],
    provider: AgentlessRepositoryProvider,
) -> tuple[BaselineLocation, ...]:
    """Resolve parsed Agentless records against CodeNib source identities."""

    output: list[BaselineLocation] = []
    seen: set[tuple[object, ...]] = set()
    for parsed in locations:
        files = provider.resolve_files([parsed.file_path], limit=1)
        if not files:
            continue
        file_path = files[0]
        entity: RepositoryEntity | None = None
        if parsed.kind == "line" and parsed.line_start is not None:
            entity = provider.repository.containing_entity(
                file_path,
                max(0, parsed.line_start - 1),
            )
        elif parsed.name:
            entity = provider.resolve_entity(file_path, parsed.kind, parsed.name)

        if entity is not None:
            location = _entity_location(entity, parsed)
        else:
            name = parsed.name.split("(", 1)[0].strip()
            if parsed.kind in {"function", "method"} and name:
                name += "()"
            location = BaselineLocation(
                name=name,
                type=parsed.kind,
                file_path=file_path,
                line_start=parsed.line_start,
                line_end=parsed.line_end,
            )
        key = (
            location.file_path,
            location.name,
            location.type,
            location.line_start,
            location.line_end,
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(location)
    return tuple(output)


class AgentlessAgent:
    """Run the pinned Agentless v1.5.0 localization policy on CodeNib views."""

    def __init__(
        self,
        *,
        model: str,
        manifest_path: str | Path | None = None,
        manifest_resolver: AgentlessManifestResolver | None = None,
        top_n_files: int = 3,
        context_window: int = 10,
        max_retries: int = 5,
        max_completion_tokens: int = 300,
        base_url: str | None = None,
        api_key: str | None = None,
        policy_runner: AgentlessPolicyRunner | None = None,
        provider_factory: AgentlessProviderFactory | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not str(model).strip():
            raise ValueError("Agentless model must not be empty")
        if manifest_path is not None and manifest_resolver is not None:
            raise ValueError(
                "Specify either manifest_path or manifest_resolver, not both"
            )
        if top_n_files < 1 or top_n_files > 5:
            raise ValueError("top_n_files must be between 1 and 5")
        if context_window < 0:
            raise ValueError("context_window must be non-negative")
        if max_retries < 1:
            raise ValueError("max_retries must be at least 1")
        if max_completion_tokens < 1:
            raise ValueError("max_completion_tokens must be positive")

        self.model = str(model)
        self.top_n_files = int(top_n_files)
        self.context_window = int(context_window)
        self.max_retries = int(max_retries)
        self.max_completion_tokens = int(max_completion_tokens)
        self.base_url = base_url
        self.api_key = api_key
        self._manifest_path = (
            Path(manifest_path).expanduser().resolve()
            if manifest_path is not None
            else None
        )
        self._manifest_resolver = manifest_resolver
        self._policy_runner = policy_runner or self._run_pinned_policy
        self._provider_factory = (
            provider_factory or AgentlessRepositoryProvider.from_manifest
        )
        self._client_factory = client_factory

    async def locate_code(
        self,
        query_text: str,
        repo_path: str,
        context: Optional[Mapping[str, Any]] = None,
    ) -> BaselineRunResult:
        """Localize one issue through the shared baseline interface."""

        repo = Path(repo_path).expanduser().resolve()
        if not repo.is_dir():
            return BaselineRunResult(
                success=False,
                repo_path=str(repo),
                error_message=f"Repository path does not exist: {repo}",
            )
        problem = str(query_text or "").strip()
        runtime_context = dict(context or {})
        if not problem:
            problem = str(runtime_context.get("issue_body") or "").strip()
        if not problem:
            return BaselineRunResult(
                success=False,
                repo_path=str(repo),
                error_message="Agentless requires a non-empty problem statement",
            )

        try:
            manifest_path = select_checkout_manifest(
                repo,
                integration_name="Agentless",
                manifest_path=self._manifest_path,
                manifest_resolver=self._manifest_resolver,
            )
            provider = self._provider_factory(manifest_path)
            raw_execution = await asyncio.to_thread(
                self._policy_runner,
                problem,
                str(repo),
                provider,
                runtime_context,
            )
            execution = (
                raw_execution
                if isinstance(raw_execution, AgentlessExecution)
                else AgentlessExecution(line_output=str(raw_execution))
            )
            parsed = parse_agentless_locations(
                execution.final_output,
                valid_files=provider.files,
                root_name=provider.project_root_name,
            )
            if not parsed and execution.symbol_output:
                parsed = parse_agentless_locations(
                    execution.symbol_output,
                    valid_files=provider.files,
                    root_name=provider.project_root_name,
                )
            if not parsed and execution.file_output:
                parsed = tuple(
                    AgentlessLocation(file_path=file_path)
                    for file_path in parse_agentless_files(
                        execution.file_output,
                        valid_files=provider.files,
                        root_name=provider.project_root_name,
                        limit=self.top_n_files,
                    )
                )
            return BaselineRunResult(
                success=True,
                repo_path=str(repo),
                locations=agentless_locations_to_baseline(parsed, provider),
                usage=dict(execution.usage or {}) or None,
                raw_output={
                    "file": execution.file_output,
                    "symbol": execution.symbol_output,
                    "line": execution.line_output,
                    "trajectory": list(execution.trajectory),
                },
            )
        except Exception as exc:  # noqa: BLE001 - external baseline boundary
            return BaselineRunResult(
                success=False,
                repo_path=str(repo),
                error_message=f"Agentless localization failed: {exc}",
            )

    def _openai_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        from openai import OpenAI

        kwargs: dict[str, Any] = {}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        if self.api_key:
            kwargs["api_key"] = self.api_key
        return OpenAI(**kwargs)

    def _run_pinned_policy(
        self,
        problem_statement: str,
        repo_path: str,
        provider: AgentlessRepositoryProvider,
        context: Mapping[str, Any],
    ) -> AgentlessExecution:
        del repo_path, context
        return run_agentless_policy(
            protocol=build_agentless_protocol(problem_statement),
            provider=provider,
            model=self.model,
            client=self._openai_client(),
            top_n_files=self.top_n_files,
            context_window=self.context_window,
            max_retries=self.max_retries,
            max_completion_tokens=self.max_completion_tokens,
        )


def run_agentless_policy(
    *,
    protocol: AgentlessProtocol,
    provider: AgentlessRepositoryProvider,
    model: str,
    client: Any,
    top_n_files: int = 3,
    context_window: int = 10,
    max_retries: int = 5,
    max_completion_tokens: int = 300,
) -> AgentlessExecution:
    """Run file, symbol, and line localization with classic retry semantics."""

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
    trajectory: list[dict[str, Any]] = []
    started = time.perf_counter()

    def complete(stage: str, prompt: str, temperature: float) -> str:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=max_completion_tokens,
            temperature=temperature,
        )
        prompt_tokens, completion_tokens, cached_tokens = _response_usage(response)
        usage["prompt_tokens"] += prompt_tokens
        usage["completion_tokens"] += completion_tokens
        usage["cached_tokens"] += cached_tokens
        content = _response_text(response)
        trajectory.append(
            {
                "stage": stage,
                "temperature": temperature,
                "prompt": prompt,
                "response": content,
            }
        )
        return content

    file_output = complete(
        "file",
        protocol.file_prompt(provider.project_structure()),
        0.0,
    )
    files = parse_agentless_files(
        file_output,
        valid_files=provider.files,
        root_name=provider.project_root_name,
        limit=top_n_files,
    )
    if not files:
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        usage["elapsed_seconds"] = time.perf_counter() - started
        return AgentlessExecution(
            file_output=file_output,
            usage=usage,
            trajectory=tuple(trajectory),
        )

    symbol_prompt = protocol.symbol_prompt(provider.skeleton_context(files))
    symbol_output = ""
    coarse: tuple[AgentlessLocation, ...] = ()
    coarse_is_valid = False
    for attempt in range(max_retries):
        symbol_output = complete(
            "symbol",
            symbol_prompt,
            0.0 if attempt == 0 else 1.0,
        )
        coarse = parse_agentless_locations(
            symbol_output,
            valid_files=files,
            root_name=provider.project_root_name,
        )
        coarse_is_valid = _contains_valid_location(coarse, provider)
        if coarse_is_valid:
            break

    if not coarse_is_valid:
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        usage["elapsed_seconds"] = time.perf_counter() - started
        usage["model_calls"] = len(trajectory)
        return AgentlessExecution(
            file_output=file_output,
            symbol_output=symbol_output,
            usage=usage,
            trajectory=tuple(trajectory),
        )

    line_prompt = protocol.line_prompt(
        provider.line_context(
            files,
            coarse,
            context_window=context_window,
        )
    )
    line_output = ""
    for attempt in range(max_retries):
        line_output = complete(
            "line",
            line_prompt,
            0.0 if attempt == 0 else 1.0,
        )
        fine = parse_agentless_locations(
            line_output,
            valid_files=files,
            root_name=provider.project_root_name,
        )
        if _contains_valid_location(fine, provider):
            break

    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    usage["elapsed_seconds"] = time.perf_counter() - started
    usage["model_calls"] = len(trajectory)
    return AgentlessExecution(
        file_output=file_output,
        symbol_output=symbol_output,
        line_output=line_output,
        usage=usage,
        trajectory=tuple(trajectory),
    )


def _contains_valid_location(
    locations: Sequence[AgentlessLocation],
    provider: AgentlessRepositoryProvider,
) -> bool:
    for location in locations:
        files = provider.resolve_files([location.file_path], limit=1)
        if not files:
            continue
        if location.kind == "file":
            return True
        if location.kind == "line" and location.line_start is not None:
            line_count = len(provider.repository.source_lines(files[0]))
            if 1 <= location.line_start <= line_count:
                return True
        elif provider.resolve_entity(files[0], location.kind, location.name):
            return True
    return False


def _response_text(response: Any) -> str:
    content = getattr(response.choices[0].message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        return "\n".join(
            str(
                item.get("text")
                if isinstance(item, Mapping)
                else getattr(item, "text", "")
            )
            for item in content
            if (
                item.get("text")
                if isinstance(item, Mapping)
                else getattr(item, "text", "")
            )
        )
    return ""


def _response_usage(response: Any) -> tuple[int, int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0, 0
    prompt = int(
        getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", 0) or 0
    )
    completion = int(
        getattr(usage, "completion_tokens", None)
        or getattr(usage, "output_tokens", 0)
        or 0
    )
    details = getattr(usage, "prompt_tokens_details", None)
    cached = int(getattr(details, "cached_tokens", 0) or 0)
    return prompt, completion, cached


__all__ = [
    "AgentlessAgent",
    "AgentlessExecution",
    "AgentlessManifestResolver",
    "AgentlessPolicyRunner",
    "AgentlessProviderFactory",
    "agentless_locations_to_baseline",
    "run_agentless_policy",
]
