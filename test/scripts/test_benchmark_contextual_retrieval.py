# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from codenib.index.contextual import ContextualizationConfig, FileContextualizer
from scripts import benchmark_contextual_retrieval as pilot
from scripts.benchmark_contextual_retrieval import (
    PilotBudget,
    context_inventory,
    deterministic_context,
    request_key,
    snapshot,
)


def test_reservations_prevent_concurrent_overspending():
    budget = PilotBudget("2")
    first = budget.reserve("1.25")
    with pytest.raises(RuntimeError, match="cap"):
        budget.reserve("0.76")
    budget.finish(first, "0.50")
    second = budget.reserve("1.50")
    assert budget.spent + budget.reserved == Decimal("2")
    budget.finish(second, "0.25")
    assert budget.spent == Decimal("0.75")


def test_unknown_usage_keeps_the_full_reservation():
    budget = PilotBudget("2", "0.50")
    reservation = budget.reserve("1.25")
    budget.finish(reservation)
    assert budget.spent == Decimal("1.75")
    with pytest.raises(RuntimeError, match="cap"):
        budget.reserve("0.26")


def test_usage_cannot_silently_exceed_reserved_cost():
    budget = PilotBudget("2")
    reservation = budget.reserve("0.10")
    with pytest.raises(RuntimeError, match="ceiling"):
        budget.finish(reservation, "0.11")
    assert budget.reserved == Decimal("0.10")


def test_deterministic_context_is_bounded_and_query_independent():
    source = '''"""Payment helpers."""
import decimal

def charge(value):
    return value

def refund(value):
    """Reverse a payment."""
    return value
'''
    index = context_inventory(source)
    prefix = deterministic_context(source, 3, 180, index)
    assert "Payment helpers" in prefix
    assert "import decimal" in prefix
    assert "Reverse a payment" in prefix
    assert len(prefix) <= 180
    assert len(deterministic_context(source, 3, 40, index)) == 40


def test_request_identity_binds_prompt_and_effective_model_options():
    messages = [{"role": "user", "content": "source code only"}]
    first = request_key(messages, {"max_tokens": 128, "temperature": None})
    assert first == request_key(messages, {"temperature": None, "max_tokens": 128})
    assert first != request_key(messages, {"max_tokens": 512, "temperature": None})
    assert first != request_key(
        [{"role": "user", "content": "changed source"}],
        {"max_tokens": 128, "temperature": None},
    )


def test_snapshot_refuses_parent_traversal_before_git_access(tmp_path):
    with pytest.raises(ValueError, match="relative"):
        snapshot(tmp_path, "HEAD", ["../private"], tmp_path / "output")
    assert not (tmp_path / "output").exists()


@pytest.fixture
def generation_fixture(tmp_path, monkeypatch):
    class FakeChat:
        cache_identity = "fixture-client"
        calls = 0
        fail_once = False
        prefix = "A short explanation of source code."

        def complete(self, *args, **kwargs):
            raise AssertionError("generation should retain the raw usage response")

        def _call_raw(self, messages, **kwargs):
            self.calls += 1
            assert "GOLD_QUERY_SENTINEL" not in json.dumps(messages)
            assert kwargs["num_retries"] == 0
            if self.fail_once and self.calls == 1:
                raise TimeoutError("fixture timeout")
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=self.prefix),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(
                    model_dump=lambda: {"prompt_tokens": 11, "completion_tokens": 7}
                ),
                model="fixture-model",
                _hidden_params={},
            )

    chat = FakeChat()
    monkeypatch.setattr(pilot, "client", lambda *_args, **_kwargs: chat)
    config = {
        "model": "fixture-model",
        "model_options": {},
        "context": {"temperature": None},
        "budget": {
            "max_usd": "2",
            "cache_write_per_million": "0.20",
            "output_per_million": "0.50",
            "initial_spent_ceiling_usd": "0.25",
        },
    }
    messages = [{"role": "user", "content": "function source only"}]
    options = {"max_tokens": 128, "temperature": None}
    manifest = {
        "config_sha256": pilot.digest(json.dumps(config, sort_keys=True)),
        "invalid_targets": [],
        "prefix_policy": FileContextualizer(
            chat, ContextualizationConfig(temperature=None)
        ).artifact_identity(),
    }
    (tmp_path / "config.json").write_text(json.dumps(config))
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "cases.json").write_text("GOLD_QUERY_SENTINEL")
    (tmp_path / "requests.jsonl").write_text(
        json.dumps(
            {
                "key": request_key(messages, options),
                "messages": messages,
                "options": options,
                "node_id": "source.py:run()",
            }
        )
        + "\n"
    )
    credential = tmp_path / "private-key"
    manifest["requests_sha256"] = pilot.digest(
        (tmp_path / "requests.jsonl").read_text()
    )
    manifest["cases_sha256"] = pilot.digest((tmp_path / "cases.json").read_text())
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    credential.write_text("private-fixture-key")
    args = SimpleNamespace(
        study=tmp_path, credential_file=credential, limit=None, workers=1
    )
    return args, chat, config


def test_generation_resume_preserves_cost_and_does_not_repeat_responses(
    generation_fixture,
):
    args, chat, _ = generation_fixture
    pilot.generate(args)
    first = json.loads((args.study / "generation-summary.json").read_text())
    pilot.generate(args)
    second = json.loads((args.study / "generation-summary.json").read_text())
    assert chat.calls == 1
    assert (
        first["conservative_spend_ceiling_usd"]
        == second["conservative_spend_ceiling_usd"]
    )
    assert Decimal(first["conservative_spend_ceiling_usd"]) > Decimal("0.25")
    assert "private-fixture-key" not in (args.study / "responses.jsonl").read_text()
    assert "private-fixture-key" not in (args.study / "cost-ledger.jsonl").read_text()


def test_transient_retry_keeps_failed_attempt_cost(generation_fixture):
    args, chat, _ = generation_fixture
    chat.fail_once = True
    pilot.generate(args)
    first = json.loads((args.study / "generation-summary.json").read_text())
    pilot.generate(args)
    second = json.loads((args.study / "generation-summary.json").read_text())
    assert chat.calls == 2
    assert (
        second["errors"] == 1 and second["responses"] == 1 and second["attempts"] == 2
    )
    assert Decimal(second["conservative_spend_ceiling_usd"]) > Decimal(
        first["conservative_spend_ceiling_usd"]
    )


def test_invalid_text_is_frozen_instead_of_regenerated_until_favorable(
    generation_fixture,
):
    args, chat, _ = generation_fixture
    chat.prefix = "x" * 2048
    pilot.generate(args)
    pilot.generate(args)
    assert chat.calls == 1
    summary = json.loads((args.study / "generation-summary.json").read_text())
    assert summary["invalid_prefixes"] == 1


def test_insufficient_remaining_budget_prevents_a_paid_call(generation_fixture):
    args, chat, config = generation_fixture
    config["budget"]["max_usd"] = "0.25000001"
    (args.study / "config.json").write_text(json.dumps(config))
    manifest = json.loads((args.study / "manifest.json").read_text())
    manifest["config_sha256"] = pilot.digest(json.dumps(config, sort_keys=True))
    (args.study / "manifest.json").write_text(json.dumps(manifest))
    pilot.generate(args)
    assert chat.calls == 0
    summary = json.loads((args.study / "generation-summary.json").read_text())
    assert summary["conservative_spend_ceiling_usd"] == "0.25"


def test_changed_prompt_records_are_rejected_before_a_paid_call(generation_fixture):
    args, chat, _ = generation_fixture
    with (args.study / "requests.jsonl").open("a") as handle:
        handle.write("\n")
    with pytest.raises(ValueError, match="prompt records changed"):
        pilot.generate(args)
    assert chat.calls == 0


def test_changed_evaluation_cases_are_rejected(generation_fixture):
    args, _, _ = generation_fixture
    manifest = json.loads((args.study / "manifest.json").read_text())
    (args.study / "cases.json").write_text("changed gold labels")
    with pytest.raises(ValueError, match="evaluation cases changed"):
        pilot.read_cases(args.study, manifest)


def test_comparison_rejects_source_drift_but_allows_ranking_metadata(tmp_path):
    baseline = [
        {"page_content": "code", "metadata": {"file": "src.py", "start_line": 0}}
    ]
    contextual = [
        {
            "page_content": "code",
            "metadata": {
                "file": "src.py",
                "start_line": 0,
                "contextualization": {"prefix": "explanation"},
            },
        }
    ]
    for name, docs in [("B0", baseline), ("B2", contextual)]:
        (tmp_path / name).mkdir()
        pilot.write_json(tmp_path / name / "documents.json", docs)
    pilot.verify_arms(tmp_path, ["B0", "B2"])
    contextual[0]["metadata"]["start_line"] = 1
    pilot.write_json(tmp_path / "B2" / "documents.json", contextual)
    with pytest.raises(ValueError, match="source chunks"):
        pilot.verify_arms(tmp_path, ["B0", "B2"])
