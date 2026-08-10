# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""LocAgent policy adapter for CodeNib's localization benchmark contract."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from codenib.clients._manifest import ManifestResolver, select_checkout_manifest
from codenib.clients.locagent_protocol import LocAgentProtocol, build_locagent_protocol
from codenib.eval.baseline import BaselineLocation, BaselineRunResult
from codenib.eval.benchmarks.locagent import LocAgentLocation, parse_locagent_locations
from codenib.integrations._repository import RepositoryAdapter, RepositoryEntity
from codenib.integrations.locagent import LocAgentToolProvider
from codenib.source_fingerprint import lexical_repository_path
from codenib.types import (
    NODE_TYPE_CLASS,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NODE_TYPE_SYMBOL,
)


@dataclass(frozen=True, slots=True)
class LocAgentExecution:
    """Final answer and accounting returned by one LocAgent policy run."""

    final_output: str
    usage: Optional[Mapping[str, Any]] = None
    tool_calls: Optional[int] = None
    iterations: Optional[int] = None
    tool_trace: Sequence[Mapping[str, Any]] = ()
    trajectory: Sequence[Mapping[str, Any]] = ()


LocAgentPolicyRunner = Callable[
    [str, str, LocAgentToolProvider, Mapping[str, Any]],
    LocAgentExecution | str,
]
LocAgentProviderFactory = Callable[[Path], LocAgentToolProvider]
LocAgentManifestResolver = ManifestResolver
LocAgentDispatch = Callable[[str, Mapping[str, Any]], str]


def _clean_symbol(value: str) -> str:
    symbol = str(value or "").strip().strip("`'\" ")
    symbol = symbol.replace("::", ".")
    return _strip_call_signature(symbol)


def _strip_call_signature(value: str) -> str:
    """Remove a display-only call signature from a symbol name."""

    return value.split("(", 1)[0].strip()


def _canonical_callable_name(value: str) -> str:
    name = _clean_symbol(value)
    return f"{name}()" if name else ""


def _location_entity(
    location: LocAgentLocation,
    repository: RepositoryAdapter,
) -> tuple[str, RepositoryEntity | None]:
    files = repository.find_files(location.file_path)
    if len(files) != 1:
        return location.file_path, None
    file_path = files[0]

    candidates: list[RepositoryEntity] = []
    if location.function_name:
        function_name = _clean_symbol(location.function_name)
        class_name = _clean_symbol(location.class_name)
        queries: list[str] = []
        if class_name and not function_name.startswith(class_name + "."):
            queries.append(f"{class_name}.{function_name}")
        queries.append(function_name)
        for query in queries:
            candidates = repository.find_entities(query, file_path=file_path)
            if candidates:
                break
    elif location.class_name:
        candidates = repository.find_entities(
            _clean_symbol(location.class_name),
            file_path=file_path,
            kinds={NODE_TYPE_CLASS},
        )

    line_entity = None
    if location.line_start is not None:
        line_entity = repository.containing_entity(
            file_path,
            max(0, location.line_start - 1),
        )
    if len(candidates) == 1:
        return file_path, candidates[0]
    if line_entity is not None and (
        not candidates
        or any(item.vertex_id == line_entity.vertex_id for item in candidates)
    ):
        return file_path, line_entity
    return file_path, None


def _entity_location(
    entity: RepositoryEntity,
    parsed: LocAgentLocation,
) -> BaselineLocation:
    qualified_name = entity.qualified_name or entity.simple_name
    if parsed.function_name or entity.kind in {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}:
        name = _canonical_callable_name(qualified_name)
        location_type = (
            "method"
            if entity.kind == NODE_TYPE_METHOD
            or bool(entity.class_name)
            or bool(parsed.class_name)
            else "function"
        )
    elif parsed.class_name or entity.kind == NODE_TYPE_CLASS:
        name = qualified_name
        location_type = "class"
    else:
        name = qualified_name
        location_type = "symbol" if entity.kind == NODE_TYPE_SYMBOL else entity.kind
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


def _fallback_location(
    file_path: str,
    parsed: LocAgentLocation,
) -> BaselineLocation:
    if parsed.function_name:
        function_name = _clean_symbol(parsed.function_name)
        class_name = _clean_symbol(parsed.class_name)
        if class_name and not function_name.startswith(class_name + "."):
            function_name = f"{class_name}.{function_name}"
        return BaselineLocation(
            name=_canonical_callable_name(function_name),
            type="method" if class_name or "." in function_name else "function",
            file_path=file_path,
            line_start=parsed.line_start,
            line_end=parsed.line_end,
        )
    if parsed.class_name:
        return BaselineLocation(
            name=_clean_symbol(parsed.class_name),
            type="class",
            file_path=file_path,
            line_start=parsed.line_start,
            line_end=parsed.line_end,
        )
    return BaselineLocation(
        name="",
        type="file",
        file_path=file_path,
        line_start=parsed.line_start,
        line_end=parsed.line_end,
    )


def locagent_locations_to_baseline(
    locations: Sequence[LocAgentLocation],
    repository: RepositoryAdapter,
) -> tuple[BaselineLocation, ...]:
    """Resolve LocAgent records against CodeNib's language-neutral graph."""

    output: list[BaselineLocation] = []
    seen: set[tuple[object, ...]] = set()
    for parsed in locations:
        file_path, entity = _location_entity(parsed, repository)
        location = (
            _entity_location(entity, parsed)
            if entity is not None
            else _fallback_location(file_path, parsed)
        )
        key = (
            location.file_path,
            location.name,
            location.type,
            location.line_start,
            location.line_end,
        )
        if not location.file_path or key in seen:
            continue
        seen.add(key)
        output.append(location)
    return tuple(output)


def load_locagent_protocol(
    checkout: str | Path | None = None,
    *,
    problem_statement: str,
    package_name: str,
    python_executable: str | Path | None = None,
) -> LocAgentProtocol:
    """Build the vendored protocol; legacy path arguments are ignored."""

    del checkout, python_executable
    return build_locagent_protocol(
        problem_statement=problem_statement,
        package_name=package_name,
    )


class LocAgentAgent:
    """Run CodeNib's pinned LocAgent policy over manifest-backed tools."""

    def __init__(
        self,
        *,
        model: str,
        locagent_checkout: str | Path | None = None,
        locagent_python: str | Path | None = None,
        manifest_path: str | Path | None = None,
        manifest_resolver: LocAgentManifestResolver | None = None,
        max_iterations: int = 10,
        max_completion_tokens: int = 4096,
        reasoning_effort: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        policy_runner: LocAgentPolicyRunner | None = None,
        provider_factory: LocAgentProviderFactory | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not str(model).strip():
            raise ValueError("LocAgent model must not be empty")
        if manifest_path is not None and manifest_resolver is not None:
            raise ValueError(
                "Specify either manifest_path or manifest_resolver, not both"
            )
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if max_completion_tokens < 1:
            raise ValueError("max_completion_tokens must be positive")

        self.model = str(model)
        # Retain these arguments for callers migrating from the checkout-backed
        # adapter. The pinned production protocol no longer reads either path.
        del locagent_checkout, locagent_python
        self.max_iterations = int(max_iterations)
        self.max_completion_tokens = int(max_completion_tokens)
        self.reasoning_effort = reasoning_effort
        self.base_url = base_url
        self.api_key = api_key
        self._manifest_path = (
            Path(manifest_path).expanduser().resolve()
            if manifest_path is not None
            else None
        )
        self._manifest_resolver = manifest_resolver
        self._policy_runner = policy_runner or self._run_pinned_policy
        self._provider_factory = provider_factory or LocAgentToolProvider.from_manifest
        self._client_factory = client_factory

    async def locate_code(
        self,
        query_text: str,
        repo_path: str,
        context: Optional[Mapping[str, Any]] = None,
    ) -> BaselineRunResult:
        """Localize one task through the shared external-agent interface."""

        repo = lexical_repository_path(repo_path)
        if not repo.is_dir():
            return BaselineRunResult(
                success=False,
                repo_path=str(repo),
                error_message=f"Repository path does not exist: {repo}",
            )
        problem_statement = str(query_text or "").strip()
        runtime_context = dict(context or {})
        if not problem_statement:
            problem_statement = str(runtime_context.get("issue_body") or "").strip()
        if not problem_statement:
            return BaselineRunResult(
                success=False,
                repo_path=str(repo),
                error_message="LocAgent requires a non-empty problem statement",
            )

        try:
            manifest_path = select_checkout_manifest(
                repo,
                integration_name="LocAgent",
                manifest_path=self._manifest_path,
                manifest_resolver=self._manifest_resolver,
            )
            provider = self._provider_factory(manifest_path)
            raw_execution = await asyncio.to_thread(
                self._policy_runner,
                problem_statement,
                str(repo),
                provider,
                runtime_context,
            )
            execution = (
                raw_execution
                if isinstance(raw_execution, LocAgentExecution)
                else LocAgentExecution(final_output=str(raw_execution))
            )
            parsed = parse_locagent_locations(
                execution.final_output,
                valid_files=provider.repository.files,
            )
            locations = locagent_locations_to_baseline(
                parsed,
                provider.repository,
            )
            usage = dict(execution.usage or {})
            if execution.tool_calls is not None:
                usage.setdefault("tool_calls", execution.tool_calls)
            if execution.iterations is not None:
                usage.setdefault("iterations", execution.iterations)
            return BaselineRunResult(
                success=True,
                repo_path=str(repo),
                locations=locations,
                usage=usage or None,
                raw_output=execution.final_output,
            )
        except Exception as exc:  # noqa: BLE001 - external baseline boundary
            return BaselineRunResult(
                success=False,
                repo_path=str(repo),
                error_message=f"LocAgent localization failed: {exc}",
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
        provider: LocAgentToolProvider,
        context: Mapping[str, Any],
    ) -> LocAgentExecution:
        del repo_path
        package_name = str(
            context.get("package_name")
            or context.get("repo_name")
            or provider.repository.repo_root.name
        ).replace("\\", "/")
        package_name = package_name.rsplit("/", 1)[-1]
        protocol = build_locagent_protocol(
            problem_statement=problem_statement,
            package_name=package_name,
        )
        return run_locagent_policy(
            protocol=protocol,
            model=self.model,
            dispatch=provider.dispatch,
            client=self._openai_client(),
            max_iterations=self.max_iterations,
            max_completion_tokens=self.max_completion_tokens,
            reasoning_effort=self.reasoning_effort,
        )


def run_locagent_policy(
    *,
    protocol: LocAgentProtocol,
    model: str,
    dispatch: LocAgentDispatch,
    client: Any,
    max_iterations: int = 10,
    max_completion_tokens: int = 4096,
    reasoning_effort: str | None = None,
) -> LocAgentExecution:
    """Run the pinned LocAgent reason/action loop over an arbitrary provider."""

    if max_iterations < 1:
        raise ValueError("max_iterations must be at least 1")
    if max_completion_tokens < 1:
        raise ValueError("max_completion_tokens must be positive")

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": protocol.system_prompt},
        {"role": "user", "content": protocol.instruction},
    ]
    trajectory: list[dict[str, Any]] = list(messages)
    tools = json.loads(json.dumps(protocol.tools))
    prompt_tokens = completion_tokens = cached_tokens = 0
    final_output = ""
    tool_trace: list[dict[str, Any]] = []
    model_turns = 0
    started = time.perf_counter()

    for iteration in range(1, max_iterations + 2):
        model_turns = iteration
        if iteration == max_iterations + 1:
            warning = {
                "role": "user",
                "content": (
                    "The maximum number of iterations has been reached. "
                    "Return the best locations found so far and call finish now."
                ),
            }
            messages.append(warning)
            trajectory.append(warning)

        request: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "max_completion_tokens": max_completion_tokens,
        }
        if reasoning_effort is not None:
            request["reasoning_effort"] = reasoning_effort
        response = client.chat.completions.create(**request)
        prompt, completion, cached = _response_usage(response)
        prompt_tokens += prompt
        completion_tokens += completion
        cached_tokens += cached

        assistant = response.choices[0].message
        assistant_dict = _assistant_message_dict(assistant)
        messages.append(assistant_dict)
        trajectory.append(assistant_dict)
        assistant_content = _assistant_text(assistant)
        if assistant_content:
            final_output = assistant_content
        calls = list(getattr(assistant, "tool_calls", None) or ())
        if not calls:
            followup = {"role": "user", "content": protocol.followup}
            messages.append(followup)
            trajectory.append(followup)
            continue

        finished = False
        for call in calls:
            function = call.function
            name = str(function.name)
            try:
                arguments = json.loads(function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            if not isinstance(arguments, Mapping):
                arguments = {}
            if name == "finish":
                finish_output = next(
                    (str(value) for value in arguments.values() if value is not None),
                    "",
                )
                if not assistant_content and finish_output:
                    final_output = finish_output
                finished = True
                break

            tool_started = time.perf_counter()
            try:
                result = dispatch(name, arguments)
                error = None
            except Exception as exc:  # noqa: BLE001 - policy observation
                result = f"ERROR: {type(exc).__name__}: {exc}"
                error = result
            tool_trace.append(
                {
                    "iteration": iteration,
                    "name": name,
                    "arguments": dict(arguments),
                    "seconds": time.perf_counter() - tool_started,
                    "output_chars": len(result),
                    "error": error,
                }
            )
            tool_message = {
                "role": "tool",
                "tool_call_id": call.id,
                "name": name,
                "content": "OBSERVATION:\n" + result,
            }
            messages.append(tool_message)
            trajectory.append(tool_message)
        if finished:
            break

    elapsed = time.perf_counter() - started
    return LocAgentExecution(
        final_output=final_output,
        usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cached_tokens": cached_tokens,
            "elapsed_seconds": elapsed,
            "tool_seconds": sum(float(item["seconds"]) for item in tool_trace),
        },
        tool_calls=len(tool_trace),
        iterations=model_turns,
        tool_trace=tuple(tool_trace),
        trajectory=tuple(trajectory),
    )


def _assistant_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping):
                text = item.get("text")
            else:
                text = getattr(item, "text", None)
            if text:
                parts.append(str(text))
        return "\n".join(parts)
    return ""


def _assistant_message_dict(message: Any) -> dict[str, Any]:
    model_dump = getattr(message, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(exclude_none=True))
    output: dict[str, Any] = {
        "role": getattr(message, "role", "assistant"),
        "content": getattr(message, "content", None),
    }
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        output["tool_calls"] = tool_calls
    return output


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
    "LocAgentAgent",
    "LocAgentDispatch",
    "LocAgentExecution",
    "LocAgentManifestResolver",
    "LocAgentPolicyRunner",
    "LocAgentProtocol",
    "LocAgentProviderFactory",
    "load_locagent_protocol",
    "locagent_locations_to_baseline",
    "run_locagent_policy",
]
