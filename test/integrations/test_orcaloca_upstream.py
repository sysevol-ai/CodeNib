# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Optional compatibility probe against a pinned OrcaLoca checkout."""

from __future__ import annotations

import importlib
import inspect
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from codenib.compiler.manifest import RepoManifest
from codenib.integrations.orcaloca import (
    ORCALOCA_MANAGER_HOOKS,
    ORCALOCA_REVISION,
    ORCALOCA_TOOL_NAMES,
    OrcaLocaSearchProvider,
    make_orcaloca_search_manager_factory,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def upstream_modules() -> SimpleNamespace:
    raw_checkout = os.environ.get("ORCALOCA_CHECKOUT")
    if not raw_checkout:
        pytest.skip("set ORCALOCA_CHECKOUT to run the pinned upstream probe")
    checkout = Path(raw_checkout).expanduser().resolve()
    if not (checkout / "Orcar" / "search_agent.py").is_file():
        pytest.fail(f"not an OrcaLoca checkout: {checkout}")

    ancestor = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "merge-base",
            "--is-ancestor",
            ORCALOCA_REVISION,
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if ancestor.returncode != 0:
        pytest.fail(
            "OrcaLoca checkout does not contain pinned revision " f"{ORCALOCA_REVISION}"
        )

    pinned_manager = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "show",
            f"{ORCALOCA_REVISION}:Orcar/search/search_tool.py",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    current_manager = (checkout / "Orcar" / "search" / "search_tool.py").read_text(
        encoding="utf-8"
    )
    if current_manager != pinned_manager:
        pytest.fail(
            "OrcaLoca SearchManager drifted from the supported revision "
            f"{ORCALOCA_REVISION}"
        )

    sys.path.insert(0, str(checkout))
    search = importlib.import_module("Orcar.search")
    search_agent = importlib.import_module("Orcar.search_agent")
    types = importlib.import_module("Orcar.types")
    loaded_path = Path(inspect.getfile(search_agent)).resolve()
    if not loaded_path.is_relative_to(checkout):
        pytest.fail(f"loaded OrcaLoca from {loaded_path}, not {checkout}")
    return SimpleNamespace(
        checkout=checkout,
        SearchManager=search.SearchManager,
        search_agent=search_agent,
        types=types,
    )


def _location_tuple(location_info) -> tuple[str, str, int, int, str]:
    location = location_info.loc
    return (
        location.file_name,
        location.node_name,
        location.start_line,
        location.end_line,
        location_info.type,
    )


def test_upstream_contract_and_semantic_locations(
    upstream_modules: SimpleNamespace,
    orcaloca_contract_manifest: Path,
) -> None:
    provider = OrcaLocaSearchProvider.from_manifest(orcaloca_contract_manifest)
    upstream_type = upstream_modules.SearchManager

    for name in ORCALOCA_TOOL_NAMES:
        upstream_parameters = list(
            inspect.signature(getattr(upstream_type, name)).parameters
        )
        provider_parameters = list(
            inspect.signature(getattr(provider, name)).parameters
        )
        assert upstream_parameters[1:] == provider_parameters
    for name in ORCALOCA_MANAGER_HOOKS:
        assert hasattr(upstream_type, name)

    upstream = upstream_type(repo_path=provider.repo_path)
    queries = (
        "src/service.py",
        "src/service.py::BillingService",
        "src/service.py::BillingService::calculate_tax",
        "src/service.py::helper",
        "src/model.py::TaxRule",
    )
    for query in queries:
        expected = upstream._get_exact_loc(query)
        actual = provider._get_exact_loc(query)
        assert expected is not None
        assert actual is not None
        assert _location_tuple(actual) == _location_tuple(expected)

    distance_pairs = (
        (
            "src/service.py::BillingService::calculate_tax",
            "src/service.py::helper",
        ),
        (
            "src/service.py::BillingService::calculate_tax",
            "src/other.py::helper",
        ),
    )
    for first, second in distance_pairs:
        assert provider.get_distance_between_queries(
            first, second
        ) == upstream.get_distance_between_queries(first, second)

    upstream_results = {
        "tree": upstream.search_file_tree(),
        "file": upstream.search_file_contents("service.py", "src"),
        "source": upstream.search_source_code(
            "src/service.py", "return helper(amount)"
        ),
        "class": upstream.search_class("BillingService"),
        "method": upstream.search_method_in_class(
            "BillingService", "calculate_tax", "src/service.py"
        ),
        "callable": upstream.search_callable("helper", "src/service.py"),
    }
    provider_results = {
        "tree": provider.search_file_tree(),
        "file": provider.search_file_contents("service.py", "src"),
        "source": provider.search_source_code(
            "src/service.py", "return helper(amount)"
        ),
        "class": provider.search_class("BillingService"),
        "method": provider.search_method_in_class(
            "BillingService", "calculate_tax", "src/service.py"
        ),
        "callable": provider.search_callable("helper", "src/service.py"),
    }
    markers = {
        "tree": "service.py",
        "file": "File Path: src/service.py",
        "source": "File Path: src/service.py",
        "class": "File Path: src/service.py",
        "method": "File Path: src/service.py",
        "callable": "File Path: src/service.py",
    }
    for result_name, marker in markers.items():
        assert marker in upstream_results[result_name]
        assert marker in provider_results[result_name]

    history_keys = (
        ("search_file_contents", "src/service.py"),
        ("search_source_code", "return helper(amount)"),
        ("search_class", "BillingService"),
        (
            "search_method_in_class",
            "src/service.py::BillingService::calculate_tax",
        ),
        ("search_callable", "src/service.py::helper"),
    )
    for action, search_input in history_keys:
        expected = upstream.get_frame_from_history(action, search_input)
        actual = provider.get_frame_from_history(action, search_input)
        assert expected is not None
        assert actual is not None
        assert actual["file_path"] == expected["file_path"]
        assert actual["query_type"] == expected["query_type"]
        assert actual["search_query"] == expected["search_query"]

    for file_path in ("src/service.py", "src/contract.py"):
        assert provider._direct_get_file_skeleton(
            file_path
        ) == upstream._direct_get_file_skeleton(file_path)
        assert provider._get_file_functions(file_path) == upstream._get_file_functions(
            file_path
        )
    for class_name in (
        "src/service.py::BillingService",
        "src/contract.py::Writer",
    ):
        assert provider._direct_get_class(class_name) == upstream._direct_get_class(
            class_name
        )
        assert provider._get_class_methods(class_name) == upstream._get_class_methods(
            class_name
        )


def test_search_worker_decomposition_and_final_location(
    upstream_modules: SimpleNamespace,
    integration_manifest: Path,
) -> None:
    provider = OrcaLocaSearchProvider.from_manifest(integration_manifest)
    class_content = provider.search_class("BillingService")
    SearchResult = upstream_modules.types.SearchResult
    SearchQueue = upstream_modules.types.SearchQueue
    SearchWorker = upstream_modules.search_agent.SearchWorker

    class_result = SearchResult(
        search_action="search_class",
        search_action_input={"class_name": "BillingService"},
        search_content=class_content,
    )
    worker = object.__new__(SearchWorker)
    worker._search_manager = provider
    worker._llm = object()
    worker._problem_statement = "Tax calculation is wrong."
    worker._config_dict = {"score_threshold": 0.0, "top_k_methods": 2}
    task = SimpleNamespace(extra_state={"token_cnts": []})

    scorer = SimpleNamespace(
        score_batch=lambda messages: [1.0 for _ in messages],
        get_sum_cnt=lambda: SimpleNamespace(in_token_cnt=0, out_token_cnt=0),
    )

    def scorer_factory(**_kwargs):
        return scorer

    with patch.object(
        upstream_modules.search_agent,
        "CodeScorer",
        scorer_factory,
    ):
        steps = worker._class_methods_ranking(class_result, task)

    assert len(steps) == 1
    assert steps[0].search_action == "search_method_in_class"
    assert steps[0].search_action_input == {
        "class_name": "BillingService",
        "method_name": "calculate_tax",
        "file_path": "src/service.py",
    }

    queue = SearchQueue({"enable": True, "basic": 1})
    queue.append_with_priority(steps[0], 2)
    assert queue.pop() == steps[0]

    method_content = provider.search_method_in_class(
        "BillingService",
        "calculate_tax",
        "src/service.py",
    )
    method_result = SearchResult(
        search_action="search_method_in_class",
        search_action_input=steps[0].search_action_input,
        search_content=method_content,
    )
    assert worker._decode_bug_location(method_result) == {
        "file_path": "src/service.py",
        "class_name": "BillingService",
        "method_name": "calculate_tax",
    }


def test_search_agent_injects_provider_without_upstream_build(
    upstream_modules: SimpleNamespace,
    integration_manifest: Path,
) -> None:
    module = upstream_modules.search_agent
    manifest = RepoManifest.load(integration_manifest)
    factory = make_orcaloca_search_manager_factory(integration_manifest)
    llm = SimpleNamespace(callback_manager=None)

    with (
        patch.object(module, "SearchManager", side_effect=AssertionError),
        patch.object(module.SearchAgent, "_setup_tools", return_value=[]),
        patch.object(module.SearchWorker, "from_tools", return_value=object()),
        patch.object(module.AgentRunner, "__init__", return_value=None),
    ):
        agent = module.SearchAgent(
            llm=llm,
            repo_path=manifest.repo_path,
            search_manager_factory=factory,
        )

    assert isinstance(agent._search_manager, OrcaLocaSearchProvider)
    assert agent._search_manager.repo_path == str(Path(manifest.repo_path).resolve())
    assert not (Path(manifest.repo_path) / "_index_data").exists()
