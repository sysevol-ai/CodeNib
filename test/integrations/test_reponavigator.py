# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from codenib.compiler.artifact_fingerprints import regular_file_fingerprint
from codenib.compiler.manifest import RepoManifest
from codenib.integrations._repository import IntegrationCapabilityError
from codenib.integrations.reponavigator import (
    REPONAVIGATOR_PAPER_REVISION,
    RepoNavigatorDefinitionSignal,
    RepoNavigatorRepositoryProvider,
    get_reponavigator_tool_schemas,
)
from codenib.mcp.context import ServerContext
from codenib.scip_interface.lsp_occurrence_index import (
    SCIPOccurrence,
    SCIPOccurrenceIndex,
)


class RecordingDefinitionProvider:
    def __init__(
        self,
        result: object | None = None,
        *,
        position_encoding: str = "utf-32",
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.result = result or SimpleNamespace(
            file="src/service.py",
            start_line=5,
        )
        self.position_encoding = position_encoding

    def definition(self, **kwargs: object) -> list[object]:
        self.calls.append(dict(kwargs))
        return [self.result]


class NativeOccurrenceIndexStub:
    behavior_contract = "static_native_occurrence_lsp_v1"
    position_encoding = "UTF16"
    authoritative_empty = True

    def __init__(self, occurrence_count: int) -> None:
        self.occurrence_count = occurrence_count

    def occurrence_at(self, file_path: str, line: int, character: int) -> object:
        del file_path, line, character
        return None

    def definitions(self, **kwargs: object) -> list[object]:
        del kwargs
        return []


def _write_manifest_occurrence_index(
    manifest_path: Path,
    occurrence_index: SCIPOccurrenceIndex,
) -> Path:
    manifest = RepoManifest.load(manifest_path)
    entry = manifest.indexes["symbol_graph"]
    generation = Path(entry.path)
    index_path = generation / "lsp_index.pkl"
    occurrence_index.save(index_path)
    receipt = {
        "relative_path": "lsp_index.pkl",
        **regular_file_fingerprint(index_path),
    }
    entry.config["lsp_occurrence_artifact"] = dict(receipt)
    entry.metadata["lsp_occurrence_artifact"] = dict(receipt)
    manifest.save(manifest_path)
    return index_path


@pytest.fixture()
def semantic_manifest(integration_manifest: Path) -> Path:
    _write_manifest_occurrence_index(
        integration_manifest,
        SCIPOccurrenceIndex(
            [
                SCIPOccurrence(
                    "src/service.py",
                    0,
                    6,
                    0,
                    20,
                    "scip-python fixture BillingService#",
                    1,
                ),
                SCIPOccurrence(
                    "src/service.py",
                    3,
                    15,
                    3,
                    21,
                    "scip-python fixture helper().",
                    8,
                ),
                SCIPOccurrence(
                    "src/service.py",
                    5,
                    4,
                    5,
                    10,
                    "scip-python fixture helper().",
                    1,
                ),
            ]
        ),
    )
    return integration_manifest


@pytest.fixture()
def provider(semantic_manifest: Path) -> RepoNavigatorRepositoryProvider:
    return RepoNavigatorRepositoryProvider.from_manifest(semantic_manifest)


def test_provider_loads_only_symbol_graph_and_reattaches_definition_signal(
    semantic_manifest: Path,
) -> None:
    with patch.object(ServerContext, "load", wraps=ServerContext.load) as load:
        provider = RepoNavigatorRepositoryProvider.from_manifest(semantic_manifest)

    load.assert_called_once_with(
        semantic_manifest,
        views=frozenset({"symbol_graph"}),
    )
    assert provider.context.symbol_graph is not None
    assert provider.context.bm25 is None
    assert provider.context.vector is None
    assert provider.signal_metadata() == {
        "backend": "scip_occurrence",
        "provider": "codenib_static_index",
        "behavior_contract": "static_scip_occurrence_lsp_v1",
        "position_granularity": "character",
        "position_encoding": "utf-8",
        "semantic": True,
        "detail": (
            "character-level definition resolution from the persisted SCIP "
            "occurrence index"
        ),
    }


def test_graph_only_manifest_requires_explicit_degraded_opt_in(
    integration_manifest: Path,
) -> None:
    with pytest.raises(IntegrationCapabilityError, match="semantic definition signal"):
        RepoNavigatorRepositoryProvider.from_manifest(integration_manifest)

    provider = RepoNavigatorRepositoryProvider.from_manifest(
        integration_manifest,
        allow_graph_fallback=True,
    )

    assert provider.definition_signal.backend == "symbol_graph_fallback"
    assert provider.definition_signal.semantic is False


def test_empty_occurrence_signal_is_not_reported_as_semantic(
    integration_manifest: Path,
) -> None:
    _write_manifest_occurrence_index(integration_manifest, SCIPOccurrenceIndex([]))

    with pytest.raises(IntegrationCapabilityError, match="semantic definition signal"):
        RepoNavigatorRepositoryProvider.from_manifest(integration_manifest)

    provider = RepoNavigatorRepositoryProvider.from_manifest(
        integration_manifest,
        allow_graph_fallback=True,
    )
    assert provider.definition_signal.semantic is False
    assert "occurrence index is empty" in provider.definition_signal.detail
    assert provider.jump("src/service.py", "helper").startswith(
        'The definition of symbol "helper" is:'
    )


def test_unreceipted_occurrence_sidecar_is_not_loaded_from_mutable_path(
    integration_manifest: Path,
) -> None:
    manifest = RepoManifest.load(integration_manifest)
    generation = Path(manifest.indexes["symbol_graph"].path)
    SCIPOccurrenceIndex(
        [
            SCIPOccurrence(
                "src/service.py",
                3,
                15,
                3,
                21,
                "scip-python fixture helper().",
                8,
            )
        ]
    ).save(generation / "lsp_index.pkl")

    with pytest.raises(IntegrationCapabilityError, match="semantic definition signal"):
        RepoNavigatorRepositoryProvider.from_manifest(integration_manifest)

    provider = RepoNavigatorRepositoryProvider.from_manifest(
        integration_manifest,
        allow_graph_fallback=True,
    )
    assert provider.definition_signal.backend == "symbol_graph_fallback"
    assert getattr(provider.context.symbol_graph, "lsp_occurrence_index", None) is None


def test_empty_graph_attached_occurrence_rebinds_to_graph_only_provider(
    integration_manifest: Path,
) -> None:
    context = ServerContext.load(integration_manifest, views={"symbol_graph"})
    occurrence_index = SCIPOccurrenceIndex([])
    context.symbol_graph.lsp_occurrence_index = occurrence_index

    provider = RepoNavigatorRepositoryProvider(
        context,
        occurrence_index=occurrence_index,
        allow_graph_fallback=True,
    )

    assert provider.lsp_provider.occurrence_index is None
    assert provider.jump("src/service.py", "helper").startswith(
        'The definition of symbol "helper" is:'
    )


def test_native_occurrence_count_and_backend_are_reported_accurately(
    integration_manifest: Path,
) -> None:
    context = ServerContext.load(integration_manifest, views={"symbol_graph"})

    with pytest.raises(IntegrationCapabilityError, match="semantic definition signal"):
        RepoNavigatorRepositoryProvider(
            context,
            occurrence_index=NativeOccurrenceIndexStub(0),
        )

    empty_provider = RepoNavigatorRepositoryProvider(
        context,
        occurrence_index=NativeOccurrenceIndexStub(0),
        allow_graph_fallback=True,
    )
    assert empty_provider.definition_signal.backend == "symbol_graph_fallback"
    assert "native occurrence index is empty" in empty_provider.definition_signal.detail
    assert empty_provider.jump("src/service.py", "helper").startswith(
        'The definition of symbol "helper" is:'
    )

    native_provider = RepoNavigatorRepositoryProvider(
        context,
        occurrence_index=NativeOccurrenceIndexStub(1),
    )
    assert native_provider.signal_metadata() == {
        "backend": "native_occurrence",
        "provider": "codenib_static_index",
        "behavior_contract": "static_native_occurrence_lsp_v1",
        "position_granularity": "character",
        "position_encoding": "utf-16",
        "semantic": True,
        "detail": "character-level definition resolution from the native occurrence index",
    }


def test_per_call_graph_fallback_requires_opt_in_and_updates_signal(
    integration_manifest: Path,
) -> None:
    occurrence_index = SCIPOccurrenceIndex(
        [
            SCIPOccurrence(
                "src/service.py",
                3,
                15,
                3,
                21,
                "scip-python fixture helper().",
                8,
            )
        ]
    )
    context = ServerContext.load(integration_manifest, views={"symbol_graph"})
    blocked = RepoNavigatorRepositoryProvider(
        context,
        occurrence_index=occurrence_index,
    )

    assert blocked.jump("src/service.py", "helper") == (
        "Jump failed: definition resolution degraded to the symbol graph; pass "
        "allow_graph_fallback=True to permit this call"
    )
    assert blocked.signal_metadata()["backend"] == "symbol_graph_fallback"
    assert blocked.signal_metadata()["semantic"] is False

    allowed = RepoNavigatorRepositoryProvider(
        context,
        occurrence_index=occurrence_index,
        allow_graph_fallback=True,
    )
    result = allowed.jump("src/service.py", "helper")

    assert result.startswith('The definition of symbol "helper" is:')
    assert allowed.signal_metadata()["backend"] == "symbol_graph_fallback"
    assert allowed.signal_metadata()["behavior_contract"] == (
        "static_symbol_graph_position_lsp_v1"
    )
    assert allowed.signal_metadata()["semantic"] is False


def test_per_call_graph_gate_does_not_read_shared_signal_state(
    integration_manifest: Path,
    monkeypatch,
) -> None:
    occurrence_index = SCIPOccurrenceIndex(
        [
            SCIPOccurrence(
                "src/service.py",
                3,
                15,
                3,
                21,
                "scip-python fixture helper().",
                8,
            )
        ]
    )
    context = ServerContext.load(integration_manifest, views={"symbol_graph"})
    provider = RepoNavigatorRepositoryProvider(
        context,
        occurrence_index=occurrence_index,
    )

    def overwrite_latest_signal(_signal) -> None:
        with provider._signal_lock:
            provider._definition_signal = provider._configured_definition_signal

    monkeypatch.setattr(
        provider,
        "_record_definition_signal",
        overwrite_latest_signal,
    )

    assert provider.jump("src/service.py", "helper") == (
        "Jump failed: definition resolution degraded to the symbol graph; pass "
        "allow_graph_fallback=True to permit this call"
    )


def test_paper_contract_exposes_one_independent_jump_schema() -> None:
    schemas = get_reponavigator_tool_schemas()

    assert REPONAVIGATOR_PAPER_REVISION == "arXiv:2512.20957v6"
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "jump"
    assert schemas[0]["function"]["parameters"]["required"] == [
        "file_path",
        "symbol",
    ]
    assert schemas[0]["function"]["parameters"]["properties"]["index"] == {
        "type": "integer",
        "minimum": 0,
        "default": 0,
        "description": (
            "Zero-based occurrence index used when the symbol appears more than "
            "once in the file."
        ),
    }

    schemas[0]["function"]["name"] = "changed"
    assert get_reponavigator_tool_schemas()[0]["function"]["name"] == "jump"


def test_jump_converts_occurrence_index_to_exact_definition_position(
    integration_manifest: Path,
) -> None:
    context = ServerContext.load(integration_manifest, views={"symbol_graph"})
    lsp_provider = RecordingDefinitionProvider()
    provider = RepoNavigatorRepositoryProvider(
        context,
        lsp_provider=lsp_provider,
    )

    observation = provider.jump("src/service.py", "helper", index=1)

    assert observation == (
        'The definition of symbol "helper" is:\n'
        "def helper(amount):\n"
        "    return amount * 0.08\n\n"
        "It is defined in: src/service.py"
    )
    assert lsp_provider.calls == [
        {
            "file_path": "src/service.py",
            "line": 5,
            "character": 9,
            "top_k": 1,
        }
    ]


def test_default_occurrence_is_zero_based(
    integration_manifest: Path,
) -> None:
    context = ServerContext.load(integration_manifest, views={"symbol_graph"})
    lsp_provider = RecordingDefinitionProvider()
    provider = RepoNavigatorRepositoryProvider(
        context,
        lsp_provider=lsp_provider,
    )

    provider.jump("src/service.py", "helper")

    assert lsp_provider.calls[0]["line"] == 3
    assert lsp_provider.calls[0]["character"] == 20


def test_jump_converts_non_ascii_prefix_to_provider_position_encoding(
    integration_manifest: Path,
) -> None:
    context = ServerContext.load(integration_manifest, views={"symbol_graph"})
    lsp_provider = RecordingDefinitionProvider(position_encoding="UTF16")
    provider = RepoNavigatorRepositoryProvider(
        context,
        lsp_provider=lsp_provider,
    )

    with patch.object(
        provider.repository,
        "source_lines",
        return_value=["é😀 helper()"],
    ):
        provider.jump("src/service.py", "helper")

    assert provider.definition_signal.position_encoding == "utf-16"
    assert lsp_provider.calls[0]["line"] == 0
    assert lsp_provider.calls[0]["character"] == 9


def test_position_encoding_does_not_read_shared_signal_state(
    integration_manifest: Path,
) -> None:
    context = ServerContext.load(integration_manifest, views={"symbol_graph"})
    lsp_provider = RecordingDefinitionProvider(position_encoding="UTF16")
    provider = RepoNavigatorRepositoryProvider(
        context,
        lsp_provider=lsp_provider,
    )

    def source_lines(_file_path: str) -> list[str]:
        with provider._signal_lock:
            provider._definition_signal = RepoNavigatorDefinitionSignal(
                backend="concurrent-call",
                provider="test",
                behavior_contract="test",
                position_granularity="character",
                position_encoding="utf-32",
                semantic=True,
                detail="simulated concurrent metadata update",
            )
        return ["é😀 helper()"]

    with patch.object(provider.repository, "source_lines", side_effect=source_lines):
        provider.jump("src/service.py", "helper")

    assert lsp_provider.calls[0]["character"] == 9


def test_static_provider_returns_containing_definition_source(
    provider: RepoNavigatorRepositoryProvider,
) -> None:
    assert provider.jump("src/service.py", "helper") == (
        'The definition of symbol "helper" is:\n'
        "def helper(amount):\n"
        "    return amount * 0.08\n\n"
        "It is defined in: src/service.py"
    )
    assert provider.jump("src/service.py", "BillingService") == (
        'The definition of symbol "BillingService" is:\n'
        "class BillingService:\n"
        "    def calculate_tax(self, amount):\n"
        '        """Compute sales tax for an invoice."""\n'
        "        return helper(amount)\n\n"
        "It is defined in: src/service.py"
    )


def test_jump_preserves_cross_file_definition_path(
    integration_manifest: Path,
) -> None:
    context = ServerContext.load(integration_manifest, views={"symbol_graph"})
    provider = RepoNavigatorRepositoryProvider(
        context,
        lsp_provider=RecordingDefinitionProvider(
            SimpleNamespace(file="src/other.py", start_line=0)
        ),
    )

    result = provider.jump("src/service.py", "helper")

    assert 'The definition of symbol "helper" is:' in result
    assert '"""Fallback tax helper."""' in result
    assert result.endswith("It is defined in: src/other.py")


def test_scip_signal_filters_text_that_is_not_a_semantic_occurrence(
    provider: RepoNavigatorRepositoryProvider,
) -> None:
    source_path = Path(provider.context.manifest.repo_path) / "src/service.py"
    lines = source_path.read_text(encoding="utf-8").splitlines()
    lines[4] = "# helper is only mentioned in this comment"
    source_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = provider.jump("src/service.py", "helper", index=1)

    assert result.startswith('The definition of symbol "helper" is:')
    assert "Jump failed" not in result


def test_dispatch_accepts_json_and_uppercase_alias(
    provider: RepoNavigatorRepositoryProvider,
) -> None:
    result = provider.dispatch(
        "jump",
        '{"file_path":"src/service.py","symbol":"helper","index":0}',
    )

    assert result.startswith('The definition of symbol "helper" is:')
    assert list(provider.bindings()) == ["jump"]
    with pytest.raises(KeyError, match="unknown RepoNavigator tool"):
        provider.dispatch("bash", {})
    with pytest.raises(ValueError, match="valid JSON"):
        provider.dispatch("Jump", "{")
    with pytest.raises(ValueError, match="decode to an object"):
        provider.dispatch("Jump", "[]")


@pytest.mark.parametrize(
    ("file_path", "symbol", "index", "message"),
    [
        ("../secret.py", "token", 0, "path traversal"),
        ("src/service.py", "missing", 0, "no resolvable occurrence"),
        ("src/service.py", "helper", 2, "out of range"),
        ("src/service.py", "helper", -1, "non-negative integer"),
        ("src/service.py", "helper", True, "non-negative integer"),
        ("src/service.py", "bad\nsymbol", 0, "single-line text"),
    ],
)
def test_jump_returns_bounded_observations_for_invalid_calls(
    provider: RepoNavigatorRepositoryProvider,
    file_path: str,
    symbol: str,
    index: object,
    message: str,
) -> None:
    result = provider.jump(file_path, symbol, index=index)  # type: ignore[arg-type]

    assert result.startswith("Jump failed: ")
    assert message in result


def test_jump_normalizes_invalid_path_exceptions(
    provider: RepoNavigatorRepositoryProvider,
) -> None:
    result = provider.jump("\0", "helper")

    assert result == "Jump failed: file_path must not contain NUL characters"


def test_jump_returns_definition_provider_failures(
    integration_manifest: Path,
) -> None:
    class FailingProvider:
        def definition(self, **kwargs: object) -> list[object]:
            del kwargs
            raise ValueError("no definition at occurrence")

    context = ServerContext.load(integration_manifest, views={"symbol_graph"})
    provider = RepoNavigatorRepositoryProvider(
        context,
        lsp_provider=FailingProvider(),
    )

    assert provider.jump("src/service.py", "helper") == (
        "Jump failed: no definition at occurrence"
    )


def test_jump_returns_structured_live_provider_failures(
    integration_manifest: Path,
) -> None:
    class ErrorProvider:
        provider = "json_rpc"

        def definition(self, **kwargs: object) -> dict[str, str]:
            del kwargs
            return {"error": "language server returned no definition"}

    context = ServerContext.load(integration_manifest, views={"symbol_graph"})
    provider = RepoNavigatorRepositoryProvider(
        context,
        lsp_provider=ErrorProvider(),
    )

    assert provider.definition_signal.backend == "live_lsp"
    assert provider.jump("src/service.py", "helper") == (
        "Jump failed: language server returned no definition"
    )


def test_jump_returns_source_read_failures(
    provider: RepoNavigatorRepositoryProvider,
) -> None:
    with patch.object(
        provider.repository,
        "source_lines",
        side_effect=OSError("source is unavailable"),
    ):
        result = provider.jump("src/service.py", "helper")

    assert result == "Jump failed: source is unavailable"


def test_jump_bounds_large_definition_source(integration_manifest: Path) -> None:
    context = ServerContext.load(integration_manifest, views={"symbol_graph"})
    provider = RepoNavigatorRepositoryProvider(
        context,
        lsp_provider=RecordingDefinitionProvider(
            SimpleNamespace(file="src/service.py", start_line=0)
        ),
        max_source_lines=1,
    )

    result = provider.jump("src/service.py", "BillingService")

    assert result == (
        'The definition of symbol "BillingService" is:\n'
        "class BillingService:\n"
        "...[truncated RepoNavigator definition]\n\n"
        "It is defined in: src/service.py"
    )


def test_source_budgets_must_be_positive(integration_manifest: Path) -> None:
    context = ServerContext.load(integration_manifest, views={"symbol_graph"})

    with pytest.raises(ValueError, match="max_source_lines"):
        RepoNavigatorRepositoryProvider(context, max_source_lines=0)
    with pytest.raises(ValueError, match="max_source_chars"):
        RepoNavigatorRepositoryProvider(context, max_source_chars=8)
