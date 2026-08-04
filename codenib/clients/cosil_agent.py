# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CoSIL RQ1 localization over CodeNib manifest-backed repository views."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from codenib.clients._manifest import ManifestResolver, select_checkout_manifest
from codenib.clients.cosil_protocol import CoSILProtocol, build_cosil_protocol
from codenib.eval.baseline import BaselineLocation, BaselineRunResult
from codenib.eval.benchmarks.cosil import (
    CoSILLocation,
    parse_cosil_files,
    parse_cosil_locations,
)
from codenib.integrations.cosil import CoSILRepositoryProvider, get_cosil_tool_schemas

_FALSE_OBSERVATION = (
    "I have already checked this function/class and it is not related to the bug. "
    "Don't check the functions it calls."
)


@dataclass(frozen=True, slots=True)
class CoSILExecution:
    """File, reflection, function, and accounting outputs from one run."""

    file_output: str = ""
    reflection_output: str = ""
    location_output: str = ""
    candidate_files: Sequence[str] = ()
    usage: Optional[Mapping[str, Any]] = None
    tool_trace: Sequence[Mapping[str, Any]] = ()
    trajectory: Sequence[Mapping[str, Any]] = ()


CoSILPolicyRunner = Callable[
    [str, str, CoSILRepositoryProvider, Mapping[str, Any]],
    CoSILExecution | str,
]
CoSILProviderFactory = Callable[[Path], CoSILRepositoryProvider]
CoSILManifestResolver = ManifestResolver


def cosil_locations_to_baseline(
    locations: Sequence[CoSILLocation],
    provider: CoSILRepositoryProvider,
) -> tuple[BaselineLocation, ...]:
    """Resolve CoSIL XML identities against its manifest-bound AST view."""

    output: list[BaselineLocation] = []
    seen: set[tuple[object, ...]] = set()
    for parsed in locations:
        files = provider.resolve_files([parsed.file_path], limit=1)
        if not files:
            continue
        file_path = files[0]
        resolved = provider.resolve_location(file_path, parsed.kind, parsed.name)
        if resolved is None:
            location = BaselineLocation(
                name=(f"{parsed.name}()" if parsed.kind == "function" else parsed.name),
                type=parsed.kind,
                file_path=file_path,
            )
        else:
            kind, name, start, end = resolved
            location = BaselineLocation(
                name=f"{name}()" if kind in {"function", "method"} else name,
                type=kind,
                file_path=file_path,
                line_start=start,
                line_end=end,
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


class CoSILAgent:
    """Run CoSIL file reflection and function localization on CodeNib views."""

    def __init__(
        self,
        *,
        model: str,
        manifest_path: str | Path | None = None,
        manifest_resolver: CoSILManifestResolver | None = None,
        top_n_files: int = 5,
        file_max_retry: int = 10,
        max_tool_rounds: int = 10,
        prune_tool_results: bool = True,
        max_prune_rounds: int = 3,
        max_completion_tokens: int = 4096,
        base_url: str | None = None,
        api_key: str | None = None,
        policy_runner: CoSILPolicyRunner | None = None,
        provider_factory: CoSILProviderFactory | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not str(model).strip():
            raise ValueError("CoSIL model must not be empty")
        if manifest_path is not None and manifest_resolver is not None:
            raise ValueError(
                "Specify either manifest_path or manifest_resolver, not both"
            )
        if not 1 <= top_n_files <= 5:
            raise ValueError("top_n_files must be between 1 and 5")
        if file_max_retry < 2:
            raise ValueError("file_max_retry must be at least 2")
        if max_tool_rounds < 1 or max_prune_rounds < 1:
            raise ValueError("tool and prune round limits must be positive")
        if max_completion_tokens < 1:
            raise ValueError("max_completion_tokens must be positive")

        self.model = str(model)
        self.top_n_files = int(top_n_files)
        self.file_max_retry = int(file_max_retry)
        self.max_tool_rounds = int(max_tool_rounds)
        self.prune_tool_results = bool(prune_tool_results)
        self.max_prune_rounds = int(max_prune_rounds)
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
            provider_factory or CoSILRepositoryProvider.from_manifest
        )
        self._client_factory = client_factory

    async def locate_code(
        self,
        query_text: str,
        repo_path: str,
        context: Optional[Mapping[str, Any]] = None,
    ) -> BaselineRunResult:
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
                error_message="CoSIL requires a non-empty problem statement",
            )

        try:
            manifest_path = select_checkout_manifest(
                repo,
                integration_name="CoSIL",
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
                if isinstance(raw_execution, CoSILExecution)
                else CoSILExecution(location_output=str(raw_execution))
            )
            parsed = parse_cosil_locations(
                execution.location_output,
                valid_files=execution.candidate_files or provider.files,
                root_name=provider.project_root_name,
            )
            locations = cosil_locations_to_baseline(parsed, provider)
            if not locations:
                files = tuple(execution.candidate_files) or parse_cosil_files(
                    execution.reflection_output or execution.file_output,
                    valid_files=provider.files,
                    root_name=provider.project_root_name,
                    limit=self.top_n_files,
                )
                locations = tuple(
                    BaselineLocation(name="", type="file", file_path=file_path)
                    for file_path in files
                )
            return BaselineRunResult(
                success=True,
                repo_path=str(repo),
                locations=locations,
                usage=dict(execution.usage or {}) or None,
                raw_output={
                    "file": execution.file_output,
                    "reflection": execution.reflection_output,
                    "locations": execution.location_output,
                    "candidate_files": list(execution.candidate_files),
                    "tool_trace": list(execution.tool_trace),
                    "trajectory": list(execution.trajectory),
                },
            )
        except Exception as exc:  # noqa: BLE001 - external baseline boundary
            return BaselineRunResult(
                success=False,
                repo_path=str(repo),
                error_message=f"CoSIL localization failed: {exc}",
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
        provider: CoSILRepositoryProvider,
        context: Mapping[str, Any],
    ) -> CoSILExecution:
        del repo_path, context
        return run_cosil_policy(
            protocol=build_cosil_protocol(problem_statement),
            provider=provider,
            model=self.model,
            client=self._openai_client(),
            top_n_files=self.top_n_files,
            file_max_retry=self.file_max_retry,
            max_tool_rounds=self.max_tool_rounds,
            prune_tool_results=self.prune_tool_results,
            max_prune_rounds=self.max_prune_rounds,
            max_completion_tokens=self.max_completion_tokens,
        )


def run_cosil_policy(
    *,
    protocol: CoSILProtocol,
    provider: CoSILRepositoryProvider,
    model: str,
    client: Any,
    top_n_files: int = 5,
    file_max_retry: int = 10,
    max_tool_rounds: int = 10,
    prune_tool_results: bool = True,
    max_prune_rounds: int = 3,
    max_completion_tokens: int = 4096,
) -> CoSILExecution:
    """Run CoSIL's file reflection, tool loop, pruning, and XML summary."""

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
    trajectory: list[dict[str, Any]] = []
    tool_trace: list[dict[str, Any]] = []
    started = time.perf_counter()
    temperature_supported = True
    max_completion_tokens_supported = True
    tool_reasoning_disabled = False

    def complete(
        *,
        stage: str,
        messages: Sequence[Mapping[str, Any]],
        temperature: float = 0.0,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        nonlocal max_completion_tokens_supported, temperature_supported
        nonlocal tool_reasoning_disabled
        request: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
        }
        token_parameter = (
            "max_completion_tokens" if max_completion_tokens_supported else "max_tokens"
        )
        request[token_parameter] = max_completion_tokens
        if temperature_supported:
            request["temperature"] = temperature
        if tools is not None:
            request["tools"] = list(tools)
            if tool_reasoning_disabled:
                request["reasoning_effort"] = "none"
        if tool_choice is not None:
            request["tool_choice"] = tool_choice
        while True:
            try:
                response = client.chat.completions.create(**request)
                break
            except Exception as exc:
                if temperature_supported and _unsupported_temperature(exc):
                    temperature_supported = False
                    request.pop("temperature", None)
                    continue
                if max_completion_tokens_supported and (
                    _unsupported_max_completion_tokens(exc)
                ):
                    max_completion_tokens_supported = False
                    request.pop("max_completion_tokens", None)
                    request["max_tokens"] = max_completion_tokens
                    continue
                if (
                    tools is not None
                    and not tool_reasoning_disabled
                    and _requires_no_reasoning_with_tools(exc)
                ):
                    tool_reasoning_disabled = True
                    request["reasoning_effort"] = "none"
                    continue
                raise
        prompt_tokens, completion_tokens, cached_tokens = _response_usage(response)
        usage["prompt_tokens"] += prompt_tokens
        usage["completion_tokens"] += completion_tokens
        usage["cached_tokens"] += cached_tokens
        message = _assistant_message(response.choices[0].message)
        content = str(message.get("content") or "")
        trajectory.append(
            {
                "stage": stage,
                "temperature": temperature if temperature_supported else None,
                "reasoning_effort": request.get("reasoning_effort"),
                "response": content,
                "tool_calls": message.get("tool_calls", []),
            }
        )
        return content, message

    structure = provider.project_structure()
    file_output, _ = complete(
        stage="file",
        messages=protocol.file_messages(
            structure=structure,
            max_retry=file_max_retry,
        ),
    )
    files = parse_cosil_files(
        file_output,
        valid_files=provider.files,
        root_name=provider.project_root_name,
        limit=top_n_files,
    )
    if not files:
        corrected, _ = complete(
            stage="file_format_correction",
            messages=[
                {"role": "user", "content": protocol.correction_prompt(file_output)}
            ],
        )
        files = parse_cosil_files(
            corrected,
            valid_files=provider.files,
            root_name=provider.project_root_name,
            limit=top_n_files,
        )
    if not files:
        return _finish_execution(
            started=started,
            usage=usage,
            trajectory=trajectory,
            tool_trace=tool_trace,
            file_output=file_output,
        )

    reflection_output, _ = complete(
        stage="file_reflection",
        messages=[
            {
                "role": "user",
                "content": protocol.reflection_prompt(
                    structure=structure,
                    pre_files=list(files),
                    import_content=provider.import_context(files),
                ),
            }
        ],
    )
    reflected = parse_cosil_files(
        reflection_output,
        valid_files=provider.files,
        root_name=provider.project_root_name,
        limit=top_n_files,
    )
    candidate_files = reflected or files
    candidate_structure = provider.candidate_structure(candidate_files)
    messages: list[dict[str, Any]] = protocol.location_messages(
        candidate_structure=candidate_structure,
        max_rounds=max_tool_rounds,
    )
    tools = get_cosil_tool_schemas()
    collected_code: list[tuple[str, Mapping[str, Any], str]] = []

    for round_index in range(max_tool_rounds):
        _content, assistant = complete(
            stage="location_tool",
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        messages.append(assistant)
        calls = list(assistant.get("tool_calls") or ())
        if not calls:
            break
        should_exit = False
        for call in calls:
            function = dict(call.get("function") or {})
            name = str(function.get("name") or "")
            arguments = _tool_arguments(function.get("arguments"))
            tool_started = time.perf_counter()
            try:
                result = provider.dispatch(name, arguments)
                error = None
            except Exception as exc:  # noqa: BLE001 - policy observation
                result = f"Tool call failed: {exc}"
                error = result
            kept = name != "exit"
            trace_entry: dict[str, Any] = {
                "phase": "location",
                "round": round_index + 1,
                "name": name,
                "arguments": arguments,
                "seconds": time.perf_counter() - tool_started,
                "output_chars": len(result),
                "kept": kept,
                "error": error,
            }
            tool_trace.append(trace_entry)
            if kept and prune_tool_results and error is None:
                kept = _prune_tool_result(
                    protocol=protocol,
                    provider=provider,
                    complete=complete,
                    tools=tools,
                    tool_name=name,
                    arguments=arguments,
                    tool_result=result,
                    max_rounds=max_prune_rounds,
                    tool_trace=tool_trace,
                )
                if not kept:
                    result = _FALSE_OBSERVATION
            trace_entry["kept"] = kept
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or ""),
                    "name": name,
                    "content": result,
                }
            )
            if kept and error is None:
                collected_code.append((name, arguments, result))
            if name == "exit":
                should_exit = True
        if should_exit:
            break

    location_output, _ = complete(
        stage="location_summary",
        messages=protocol.summary_messages(
            candidate_structure=candidate_structure,
            collected_code=collected_code,
        ),
    )
    return _finish_execution(
        started=started,
        usage=usage,
        trajectory=trajectory,
        tool_trace=tool_trace,
        file_output=file_output,
        reflection_output=reflection_output,
        location_output=location_output,
        candidate_files=candidate_files,
    )


def _prune_tool_result(
    *,
    protocol: CoSILProtocol,
    provider: CoSILRepositoryProvider,
    complete: Callable[..., tuple[str, dict[str, Any]]],
    tools: Sequence[Mapping[str, Any]],
    tool_name: str,
    arguments: Mapping[str, Any],
    tool_result: str,
    max_rounds: int,
    tool_trace: list[dict[str, Any]],
) -> bool:
    messages: list[dict[str, Any]] = protocol.prune_messages(
        tool_name=tool_name,
        arguments=arguments,
        tool_result=tool_result,
    )
    for _round_index in range(max_rounds):
        _content, assistant = complete(
            stage="prune_explore",
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        messages.append(assistant)
        calls = list(assistant.get("tool_calls") or ())
        if not calls:
            break
        should_exit = False
        for call in calls:
            function = dict(call.get("function") or {})
            name = str(function.get("name") or "")
            arguments = _tool_arguments(function.get("arguments"))
            tool_started = time.perf_counter()
            try:
                result = provider.dispatch(name, arguments)
                error = None
            except Exception as exc:  # noqa: BLE001 - policy observation
                result = f"Tool call failed: {exc}"
                error = result
            tool_trace.append(
                {
                    "phase": "prune",
                    "round": _round_index + 1,
                    "name": name,
                    "arguments": arguments,
                    "seconds": time.perf_counter() - tool_started,
                    "output_chars": len(result),
                    "kept": name != "exit" and error is None,
                    "error": error,
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or ""),
                    "name": name,
                    "content": result,
                }
            )
            should_exit = should_exit or name == "exit"
        if should_exit:
            break
    messages.append({"role": "user", "content": protocol.prune_decision_prompt})
    decision, _assistant = complete(
        stage="prune_decision",
        messages=messages,
        tools=tools,
        tool_choice="none",
    )
    fenced = re.search(r"```(?:[^\n]*\n)?\s*(True|False)\s*```", decision)
    value = fenced.group(1) if fenced is not None else decision.strip()
    return value == "True"


def _finish_execution(
    *,
    started: float,
    usage: dict[str, Any],
    trajectory: Sequence[Mapping[str, Any]],
    tool_trace: Sequence[Mapping[str, Any]],
    file_output: str,
    reflection_output: str = "",
    location_output: str = "",
    candidate_files: Sequence[str] = (),
) -> CoSILExecution:
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    usage["model_calls"] = len(trajectory)
    usage["tool_calls"] = len(tool_trace)
    usage["tool_seconds"] = sum(float(item["seconds"]) for item in tool_trace)
    usage["elapsed_seconds"] = time.perf_counter() - started
    return CoSILExecution(
        file_output=file_output,
        reflection_output=reflection_output,
        location_output=location_output,
        candidate_files=tuple(candidate_files),
        usage=usage,
        tool_trace=tuple(tool_trace),
        trajectory=tuple(trajectory),
    )


def _tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        decoded = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _unsupported_temperature(exc: Exception) -> bool:
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        detail = body.get("error", body)
        if isinstance(detail, Mapping):
            param = str(detail.get("param") or "").lower()
            code = str(detail.get("code") or "").lower()
            message = str(detail.get("message") or "").lower()
            if param == "temperature" and (
                code == "unsupported_value" or "unsupported" in message
            ):
                return True
    message = str(exc).lower()
    return "temperature" in message and "unsupported value" in message


def _unsupported_max_completion_tokens(exc: Exception) -> bool:
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        detail = body.get("error", body)
        if isinstance(detail, Mapping):
            param = str(detail.get("param") or "").lower()
            message = str(detail.get("message") or "").lower()
            if param == "max_completion_tokens" and any(
                marker in message
                for marker in ("unsupported", "unrecognized", "unknown")
            ):
                return True
    message = str(exc).lower()
    return "max_completion_tokens" in message and any(
        marker in message
        for marker in ("unexpected keyword", "unsupported", "unrecognized", "unknown")
    )


def _requires_no_reasoning_with_tools(exc: Exception) -> bool:
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        detail = body.get("error", body)
        if isinstance(detail, Mapping):
            param = str(detail.get("param") or "").lower()
            message = str(detail.get("message") or "").lower()
            if param == "reasoning_effort" and "function tools" in message:
                return True
    message = str(exc).lower()
    return "function tools" in message and "reasoning_effort" in message


def _assistant_message(message: Any) -> dict[str, Any]:
    calls = []
    for call in list(getattr(message, "tool_calls", None) or ()):
        function = getattr(call, "function", None)
        calls.append(
            {
                "id": str(getattr(call, "id", "")),
                "type": str(getattr(call, "type", "function") or "function"),
                "function": {
                    "name": str(getattr(function, "name", "")),
                    "arguments": getattr(function, "arguments", "{}"),
                },
            }
        )
    output: dict[str, Any] = {
        "role": str(getattr(message, "role", "assistant") or "assistant"),
        "content": getattr(message, "content", None),
    }
    if calls:
        output["tool_calls"] = calls
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
    "CoSILAgent",
    "CoSILExecution",
    "CoSILManifestResolver",
    "CoSILPolicyRunner",
    "CoSILProviderFactory",
    "cosil_locations_to_baseline",
    "run_cosil_policy",
]
