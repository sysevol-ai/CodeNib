# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from codenib.clients.cosil_agent import CoSILAgent
from codenib.compiler.manifest import RepoManifest
from codenib.eval.benchmarks.cosil import (
    CoSILLocation,
    parse_cosil_files,
    parse_cosil_locations,
)


def _tool_call(call_id: str, name: str, arguments: str = "{}") -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class _Completions:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.requests: list[dict] = []

    def create(self, **request):
        self.requests.append(request)
        response = self.responses.pop(0)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant",
                        content=response.get("content", ""),
                        tool_calls=response.get("tool_calls"),
                    )
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=2,
                prompt_tokens_details=SimpleNamespace(cached_tokens=1),
            ),
        )


class _Client:
    def __init__(self, responses: list[dict]) -> None:
        self.chat = SimpleNamespace(completions=_Completions(responses))


class _UnsupportedTemperatureError(Exception):
    body = {
        "error": {
            "message": "Unsupported value for temperature",
            "param": "temperature",
            "code": "unsupported_value",
        }
    }


class _UnsupportedMaxCompletionTokensError(TypeError):
    pass


class _TemperatureRejectingCompletions(_Completions):
    def __init__(self, responses: list[dict]) -> None:
        super().__init__(responses)
        self.attempts: list[dict] = []
        self.rejected = False

    def create(self, **request):
        self.attempts.append(request)
        if "temperature" in request and not self.rejected:
            self.rejected = True
            raise _UnsupportedTemperatureError
        return super().create(**request)


class _MaxCompletionTokensRejectingCompletions(_Completions):
    def __init__(self, responses: list[dict]) -> None:
        super().__init__(responses)
        self.attempts: list[dict] = []
        self.rejected = False

    def create(self, **request):
        self.attempts.append(request)
        if "max_completion_tokens" in request and not self.rejected:
            self.rejected = True
            raise _UnsupportedMaxCompletionTokensError(
                "create() got an unexpected keyword argument 'max_completion_tokens'"
            )
        return super().create(**request)


class _ToolReasoningError(Exception):
    body = {
        "error": {
            "message": "Function tools with reasoning_effort are not supported",
            "param": "reasoning_effort",
        }
    }


class _ToolReasoningRejectingCompletions(_Completions):
    def __init__(self, responses: list[dict]) -> None:
        super().__init__(responses)
        self.attempts: list[dict] = []
        self.rejected = False

    def create(self, **request):
        self.attempts.append(request)
        if request.get("tools") and not self.rejected:
            self.rejected = True
            raise _ToolReasoningError
        return super().create(**request)


class _AlwaysToolReasoningRejectingCompletions(_Completions):
    def __init__(self, responses: list[dict]) -> None:
        super().__init__(responses)
        self.attempts: list[dict] = []

    def create(self, **request):
        self.attempts.append(request)
        if request.get("tools"):
            raise _ToolReasoningError
        return super().create(**request)


def test_file_parser_requires_balanced_fences_and_known_paths() -> None:
    valid = ("src/service.py", "src/model.py")

    assert (
        parse_cosil_files(
            "```\nrepo/src/service.py\nsrc/model.py\nmissing.py\n```",
            valid_files=valid,
            root_name="repo",
        )
        == valid
    )
    assert (
        parse_cosil_files(
            "```\nsrc/service.py",
            valid_files=valid,
            root_name="repo",
        )
        == ()
    )
    assert parse_cosil_files(
        "```\ndjango/db/models.py\n```",
        valid_files=("django/db/models.py",),
        root_name="django",
    ) == ("django/db/models.py",)
    assert parse_cosil_files(
        "```\n.github/scripts/check.py\n```",
        valid_files=(".github/scripts/check.py",),
    ) == (".github/scripts/check.py",)


def test_xml_parser_is_structured_and_candidate_bounded() -> None:
    result = parse_cosil_locations(
        """
        analysis before XML
        <locations>
          <location>
            <file>src/service.py</file>
            <type>function</type>
            <name>BillingService.calculate_tax</name>
          </location>
          <location>
            <file>outside.py</file>
            <type>function</type>
            <name>hallucinated</name>
          </location>
          <location>
            <file>src/service.py</file>
            <type>variable</type>
            <name>unsupported</name>
          </location>
        </locations>
        """,
        valid_files=("src/service.py",),
    )

    assert result == (
        CoSILLocation(
            file_path="src/service.py",
            kind="function",
            name="BillingService.calculate_tax",
        ),
    )
    assert (
        parse_cosil_locations(
            "<locations><location></locations>",
            valid_files=("src/service.py",),
        )
        == ()
    )


def test_xml_parser_preserves_same_named_root_and_limits_to_five() -> None:
    locations = "".join(
        "<location><file>django/db/models.py</file><type>function</type>"
        f"<name>candidate_{index}</name></location>"
        for index in range(6)
    )

    result = parse_cosil_locations(
        f"<locations>{locations}</locations>",
        valid_files=("django/db/models.py",),
        root_name="django",
    )

    assert len(result) == 5
    assert all(item.file_path == "django/db/models.py" for item in result)


def test_native_policy_runs_reflection_tools_and_xml_summary(
    integration_manifest: Path,
    monkeypatch,
) -> None:
    manifest = RepoManifest.load(integration_manifest)
    monkeypatch.setattr(
        "codenib.clients.cosil_agent.select_checkout_manifest",
        lambda *_args, **_kwargs: integration_manifest,
    )
    client = _Client(
        [
            {"content": "```\nrepo/src/service.py\n```"},
            {"content": "```\nsrc/service.py\n```"},
            {
                "tool_calls": [
                    _tool_call(
                        "call-1",
                        "get_code_of_class_function",
                        '{"file_name":"src/service.py",'
                        '"class_name":"BillingService",'
                        '"func_name":"calculate_tax"}',
                    )
                ]
            },
            {"tool_calls": [_tool_call("call-2", "exit")]},
            {
                "content": (
                    "<locations><location><file>src/service.py</file>"
                    "<type>function</type>"
                    "<name>BillingService.calculate_tax</name>"
                    "</location></locations>"
                )
            },
        ]
    )
    agent = CoSILAgent(
        model="test-model",
        manifest_path=integration_manifest,
        max_tool_rounds=2,
        prune_tool_results=False,
        client_factory=lambda: client,
    )

    result = asyncio.run(
        agent.locate_code("Tax calculations are wrong", manifest.repo_path)
    )

    assert result.success is True
    assert len(result.locations) == 1
    location = result.locations[0]
    assert location.file_path == "src/service.py"
    assert location.name == "BillingService.calculate_tax()"
    assert location.type == "method"
    assert (location.line_start, location.line_end) == (2, 4)
    assert result.usage["model_calls"] == 5
    assert result.usage["tool_calls"] == 2
    assert result.usage["total_tokens"] == 60

    requests = client.chat.completions.requests
    assert "Import Relations" in requests[1]["messages"][0]["content"]
    assert "BillingService: ['calculate_tax']" in requests[2]["messages"][1]["content"]
    assert [item["function"]["name"] for item in requests[2]["tools"]] == [
        "get_code_of_class",
        "get_code_of_class_function",
        "get_code_of_file_function",
        "exit",
    ]
    assert "def calculate_tax" in requests[3]["messages"][-1]["content"]
    assert "Relevant code retrieved" in requests[4]["messages"][1]["content"]
    assert '"file_name": "src/service.py"' in requests[4]["messages"][1]["content"]


def test_failed_reflection_keeps_initial_file_candidates(
    integration_manifest: Path,
    monkeypatch,
) -> None:
    manifest = RepoManifest.load(integration_manifest)
    monkeypatch.setattr(
        "codenib.clients.cosil_agent.select_checkout_manifest",
        lambda *_args, **_kwargs: integration_manifest,
    )
    client = _Client(
        [
            {"content": "```\nsrc/model.py\n```"},
            {"content": "Unable to format a reflection"},
            {"content": "No tool call"},
            {"content": "Malformed summary"},
        ]
    )
    agent = CoSILAgent(
        model="test-model",
        manifest_path=integration_manifest,
        max_tool_rounds=1,
        prune_tool_results=False,
        client_factory=lambda: client,
    )

    result = asyncio.run(agent.locate_code("Fix tax model", manifest.repo_path))

    assert result.success is True
    assert [location.file_path for location in result.locations] == ["src/model.py"]
    assert result.raw_output["candidate_files"] == ["src/model.py"]


def test_policy_retries_without_unsupported_temperature(
    integration_manifest: Path,
    monkeypatch,
) -> None:
    manifest = RepoManifest.load(integration_manifest)
    monkeypatch.setattr(
        "codenib.clients.cosil_agent.select_checkout_manifest",
        lambda *_args, **_kwargs: integration_manifest,
    )
    completions = _TemperatureRejectingCompletions(
        [
            {"content": "```\nsrc/model.py\n```"},
            {"content": "```\nsrc/model.py\n```"},
            {"content": "No tool call"},
            {"content": "<locations></locations>"},
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    agent = CoSILAgent(
        model="reasoning-model",
        manifest_path=integration_manifest,
        max_tool_rounds=1,
        prune_tool_results=False,
        client_factory=lambda: client,
    )

    result = asyncio.run(agent.locate_code("Fix tax model", manifest.repo_path))

    assert result.success is True
    assert "temperature" in completions.attempts[0]
    assert all("temperature" not in item for item in completions.attempts[1:])


def test_policy_retries_with_legacy_max_tokens(
    integration_manifest: Path,
    monkeypatch,
) -> None:
    manifest = RepoManifest.load(integration_manifest)
    monkeypatch.setattr(
        "codenib.clients.cosil_agent.select_checkout_manifest",
        lambda *_args, **_kwargs: integration_manifest,
    )
    completions = _MaxCompletionTokensRejectingCompletions(
        [
            {"content": "```\nsrc/model.py\n```"},
            {"content": "```\nsrc/model.py\n```"},
            {"content": "No tool call"},
            {"content": "<locations></locations>"},
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    agent = CoSILAgent(
        model="legacy-sdk-model",
        manifest_path=integration_manifest,
        max_tool_rounds=1,
        prune_tool_results=False,
        client_factory=lambda: client,
    )

    result = asyncio.run(agent.locate_code("Fix tax model", manifest.repo_path))

    assert result.success is True
    assert "max_completion_tokens" in completions.attempts[0]
    assert "max_tokens" in completions.attempts[1]
    assert all("max_completion_tokens" not in item for item in completions.attempts[1:])


def test_policy_disables_reasoning_when_tools_require_it(
    integration_manifest: Path,
    monkeypatch,
) -> None:
    manifest = RepoManifest.load(integration_manifest)
    monkeypatch.setattr(
        "codenib.clients.cosil_agent.select_checkout_manifest",
        lambda *_args, **_kwargs: integration_manifest,
    )
    completions = _ToolReasoningRejectingCompletions(
        [
            {"content": "```\nsrc/model.py\n```"},
            {"content": "```\nsrc/model.py\n```"},
            {"content": "No tool call"},
            {"content": "<locations></locations>"},
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    agent = CoSILAgent(
        model="reasoning-model",
        manifest_path=integration_manifest,
        max_tool_rounds=1,
        prune_tool_results=False,
        client_factory=lambda: client,
    )

    result = asyncio.run(agent.locate_code("Fix tax model", manifest.repo_path))

    assert result.success is True
    tool_attempts = [item for item in completions.attempts if item.get("tools")]
    assert "reasoning_effort" not in tool_attempts[0]
    assert tool_attempts[1]["reasoning_effort"] == "none"


def test_policy_bounds_reasoning_fallback_retry(
    integration_manifest: Path,
    monkeypatch,
) -> None:
    manifest = RepoManifest.load(integration_manifest)
    monkeypatch.setattr(
        "codenib.clients.cosil_agent.select_checkout_manifest",
        lambda *_args, **_kwargs: integration_manifest,
    )
    completions = _AlwaysToolReasoningRejectingCompletions(
        [
            {"content": "```\nsrc/model.py\n```"},
            {"content": "```\nsrc/model.py\n```"},
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    agent = CoSILAgent(
        model="reasoning-model",
        manifest_path=integration_manifest,
        max_tool_rounds=1,
        prune_tool_results=False,
        client_factory=lambda: client,
    )

    result = asyncio.run(agent.locate_code("Fix tax model", manifest.repo_path))

    assert result.success is False
    tool_attempts = [item for item in completions.attempts if item.get("tools")]
    assert len(tool_attempts) == 2
    assert "reasoning_effort" not in tool_attempts[0]
    assert tool_attempts[1]["reasoning_effort"] == "none"


def test_prune_false_masks_code_before_next_main_round(
    integration_manifest: Path,
    monkeypatch,
) -> None:
    manifest = RepoManifest.load(integration_manifest)
    monkeypatch.setattr(
        "codenib.clients.cosil_agent.select_checkout_manifest",
        lambda *_args, **_kwargs: integration_manifest,
    )
    client = _Client(
        [
            {"content": "```\nsrc/service.py\n```"},
            {"content": "```\nsrc/service.py\n```"},
            {
                "tool_calls": [
                    _tool_call(
                        "main-1",
                        "get_code_of_file_function",
                        '{"file_name":"src/service.py","func_name":"helper"}',
                    )
                ]
            },
            {
                "tool_calls": [
                    _tool_call(
                        "prune-1",
                        "get_code_of_class",
                        '{"file_name":"src/service.py",'
                        '"class_name":"BillingService"}',
                    )
                ]
            },
            {"content": "```\nFalse\n```"},
            {"tool_calls": [_tool_call("main-2", "exit")]},
            {"content": "<locations></locations>"},
        ]
    )
    agent = CoSILAgent(
        model="test-model",
        manifest_path=integration_manifest,
        max_tool_rounds=2,
        prune_tool_results=True,
        max_prune_rounds=1,
        client_factory=lambda: client,
    )

    result = asyncio.run(agent.locate_code("Fix tax helper", manifest.repo_path))

    assert result.success is True
    assert result.usage["tool_calls"] == 3
    assert [item["phase"] for item in result.raw_output["tool_trace"]] == [
        "location",
        "prune",
        "location",
    ]
    requests = client.chat.completions.requests
    next_main_messages = requests[5]["messages"]
    assert "not related to the bug" in next_main_messages[-1]["content"]
    summary = requests[6]["messages"][1]["content"]
    assert "Relevant code retrieved" not in summary
