# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Search handoff, physical delivery and complete-run fees without providers."""

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from codenib.source_fingerprint import fingerprint_repository
from scripts import benchmark_context_agent as native
from scripts.benchmark_context_solver import (
    _HISTORY_PREFIX,
    model_observations,
    request_digest,
    run,
)
from scripts.benchmark_jev_base import digest
from scripts.contextbench_search_pair import assemble_workers, scout_query
from scripts.contextbench_search_pair_solver import (
    joined_native_result,
    load_pair_handoff,
    navigation_delivery,
    navigation_template,
    pair_budget,
    whole_run_usage,
)
from scripts.contextbench_solver_sources import initial_source_receipt


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_pair_run_requires_whole_run_budget_before_opening_inputs(tmp_path):
    from types import SimpleNamespace

    with pytest.raises(ValueError, match="whole-run metering budget"):
        run(
            SimpleNamespace(
                live=True, search_pair=tmp_path, initial_context="retrieved"
            )
        )


@pytest.fixture
def pair(tmp_path):
    repo, root = tmp_path / "repo", tmp_path / "search"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name, value in [("gc.auto", "0"), ("maintenance.auto", "false")]:
        subprocess.run(["git", "-C", str(repo), "config", name, value], check=True)
    body = ["def answer():", "    return 41"]
    (repo / "a.py").write_text("\n".join(body) + "\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.org",
            "commit",
            "-qm",
            "Base",
        ],
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    fingerprint = fingerprint_repository(repo).value
    key = "SWE-Bench-Verified__python__maintenance__bugfix__fixture"
    issue = "Return the documented answer."
    input_path = root / "inputs" / (key + ".json")
    save(
        input_path,
        {
            "instance_id": key,
            "base_commit": commit,
            "repo": str(repo),
            "problem_statement": issue,
            "language": "python",
        },
    )
    case = {
        "instance_id": key,
        "original_inst_id": "owner__project-1",
        "base_commit": commit,
        "repo": "owner/project",
        "repo_url": "https://github.com/owner/project",
        "language": "python",
        "input": str(input_path),
        "input_sha256": digest(input_path),
    }
    folder = root / "live" / key
    workers = {}
    for index, label in enumerate(["first", "scout"], 1):
        prompt = (
            issue
            if label == "first"
            else scout_query(
                issue,
                workers["first"]["prediction"],
                commit=commit,
                source_fingerprint=fingerprint,
            )
        )
        messages = [
            {"role": "user", "content": prompt},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": label,
                        "function": {
                            "name": "read",
                            "arguments": {"file_path": str(repo / "a.py")},
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": label,
                "content": f"{index}|{body[index - 1]}\n",
            },
            {"role": "assistant", "content": "Source acknowledged"},
        ]
        prediction = native.contextbench_prediction(
            instance_id=key, messages=messages, repo_path=repo
        )
        fee = index / 100
        raw_path = folder / label / "trajectory.json"
        save(
            raw_path,
            {
                "messages": messages,
                "accepted": True,
                "source_context": {"source_fingerprint": fingerprint},
                "sdk_reported_cost_usd": fee,
                "sdk_result": {"session_id": label, "total_cost_usd": fee},
            },
        )
        save(
            raw_path.parent / "model-intent.json",
            {"prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()},
        )
        workers[label] = {
            "label": label,
            "commit": commit,
            "source_fingerprint": fingerprint,
            "accepted": True,
            "prediction": prediction,
            "all_model_usage_usd": fee,
            "raw": str(raw_path),
            "raw_sha256": digest(raw_path),
        }
    prediction, receipt = assemble_workers(key, list(workers.values()))
    save(
        folder / "case-terminal.json",
        {"instance_id": key, "all2_accepted": True, "workers": workers},
    )
    save(folder / "haiku_scout_pair-prediction.json", prediction)
    save(folder / "haiku_scout_pair-receipt.json", receipt)
    save(
        root / "protocol.json",
        {
            "cases": [case],
            "navigation_token_budget": 2048,
            "implementation": {
                "scripts/contextbench_search_pair.py": digest(
                    Path(native.__file__).with_name("contextbench_search_pair.py")
                )
            },
            "source_fingerprints": {key: fingerprint},
        },
    )
    save(
        folder / "case-intent.json", {"protocol_sha256": digest(root / "protocol.json")}
    )
    return root, folder, repo, key


def test_pair_handoff_replays_originals_and_sends_locations_without_bodies(pair):
    root, folder, repo, key = pair
    originals = {p: digest(p) for p in folder.rglob("*.json")}
    result = load_pair_handoff(root, key)
    navigation = json.loads(result.navigation)
    assert result.case.problem_statement == "Return the documented answer."
    assert result.case.gold_context == () and result.case.source == "Verified"
    assert result.repo == repo and result.source_tree
    assert navigation["entries"][0]["lines"] == [1, 2]
    assert navigation["source_bodies_included"] is False
    assert "return 41" not in result.navigation
    assert result.receipt["source_footprints_reconstructed"]
    assert result.receipt["source_bodies_delivered_by_handoff"] == 0
    assert originals == {p: digest(p) for p in originals}


def test_solver_cannot_write_artifacts_into_the_frozen_search_source(pair, monkeypatch):
    from types import SimpleNamespace

    from scripts import benchmark_context_solver as solver

    root, _, repo, key = pair
    handoff = load_pair_handoff(root, key)
    monkeypatch.setattr(solver, "load_pair_handoff", lambda *args: handoff)
    monkeypatch.setattr(solver.shutil, "which", lambda name: "fixture-cli")
    monkeypatch.setattr(
        solver.subprocess,
        "check_output",
        lambda *a, **kw: solver.CONTEXTBENCH_UPSTREAM_REVISION,
    )
    output = repo / "solver-output"
    with pytest.raises(ValueError, match="outside the pinned source"):
        run(
            SimpleNamespace(
                live=True,
                search_pair=root,
                instance_id=key,
                max_turns=1,
                max_meter_usd=0.5,
                total_meter_usd=0.5,
                image="sha256:fixture",
                upstream=root,
                output=output,
            )
        )
    assert not output.exists()


@pytest.mark.parametrize("mutation", ["raw", "source", "union", "session", "fee"])
def test_pair_handoff_rejects_unreliable_source_or_usage(pair, mutation):
    root, folder, repo, key = pair
    if mutation == "source":
        (repo / "a.py").write_text("changed source\n")
    elif mutation == "union":
        path = folder / "haiku_scout_pair-prediction.json"
        value = json.loads(path.read_text())
        value["traj_data"]["pred_spans"]["a.py"][0]["end"] += 1
        save(path, value)
    else:
        path = folder / "scout/trajectory.json"
        raw = json.loads(path.read_text())
        if mutation == "raw":
            raw["messages"][-1]["content"] += "altered"
        elif mutation == "session":
            raw["sdk_result"]["session_id"] = "first"
        else:
            raw["sdk_reported_cost_usd"] = None
            raw["sdk_result"]["total_cost_usd"] = None
        save(path, raw)
        if mutation != "raw":
            terminal_path = folder / "case-terminal.json"
            terminal = json.loads(terminal_path.read_text())
            terminal["workers"]["scout"]["raw_sha256"] = digest(path)
            save(terminal_path, terminal)
    with pytest.raises((ValueError, AssertionError)):
        load_pair_handoff(root, key)


def repair_row(key):
    return {
        "instance_id": key,
        "status": "error",
        "native_resolution": None,
        "calls": [
            {
                "turn": index,
                "sdk_reported_cost_usd": index / 10,
                "events": [
                    {
                        "kind": "ResultMessage",
                        "data": {
                            "session_id": f"repair-{index}",
                            "total_cost_usd": index / 10,
                            "is_error": index == 2,
                        },
                    }
                ],
            }
            for index in [1, 2]
        ],
    }


def test_whole_run_budget_and_failed_repair_cost_include_search_once(pair):
    root, _, _, key = pair
    handoff = load_pair_handoff(root, key)
    budget = pair_budget(0.4, handoff.receipt["search_sessions"])
    assert budget["search_usage_usd"] == pytest.approx(0.03)
    assert budget["repair_meter_usd"] == pytest.approx(0.37)
    report = whole_run_usage(handoff.receipt, repair_row(key))
    assert report["total_model_usage_usd"] == pytest.approx(0.33)
    assert len(report["sessions"]) == 4 and report["solver_status"] == "error"
    assert report["native_resolution"] is None
    assert not report["official_context_metrics_complete"]
    control = whole_run_usage(handoff.receipt, repair_row(key), False)
    assert control["total_model_usage_usd"] == pytest.approx(0.3)
    assert not control["search_included"]
    for total in [None, 0, 0.03, float("nan")]:
        with pytest.raises(ValueError):
            pair_budget(total, handoff.receipt["search_sessions"])


def test_unknown_repair_usage_stays_unknown_and_duplicate_sessions_are_rejected(pair):
    root, _, _, key = pair
    handoff = load_pair_handoff(root, key)
    row = repair_row(key)
    del row["calls"][1]["sdk_reported_cost_usd"]
    report = whole_run_usage(handoff.receipt, row)
    assert report["total_model_usage_usd"] is None
    assert report["known_only_usage_usd"] == pytest.approx(0.13)
    duplicated = repair_row(key)
    duplicated["calls"][1]["events"][0]["data"]["session_id"] = "first"
    with pytest.raises(ValueError, match="more than once"):
        whole_run_usage(handoff.receipt, duplicated)
    other = copy.deepcopy(row)
    other["instance_id"] = "other"
    with pytest.raises(ValueError, match="different tasks"):
        whole_run_usage(handoff.receipt, other)
    wrong_total = repair_row(key)
    wrong_total["sdk_reported_cost_usd"] = 1.0
    with pytest.raises(ValueError, match="individual SDK receipts"):
        whole_run_usage(handoff.receipt, wrong_total)


def test_navigation_confirmation_is_separate_from_source_delivery(pair):
    root, _, repo, key = pair
    handoff = load_pair_handoff(root, key)
    from jinja2 import Template

    template = navigation_template(
        "<retrieved_source>\n{{retrieved_source}}\n</retrieved_source>\n"
    )
    content = Template(template).render(
        retrieved_source="[]", context_navigation=handoff.navigation
    )
    request = {
        "system_prompt": "Repair using source tools",
        "prompt": _HISTORY_PREFIX + json.dumps([{"role": "user", "content": content}]),
    }
    row = {
        "instance_id": key,
        "calls": [
            {
                "turn": 1,
                "request": request,
                "request_sha256": request_digest(request),
                "input_delivery_confirmed": True,
            }
        ],
        "initial_source_receipt": initial_source_receipt(
            "[]", repo, handoff.case.base_commit
        ),
    }
    observations = model_observations(row)
    receipt = navigation_delivery(handoff.navigation, observations)
    assert receipt["confirmed_turns"] == [1] and receipt["source_body_credit"] == 0
    assert observations["source_attribution"]["base_source_line_count"] == 0
    row["calls"][0]["input_delivery_confirmed"] = False
    assert (
        navigation_delivery(handoff.navigation, model_observations(row))[
            "confirmed_turns"
        ]
        == []
    )


@pytest.mark.parametrize("mismatch", [None, "solver", "task"])
def test_native_outcome_join_binds_patch_task_and_whole_run_cost(
    pair, tmp_path, mismatch
):
    root, _, _, key = pair
    handoff = load_pair_handoff(root, key)
    solver_run, native_run = tmp_path / "solver", tmp_path / "native"
    row = repair_row(key)
    row.update(
        original_inst_id=handoff.case.original_inst_id, model_patch="fixture patch"
    )
    save(
        solver_run / "config.json",
        {
            "retrieval_kind": "search_pair_navigation",
            "source_commit": handoff.case.base_commit,
            "initial_context": "retrieved",
        },
    )
    save(solver_run / "solver.json", row)
    save(solver_run / "search-pair-handoff.json", handoff.receipt)
    save(
        solver_run / "model-observations.json",
        {"source_attribution": {"status": "partial"}},
    )
    save(
        solver_run / "predictions.json",
        [{"instance_id": row["original_inst_id"], "model_patch": row["model_patch"]}],
    )
    save(solver_run / "whole-run-usage.json", whole_run_usage(handoff.receipt, row))
    save(
        native_run / "config.json",
        {
            "solver_sha256": (
                "wrong" if mismatch == "solver" else digest(solver_run / "solver.json")
            ),
            "prediction_sha256": digest(solver_run / "predictions.json"),
        },
    )
    save(
        native_run / "resolution.json",
        {
            "instance_id": "other" if mismatch == "task" else key,
            "status": "graded",
            "resolved": True,
        },
    )
    if mismatch:
        with pytest.raises(ValueError, match="another solver|identity or source"):
            joined_native_result(solver_run, native_run)
        return
    result = joined_native_result(solver_run, native_run)
    assert result["native_resolution"] is True
    assert result["usage"]["total_model_usage_usd"] == pytest.approx(0.33)
    assert not result["official_context_metrics_complete"]


@pytest.mark.integration
def test_pinned_mini_loop_reads_navigation_on_demand_and_records_edits(
    pair, monkeypatch, tmp_path
):
    """Real mini loop/local shell, scripted SDK; not benchmark repair quality."""
    import dataclasses
    import os
    import shlex
    import sys
    from types import SimpleNamespace

    import yaml

    from scripts.benchmark_context_solver import (
        CONTEXTBENCH_UPSTREAM_REVISION,
        SubscriptionModel,
    )
    from scripts.contextbench_solver_sources import SourceEnvironment

    checkout = os.environ.get("CONTEXTBENCH_CHECKOUT")
    if not checkout:
        pytest.skip("Set CONTEXTBENCH_CHECKOUT to the pinned external checkout")
    upstream = Path(checkout)
    assert (
        subprocess.check_output(
            ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
        ).strip()
        == CONTEXTBENCH_UPSTREAM_REVISION
    )
    mini = (
        upstream
        / "agent-frameworks/mini-swe-agent/multi-poly-pro-verified/mini-swe-agent/src"
    )
    monkeypatch.syspath_prepend(str(mini))
    from minisweagent.agents.default import DefaultAgent

    root, _, repo, key = pair
    handoff = load_pair_handoff(root, key)
    changed = "def answer():\n    return 42\npost_edit_only = 99\n"
    commands = [
        "codenib-read a.py 1 2",
        shlex.join(
            [
                "python3",
                "-c",
                "from pathlib import Path; Path('a.py').write_text("
                + repr(changed)
                + ")",
            ]
        ),
        "codenib-read a.py 1 3",
        shlex.join(["python3", "-c", "import a; assert a.answer() == 42"]),
        "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\nfixture patch\\n'",
    ]
    requests = []
    Assistant = dataclasses.make_dataclass("AssistantMessage", ["content", "model"])
    Result = dataclasses.make_dataclass(
        "ResultMessage", ["total_cost_usd", "is_error", "subtype", "session_id"]
    )

    async def query(**kwargs):
        requests.append(kwargs["prompt"])
        number = len(requests)
        yield dataclasses.make_dataclass("SystemMessage", ["subtype", "data"])(
            "init", {"tools": [], "apiKeySource": "none"}
        )
        yield dataclasses.make_dataclass("RateLimitEvent", ["rate_limit_info"])(
            {
                "status": "allowed",
                "raw": {"overageEnabled": False, "isUsingOverage": False},
            }
        )
        yield Assistant(
            [{"text": "```bash\n" + commands[number - 1] + "\n```"}], "fixture-model"
        )
        yield Result(0.01, False, "success", f"repair-integration-{number}")

    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(ClaudeAgentOptions=lambda **kwargs: kwargs, query=query),
    )
    row = {
        "instance_id": key,
        "calls": [],
        "execution_receipts": [],
        "initial_source_receipt": initial_source_receipt(
            "[]", repo, handoff.case.base_commit
        ),
    }
    model = SubscriptionModel(
        cli="fixture",
        model="fixture-model",
        cwd=tmp_path,
        max_meter_usd=1,
        calls=row["calls"],
        checkpoint=lambda: None,
        max_included_only=True,
    )

    class LocalEnvironment:
        def get_template_vars(self):
            return {}

        def execute(self, command):
            result = subprocess.run(
                command,
                shell=True,
                cwd=repo,
                text=True,
                capture_output=True,
                timeout=20,
            )
            return {
                "returncode": result.returncode,
                "output": result.stdout + result.stderr,
            }

    config = yaml.safe_load(
        (mini / "minisweagent/config/extra/swebench.yaml").read_text()
    )["agent"]
    config.update(
        system_template="Return one bash action.",
        step_limit=10,
        instance_template=navigation_template(
            "<issue>{{task}}</issue>\n<retrieved_source>\n{{retrieved_source}}\n</retrieved_source>\n"
        ),
    )

    def observation(command, output, timeout):
        rendered = agent.render_template(
            agent.config.action_observation_template, output=output
        )
        return len(agent.messages), rendered

    environment = SourceEnvironment(
        LocalEnvironment(),
        root=str(repo),
        commit=handoff.case.base_commit,
        receipts=row["execution_receipts"],
        checkpoint=lambda: None,
        observation=observation,
    )
    agent = DefaultAgent(model, environment, **config)
    status, _ = agent.run(
        handoff.case.problem_statement,
        retrieved_source="[]",
        context_navigation=handoff.navigation,
    )
    assert status == "Submitted" and len(requests) == 5
    row["status"] = status
    observations = model_observations(row)
    assert (
        "return 41" not in json.loads(requests[0][len(_HISTORY_PREFIX) :])[0]["content"]
    )
    assert "return 41" in requests[1] and "post_edit_only = 99" in requests[3]
    source = observations["source_attribution"]
    assert source["base_source_lines"] == {"a.py": [1, 2]}
    reads = [e for e in source["events"] if e["kind"] == "source_read"]
    assert [e["base_version"] for e in reads] == [True, False]
    assert navigation_delivery(handoff.navigation, observations)["confirmed_turns"] == [
        1,
        2,
        3,
        4,
        5,
    ]
    usage = whole_run_usage(handoff.receipt, row)
    assert usage["total_model_usage_usd"] == pytest.approx(0.08)
    assert usage["native_resolution"] is None
