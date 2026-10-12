# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Real local searches with typed fake decisions; never makes billed API calls."""

import json
from collections import Counter
from types import SimpleNamespace

import pytest

from codenib.llm.decisions import ChoiceQuestion, DecisionResult, OpenRouterDecisions
from codenib.types import NODE_TYPE_FUNCTION, NodeInfo
from scripts.benchmark_decision_loop import (
    BudgetReached,
    DecisionLoop,
    LoopPolicy,
    Spend,
    TracedDecisions,
    select_cases,
)


def node(file, content, start=0, score=0.4):
    return NodeInfo(
        node_id=f"{file}:{start}",
        node_name=f"{file}:{start}",
        type=NODE_TYPE_FUNCTION,
        file=file,
        start_line=start,
        end_line=start + len(content.splitlines()) - 1,
        content=content,
        score=score,
    )


class Decisions:
    model = "fake"

    def __init__(self, choices, *, fail_scores=False):
        self.choices = iter(choices)
        self.calls = []
        self.fail_scores = fail_scores

    def decide(self, *, state, questions):
        answers = {}
        if "next_action" in questions or "continue_search" in questions:
            wanted = next(self.choices)
            choice = (
                wanted
                if isinstance(wanted, str)
                else next(
                    name
                    for name, item in state["actions"].items()
                    if wanted(item["action"])
                )
            )
            if "continue_search" in questions:
                answers["continue_search"] = {
                    "type": "noul",
                    "noul": float(wanted != "stop"),
                }
                if choice == "stop":
                    choice = next(iter(state["actions"]))
            if "next_action" in questions:
                answers["next_action"] = {
                    "type": "choice",
                    "choice": choice,
                    "confidence": 1.0,
                    "probabilities": {
                        k: float(k == choice) for k in questions["next_action"].criteria
                    },
                }
        elif self.fail_scores:
            self.calls.append(
                {
                    "question_count": len(questions),
                    "error": "scoring failed",
                    "usage": {"cost": 0.0001},
                }
            )
            raise RuntimeError("scoring failed")
        else:
            for name, question in questions.items():
                answers[name] = {
                    "type": "score",
                    "score": 3.0,
                    "confidence": 1.0,
                    "probabilities": {str(i): float(i == 3) for i in range(4)},
                    "legend": {
                        str(i): criterion
                        for i, criterion in enumerate(question.criteria)
                    },
                }
        result = DecisionResult.model_validate(
            {"answers": answers, "model": "fake", "usage": {"cost": 0.0001}}
        )
        self.calls.append(
            {
                "question_count": len(questions),
                "usage": result.usage,
                "request": json.loads(json.dumps(state)),
            }
        )
        return result


def follow(symbol):
    return lambda a: a["kind"] == "grep" and a["pattern"] == rf"\b{symbol}\b"


def make_loop(tmp_path, nodes, decisions, **kwargs):
    files = {}
    for n in nodes:
        lines = files.setdefault(n.file, [])
        lines.extend(["\n"] * max(0, n.end_line + 1 - len(lines)))
        lines[n.start_line : n.end_line + 1] = n.content.splitlines(keepends=True)
    for file, lines in files.items():
        path = tmp_path / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(lines))
    return DecisionLoop(
        root=tmp_path,
        nodes=nodes,
        query="Repair payload validation",
        seed_ranked=kwargs.pop("seed_ranked", nodes[:1]),
        seed_actions=[],
        decisions=decisions,
        **kwargs,
    )


def test_two_observed_symbols_reach_new_code_and_final_context(tmp_path):
    nodes = [
        node("entry.py", "def entry():\n    return parse_payload()\n"),
        node("parser.py", "def parse_payload():\n    return validate_signature()\n"),
        node(
            "validation.py",
            "def validate_signature():\n    raise ValueError('invalid')\n",
        ),
        node("unrelated.py", "UNSEEN_SECRET = 'not part of the issue'\n"),
    ]
    decisions = Decisions(
        [follow("parse_payload"), follow("validate_signature"), "stop"]
    )
    result = make_loop(tmp_path, nodes, decisions).run()
    assert result["status"] == "ok" and result["stop_reason"] == "model_stop"
    assert set(result["pool"]) == {0, 1, 2}
    assert {n["file"] for n in json.loads(result["context"])} == {
        n.file for n in nodes[:3]
    }
    assert result["tokens"] <= 4096
    scored = [
        c["request"]["candidates"]
        for c in decisions.calls
        if "candidates" in c["request"]
    ]
    assert Counter(n["file"] for batch in scored for n in batch.values()) == {
        "parser.py": 1,
        "validation.py": 1,
    }
    assert "UNSEEN_SECRET" not in json.dumps(decisions.calls)
    assert result["trace"][0]["novel_candidates"] == [1]
    assert result["trace"][1]["novel_candidates"] == [2]


def test_adjacent_read_retains_exact_source_line_numbers(tmp_path):
    nodes = [node("code.py", "SETTING = 1\n"), node("code.py", "LIMIT = 12\n", start=2)]
    decisions = Decisions([lambda a: a["kind"] == "read", "stop"])
    result = make_loop(tmp_path, nodes, decisions).run()
    assert result["status"] == "ok"
    assert "3 | LIMIT = 12" in result["context"]
    assert {0, 1} == set(result["pool"])


def test_feedback_recovers_empty_search_and_receives_actual_observations(tmp_path):
    nodes = [node("target.py", "def recover_payload():\n    return 1\n")]
    feedback_inputs = []

    def feedback(state):
        feedback_inputs.append(state)
        return {
            "actions": [
                {"pattern": "recover_payload", "glob": "**/*", "case_sensitive": True}
            ]
        }

    decisions = Decisions(["replan", "stop"])
    result = make_loop(
        tmp_path,
        nodes,
        decisions,
        seed_ranked=[],
        feedback=feedback,
        policy=LoopPolicy(max_replans=1),
    ).run()
    assert result["status"] == "ok" and result["replans"] == 1
    assert feedback_inputs[0]["context"] == []
    assert feedback_inputs[0]["issue"] == "Repair payload validation"
    assert "recover_payload" in result["context"]
    later = [c["request"] for c in decisions.calls if "actions" in c["request"]][1]
    assert "replan" not in later["actions"]


def test_no_novel_hits_does_not_rescore_or_repeat_the_action(tmp_path):
    nodes = [node("code.py", "def entry():\n    return missing_handler()\n")]
    decisions = Decisions([follow("missing_handler"), "stop"])
    loop = make_loop(tmp_path, nodes, decisions)
    original = loop.packed()
    result = loop.run()
    assert (result["context"], result["tokens"]) == original
    assert all("candidates" not in c["request"] for c in decisions.calls)
    assert result["trace"][0]["novel_candidates"] == []
    assert all(
        a["action"].get("pattern") != r"\bmissing_handler\b"
        for a in decisions.calls[1]["request"]["actions"].values()
    )


@pytest.mark.parametrize(
    "choices,fail_scores",
    [(["stop"], False), (["outside_menu"], False), ([follow("parse_payload")], True)],
)
def test_stop_or_failed_decisions_keep_the_last_complete_context(
    tmp_path, choices, fail_scores
):
    nodes = [
        node("entry.py", "def entry():\n    return parse_payload()\n"),
        node("parser.py", "def parse_payload():\n    return 1\n"),
    ]
    loop = make_loop(tmp_path, nodes, Decisions(choices, fail_scores=fail_scores))
    original = loop.packed()
    result = loop.run()
    assert (result["context"], result["tokens"]) == original
    assert result["pool"] == [0]
    assert result["status"] == ("ok" if choices == ["stop"] else "degraded")


def test_full_candidate_pool_stops_before_api_and_never_evicts_seeds(tmp_path):
    nodes = [
        node(f"{i}.py", f"def handler_{i}():\n    return {i}\n") for i in range(40)
    ]
    decisions = Decisions([])
    result = make_loop(
        tmp_path,
        nodes,
        decisions,
        seed_ranked=nodes,
        policy=LoopPolicy(candidate_limit=40),
    ).run()
    assert result["stop_reason"] == "candidate_limit"
    assert result["pool"] == list(range(40)) and decisions.calls == []


def test_added_candidate_cap_keeps_seed_evidence(tmp_path, monkeypatch):
    from scripts import benchmark_decision_loop as experiment

    nodes = [
        node(f"{i}.py", f"def handler_{i}():\n    return target()\n") for i in range(50)
    ]
    monkeypatch.setattr(experiment.grep, "execute", lambda *_: (nodes[39:], []))
    result = make_loop(
        tmp_path,
        nodes,
        Decisions([follow("target")]),
        seed_ranked=nodes[:39],
        policy=LoopPolicy(candidate_limit=40),
    ).run()
    assert result["stop_reason"] == "candidate_limit"
    assert set(result["pool"]) == set(range(40))
    assert result["trace"][0]["novel_candidates"] == [39]


def test_disabled_loop_reproduces_seed_context_without_any_calls(tmp_path):
    decisions = Decisions([])
    loop = make_loop(
        tmp_path,
        [node("a.py", "def entry():\n    return 1\n")],
        decisions,
        policy=LoopPolicy(max_steps=0),
    )
    expected = loop.packed()
    result = loop.run()
    assert (result["context"], result["tokens"]) == expected
    assert decisions.calls == []


def test_seed_content_drift_is_rejected_before_calls(tmp_path):
    n = node("a.py", "def entry():\n    return 1\n")
    with pytest.raises(ValueError, match="differs from frozen source"):
        make_loop(
            tmp_path,
            [n],
            Decisions([]),
            seed_ranked=[n.model_copy(update={"content": "tampered"})],
        )


def test_selection_is_balanced_deterministic_and_independent_of_scores():
    cases = [
        SimpleNamespace(
            language=lang,
            repo=f"repo-{i % 3}",
            instance_id=f"{lang}-{i}",
            gold_context=object(),
            score=i,
        )
        for lang in ("python", "rust")
        for i in range(7)
    ]
    first = select_cases(cases, 3, "seed")
    second = select_cases(list(reversed(cases)), 3, "seed")
    assert [c.instance_id for c in first] == [c.instance_id for c in second]
    assert Counter(c.language for c in first) == {"python": 3, "rust": 3}
    assert len({(c.language, c.repo) for c in first}) == 6


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -1, 0, True])
def test_invalid_spending_limits_are_rejected(amount):
    with pytest.raises(ValueError, match="positive and finite"):
        Spend(amount, 0.1)


def test_traced_decision_records_inputs_cost_and_blocks_next_call(monkeypatch):
    spend = Spend(1, 0.01)
    calls, checkpoints = [], []

    def decide(_self, *, state, questions):
        calls.append(state)
        return DecisionResult.model_validate(
            {
                "model": "fake",
                "usage": {"cost": 0.02},
                "answers": {
                    "pick": {
                        "type": "choice",
                        "choice": "stop",
                        "confidence": 1.0,
                        "probabilities": {"stop": 1.0, "go": 0.0},
                    }
                },
            }
        )

    monkeypatch.setattr(OpenRouterDecisions, "decide", decide)
    client = TracedDecisions(spend, lambda: checkpoints.append(1))
    questions = {
        "pick": ChoiceQuestion(
            instructions="Pick", criteria={"stop": "Stop", "go": "Go"}
        )
    }
    client.decide(state={"code": "observed"}, questions=questions)
    assert spend.total == 0.02 and len(checkpoints) == 1
    assert client.calls[0]["request"]["state"] == {"code": "observed"}
    with pytest.raises(BudgetReached):
        client.decide(state={}, questions=questions)
    assert len(calls) == 1


def test_unpriced_api_failure_stops_following_cases(monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("transport failed")

    monkeypatch.setattr(OpenRouterDecisions, "decide", fail)
    spend = Spend(1, 0.1)
    client = TracedDecisions(spend, lambda: None)
    questions = {
        "pick": ChoiceQuestion(
            instructions="Pick", criteria={"stop": "Stop", "go": "Go"}
        )
    }
    with pytest.raises(RuntimeError, match="transport failed"):
        client.decide(state={}, questions=questions)
    spend.case_start = spend.total
    with pytest.raises(RuntimeError, match="unpriced"):
        spend.check()
    assert client.calls[0]["request"]["questions"]["pick"]["type"] == "choice"


def test_mid_step_cost_guard_delivers_seed_without_scoring_new_code(tmp_path):
    spend = Spend(1, 0.01)

    class LimitedDecisions(Decisions):
        def decide(self, **kwargs):
            result = super().decide(**kwargs)
            spend.charge({"cost": 0.02})
            return result

    decisions = LimitedDecisions([follow("parse_payload")])
    nodes = [
        node("entry.py", "def entry():\n    return parse_payload()\n"),
        node("parser.py", "def parse_payload():\n    return 1\n"),
    ]
    loop = make_loop(tmp_path, nodes, decisions, spend=spend)
    original = loop.packed()
    result = loop.run()
    assert result["stop_reason"] == "cost_limit"
    assert result["status"] == "ok" and len(decisions.calls) == 1
    assert (result["context"], result["tokens"]) == original
    assert result["pool"] == [0]


def test_billed_run_requires_explicit_cli_optin_before_input_io(monkeypatch):
    from scripts import benchmark_decision_loop as experiment

    monkeypatch.setenv("OPENROUTER_API_KEY", "unused-test-key")
    monkeypatch.setattr(
        experiment, "load_manifest", lambda *_: pytest.fail("Unexpected I/O")
    )
    with pytest.raises(ValueError, match="require --live"):
        experiment.run(SimpleNamespace(max_cost_usd=1, case_cost_usd=0.1, live=False))


def test_frozen_source_drift_is_rejected_before_loading_tasks(tmp_path, monkeypatch):
    from scripts import benchmark_decision_loop as experiment

    selected = tmp_path / "selected.jsonl"
    selected.write_text("not read until provenance passes")
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "config.json").write_text("{}")
    source = tmp_path / "source.json"
    source.write_text("original")
    manifest = {
        "selected_sha256": experiment.digest(selected),
        "implementation": {},
        "baseline": str(baseline),
        "baseline_config_sha256": experiment.digest(baseline / "config.json"),
        "cases": [{"source": str(source), "source_sha256": experiment.digest(source)}],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    source.write_text("changed")
    monkeypatch.setattr(
        experiment,
        "load_contextbench_cases",
        lambda *_: pytest.fail("Unexpected task load"),
    )
    with pytest.raises(ValueError, match="Frozen source changed"):
        experiment.load_manifest(tmp_path, frozen_code=False)


def test_binary_stop_has_its_own_question_and_never_competes_with_actions(tmp_path):
    class InspectDecisions(Decisions):
        def decide(self, *, state, questions):
            if "continue_search" in questions:
                assert questions["continue_search"].type == "noul"
                assert "stop" not in state["actions"]
                assert "stop" not in questions["next_action"].criteria
            return super().decide(state=state, questions=questions)

    nodes = [node("entry.py", "def entry():\n    return parse_payload()\n")]
    result = make_loop(tmp_path, nodes, InspectDecisions(["stop"])).run()
    assert result["stop_reason"] == "model_stop"
    assert result["trace"][0]["continue_probability"] == 0


def test_quality_control_searches_twice_before_honoring_model_stop(tmp_path):
    nodes = [
        node("entry.py", "def entry():\n    return 1\n"),
        node("parser.py", "def parse_payload():\n    return 2\n"),
        node("validation.py", "def validate_payload():\n    return 3\n"),
    ]
    inputs = []
    patterns = iter(["parse_payload", "validate_payload"])

    def feedback(state):
        inputs.append(state)
        return {
            "actions": [
                {"pattern": next(patterns), "glob": "**/*", "case_sensitive": True}
            ]
        }

    result = make_loop(
        tmp_path,
        nodes,
        Decisions(["stop", "stop", "stop"]),
        feedback=feedback,
        policy=LoopPolicy(min_steps=2, max_replans=6, action_mode="feedback"),
    ).run()
    assert result["stop_reason"] == "model_stop"
    assert result["replans"] == 2 and len(inputs) == 2
    assert result["trace"][0]["stop_overridden_for_exploration"]
    assert result["trace"][1]["stop_overridden_for_exploration"]
    assert {0, 1, 2} == set(result["pool"])
    assert "parse_payload" in json.dumps(inputs[1]["context"])
    assert "context_after" not in json.dumps(inputs[1]["history"])


def test_flat_menu_control_retains_legacy_stop_behavior(tmp_path):
    result = make_loop(
        tmp_path,
        [node("a.py", "def entry():\n    return 1\n")],
        Decisions(["stop"]),
        policy=LoopPolicy(stop_rule="flat"),
    ).run()
    assert result["stop_reason"] == "model_stop"
    assert "continue_probability" not in result["trace"][0]
