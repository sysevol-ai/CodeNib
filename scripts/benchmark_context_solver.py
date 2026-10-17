#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Validate context with pinned mini SWE-agent and an explicit Claude route.

Supports SWE-bench Verified and the Multi fmt/Gson/ponyc pilots, one task per
invocation. Uses the existing solver loop and pinned, already-local Docker
images. Native grading is separate; source receipts cover controlled reads/searches.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import importlib.metadata
import json
import math
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tiktoken
import yaml

from codenib.eval.benchmarks.contextbench import CONTEXTBENCH_UPSTREAM_REVISION
from scripts import benchmark_context_agent as native
from scripts.benchmark_context_claude import api_auth, subscription_auth
from scripts.benchmark_jev_base import digest, write_json
from scripts.contextbench_multiswe import solver_cwd, validate_source_only_image
from scripts.contextbench_solver_sources import (
    SourceEnvironment,
    initial_source_receipt,
    source_attribution,
)

_HISTORY_PREFIX = (
    "Continue this shell-agent conversation. Return the next assistant reply "
    "using the required bash format. Commands will be executed by the external "
    "solver; you have no tools in this model request.\n"
)


def request_digest(request):
    return hashlib.sha256(
        json.dumps(request, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def model_observations(row):
    """Export only confirmed model inputs, never unrendered or terminal output.

    A recorded request becomes confirmed when its SDK stream yields an assistant
    message. A request without that evidence stays explicitly unconfirmed; it
    may still have consumed usage. mini's final messages can contain submission
    output that no subsequent model call received, so they are not a substitute.
    Source receipts can attribute controlled reads/searches within these exact inputs;
    arbitrary shell output remains outside that partial coverage artifact.
    """
    confirmed, missing, unconfirmed = [], [], []
    for call in row["calls"]:
        turn = call["turn"]
        request = call.get("request")
        if request is None:
            missing.append(turn)
            continue
        if request_digest(request) != call.get("request_sha256"):
            raise ValueError("Solver model input receipt changed")
        prompt = request["prompt"]
        if not prompt.startswith(_HISTORY_PREFIX):
            raise ValueError("Unknown solver history format")
        history = json.loads(prompt[len(_HISTORY_PREFIX) :])
        if not isinstance(history, list) or any(
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message["role"] not in {"user", "assistant"}
            or not isinstance(message["content"], str)
            for message in history
        ):
            raise ValueError("Invalid solver input history")
        if not call.get("input_delivery_confirmed"):
            unconfirmed.append(turn)
            continue
        confirmed.append(
            {
                "turn": turn,
                "model": call.get("model"),
                "request_sha256": call["request_sha256"],
                "messages": [
                    {"role": "system", "content": request["system_prompt"]},
                    *history,
                ],
            }
        )
    return {
        "instance_id": row["instance_id"],
        "confirmed_inputs": confirmed,
        "missing_input_receipts": missing,
        "unconfirmed_inputs": unconfirmed,
        "all_model_inputs_confirmed": bool(confirmed) and not (missing or unconfirmed),
        "source_attribution": source_attribution(row, confirmed),
        "scope": (
            "Exact model inputs after output formatting/truncation; terminal output "
            "is excluded unless a later confirmed model request contains it."
        ),
    }


class SubscriptionModel:
    """Adapt actual SDK replies to mini SWE-agent's synchronous model boundary."""

    def __init__(
        self,
        *,
        cli,
        model,
        cwd,
        max_meter_usd,
        calls,
        checkpoint,
        billing_route="subscription",
    ):
        if billing_route not in {"subscription", "anthropic-api"}:
            raise ValueError("Unknown solver billing route")
        self.cli, self.model, self.cwd = cli, model, cwd
        self.billing_route = billing_route
        self.max_meter_usd = max_meter_usd
        self.calls, self.checkpoint = calls, checkpoint
        self.cost, self.n_calls = 0.0, 0
        self.budget_exhausted = False

    def get_template_vars(self):
        return {"model_name": self.model, "model_cost": self.cost}

    def query(self, messages):
        if self.cost >= self.max_meter_usd:
            self.budget_exhausted = True
            raise RuntimeError("Solver SDK meter exhausted")
        self.n_calls += 1
        request = {
            "system_prompt": messages[0]["content"],
            "prompt": _HISTORY_PREFIX
            + json.dumps(
                [{"role": m["role"], "content": m["content"]} for m in messages[1:]],
                ensure_ascii=False,
            ),
        }
        record = {
            "turn": self.n_calls,
            "events": [],
            "request": request,
            "request_sha256": request_digest(request),
            "input_delivery_confirmed": False,
        }
        self.calls.append(record)
        self.checkpoint()
        response = asyncio.run(asyncio.wait_for(self._query(record), 180))
        return {"content": response}

    async def _query(self, record):
        from claude_agent_sdk import ClaudeAgentOptions, query

        options = ClaudeAgentOptions(
            cli_path=self.cli,
            cwd=self.cwd,
            model=self.model,
            effort="medium",
            system_prompt=record["request"]["system_prompt"],
            tools=[],
            allowed_tools=[],
            setting_sources=[],
            skills=[],
            plugins=[],
            agents={},
            mcp_servers={},
            strict_mcp_config=True,
            max_turns=1,
            max_budget_usd=self.max_meter_usd - self.cost,
            extra_args={"no-session-persistence": None},
        )
        response, terminal = [], None
        async for event in query(prompt=record["request"]["prompt"], options=options):
            kind, data = type(event).__name__, dataclasses.asdict(event)
            record["events"].append({"kind": kind, "data": data})
            if kind == "SystemMessage" and data.get("subtype") == "init":
                if data["data"].get("tools"):
                    raise ValueError("Solver model unexpectedly received local tools")
                if (
                    self.billing_route == "anthropic-api"
                    and data["data"].get("apiKeySource") != "ANTHROPIC_API_KEY"
                ):
                    raise ValueError("Solver SDK did not confirm the API-key route")
            if kind == "AssistantMessage":
                record["input_delivery_confirmed"] = True
                record["model"] = data.get("model")
                for block in data["content"]:
                    if "name" in block or "input" in block:
                        raise ValueError("Solver model attempted a local tool call")
                    if "text" in block:
                        response.append(block["text"])
            if kind == "ResultMessage":
                if terminal is not None:
                    raise ValueError("Duplicate solver SDK terminal result")
                terminal = data
                meter = terminal.get("total_cost_usd")
                if (
                    not isinstance(meter, (int, float))
                    or isinstance(meter, bool)
                    or not math.isfinite(meter)
                    or meter < 0
                ):
                    raise ValueError(
                        "Missing or invalid solver SDK metering; stop inference"
                    )
                # The SDK can raise after yielding an error result. Persist its
                # usage before advancing the stream, while preserving failure.
                self.cost += meter
                record["sdk_reported_cost_usd"] = meter
                if (
                    terminal["is_error"]
                    and terminal["subtype"] == "error_max_budget_usd"
                ):
                    self.budget_exhausted = True
            self.checkpoint()
        if terminal is None:
            raise ValueError("Missing or invalid solver SDK metering; stop inference")
        if terminal["is_error"] or not response:
            raise RuntimeError(f"Solver model failed: {terminal['subtype']}")
        return "\n".join(response)


def delivered_input(run, instance_id, budget):
    """Validate immutable retrieval inputs and retain the original task identity."""
    config = json.loads((run / "config.json").read_text())
    prepared = Path(config["prepared"])
    manifest, cases = native.load_inputs(prepared)
    if digest(prepared / "manifest.json") != config["manifest_sha256"]:
        raise ValueError("Retrieval source manifest changed")
    for name, sha in config["implementation"].items():
        if digest(run / "implementation" / name) != sha:
            raise ValueError("Archived retrieval implementation changed")
    case = cases[instance_id]
    solver_cwd(case.source, case.repo)
    path = run / f"{instance_id}.json"
    row = json.loads(path.read_text())
    if row["instance_id"] != instance_id or row["status"] not in {
        "ok",
        "budget_exhausted",
    }:
        raise ValueError("Retrieval did not produce a usable result")
    payload = row[f"served_{budget}"]
    if not payload["context"] or payload["tokens"] > budget:
        raise ValueError("Invalid delivered context")
    entry = next(
        item for item in manifest["cases"] if item["instance_id"] == instance_id
    )
    repo = Path(json.loads(Path(entry["source"]).read_text())["repo"])
    native.audit_contextbench_snapshot(repo, case.base_commit)
    tree = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], text=True
    ).strip()
    return case, payload["context"], digest(path), tree, repo


def retrieval_notes(run, instance_id, token_limit, expected_sha):
    """Retain an actual final SDK answer as explicitly unverified model analysis."""
    if token_limit == 0:
        return None
    if not 1 <= token_limit <= 2048:
        raise ValueError("Retrieval notes limit must be between 0 and 2048 tokens")
    raw = (run / f"{instance_id}.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("Retrieval changed before collecting notes")
    row = json.loads(raw)
    answer = row.get("answer")
    if (
        row.get("status") != "ok"
        or not isinstance(answer, str)
        or not answer.strip()
        or row.get("sdk_result", {}).get("is_error", True)
        or row.get("sdk_result", {}).get("result") != answer
        or not any(
            m.get("role") == "assistant"
            and not m.get("tool_calls")
            and m.get("content") == answer
            for m in row.get("messages", [])
        )
    ):
        raise ValueError("Retrieval notes lack a confirmed successful SDK answer")
    encoder = tiktoken.get_encoding("cl100k_base")
    tokens = encoder.encode(answer, disallowed_special=())
    selected = tokens[:token_limit]
    while selected:
        text = b"".join(
            encoder.decode_single_token_bytes(token) for token in selected
        ).decode("utf-8", errors="ignore")
        count = len(encoder.encode(text, disallowed_special=()))
        if count <= token_limit:
            break
        selected.pop()
    if not selected or not text or not answer.startswith(text):
        raise ValueError("Retrieval notes must be a nonempty exact answer prefix")
    return {
        "text": text,
        "tokens": count,
        "token_limit": token_limit,
        "truncated": text != answer,
        "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(),
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "retrieval_sha256": expected_sha,
        "status": "unverified model analysis, not source evidence",
    }


def validate_image_snapshot(result, expected_tree):
    """Official images can add a synthetic commit while preserving the base tree."""
    lines = result["output"].strip().splitlines()
    if result["returncode"] or len(lines) != 2 or lines[1] != expected_tree:
        raise ValueError("Solver image has different or dirty source")
    return {"commit": lines[0], "tree": lines[1], "matches_selected_tree": True}


def export_prediction(args, environment, row):
    """Save executed edits before cleanup, without adding a model observation."""
    patch = environment.execute("git add -A && git diff --cached --binary")
    if patch["returncode"]:
        raise RuntimeError("Failed to export solver patch")
    row["model_patch"] = patch["output"]
    write_json(
        args.output / "predictions.json",
        [
            {
                "instance_id": row["original_inst_id"],
                "model_name_or_path": args.model,
                "model_patch": patch["output"],
            }
        ],
    )


def freeze_solver(output, mini_root):
    implementation = native.freeze(output)
    paths = {
        "scripts/benchmark_context_solver.py": Path(__file__),
        "scripts/contextbench_multiswe.py": ROOT / "scripts/contextbench_multiswe.py",
        **{
            f"scripts/{name}": ROOT / "scripts" / name
            for name in (
                "contextbench_solver_sources.py",
                "_contextbench_solver_read.py",
            )
        },
        **{
            f"mini/{name}": mini_root / "minisweagent" / name
            for name in (
                "__init__.py",
                "agents/default.py",
                "environments/docker.py",
                "config/extra/swebench.yaml",
            )
        },
    }
    for name, path in paths.items():
        target = output / "implementation" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        implementation[name] = digest(target)
    return implementation


def run(args):
    if not args.live:
        raise ValueError("Solver inference requires --live")
    billing_route = getattr(args, "billing_route", "subscription")
    initial_context = getattr(args, "initial_context", "retrieved")
    if billing_route not in {"subscription", "anthropic-api"}:
        raise ValueError("Unknown solver billing route")
    if initial_context not in {"retrieved", "none"}:
        raise ValueError("Unknown initial context policy")
    if initial_context == "none" and getattr(args, "retrieval_notes_tokens", 0):
        raise ValueError("The no-context control cannot receive retrieval notes")
    if not 1 <= args.max_turns <= 80 or not 0 < args.max_meter_usd <= 100:
        raise ValueError("Invalid solver turn or metering limit")
    if not args.image.startswith("sha256:") and "@sha256:" not in args.image:
        raise ValueError("Use a pinned local image ID or digest, not a mutable tag")
    cli = shutil.which("claude")
    if not cli:
        raise ValueError("Claude CLI unavailable")
    commit = subprocess.check_output(
        ["git", "-C", str(args.upstream), "rev-parse", "HEAD"], text=True
    ).strip()
    if commit != CONTEXTBENCH_UPSTREAM_REVISION:
        raise ValueError("mini SWE-agent requires the pinned ContextBench checkout")
    case, context, retrieval_sha, source_tree, source_repo = delivered_input(
        args.retrieval, args.instance_id, args.budget
    )
    if initial_context == "none":
        context = "[]"
    notes = retrieval_notes(
        args.retrieval,
        args.instance_id,
        getattr(args, "retrieval_notes_tokens", 0),
        retrieval_sha,
    )
    cwd = solver_cwd(case.source, case.repo)
    image = json.loads(
        subprocess.check_output(["docker", "image", "inspect", args.image], text=True)
    )[0]
    if shutil.disk_usage(args.output.parent).free < 8 * 1024**3:
        raise RuntimeError("Less than 8 GiB free; solver preparation stopped")
    mini_root = (
        args.upstream
        / "agent-frameworks/mini-swe-agent/multi-poly-pro-verified/mini-swe-agent/src"
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(args.upstream),
            "diff",
            "--exit-code",
            "HEAD",
            "--",
            str(mini_root.resolve()),
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    sys.path.insert(0, str(mini_root))
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.environments.docker import DockerEnvironment

    # The native package initializer can load a user-global dotenv file. Check
    # the effective provider route after that import, before any model request.
    auth = subscription_auth(cli) if billing_route == "subscription" else api_auth(cli)
    config_path = mini_root / "minisweagent/config/extra/swebench.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["agent"].update(
        step_limit=args.max_turns,
        cost_limit=args.max_meter_usd,
        system_template=(
            "Repair the reported software issue using a container shell. Return "
            "exactly one bash code block per reply. You may include a brief action "
            "summary. The external solver executes your command and returns its "
            "output. Commands run in separate shells; files persist."
        ),
        instance_template=(
            "<issue>\n{{task}}\n</issue>\n"
            "<retrieved_source>\n{{retrieved_source}}\n</retrieved_source>\n"
            f"Work in {cwd}. Verify this initial source against the checkout, "
            "implement a general fix in production source, and run relevant tests. "
            "For source inspection use a standalone command: "
            "codenib-read PATH START END (1-based inclusive lines, at most 2000). "
            "For source search use a standalone command: "
            "codenib-grep PATTERN PATH [LIMIT] (Python regex, a file or directory, "
            "default 100 matches, maximum 200). Tests are included; scan/output "
            "limits and skipped files are reported. Narrow PATH or read the "
            "reported source ranges when a limit is reached. Both commands "
            "resolve paths from the repository root; do not combine them with "
            "cd, pipes or other commands. Use ordinary shell commands for "
            "edits, builds and tests. "
            "Keep existing tests and configuration unchanged. The container has "
            "no network access. When finished, submit with:\n```bash\n"
            "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && git add -A && git diff --cached\n"
            "```\n"
        ),
        format_error_template="Return exactly one executable bash code block.",
    )
    if notes is not None:
        config["agent"]["instance_template"] = config["agent"][
            "instance_template"
        ].replace(
            "</retrieved_source>\n",
            "</retrieved_source>\n"
            "<retrieval_notes>\n{{retrieval_notes}}\n</retrieval_notes>\n"
            "These are unverified notes from the retrieval agent. Check their "
            "claims and proposed fixes against the issue, source and tests; "
            "preserve valid behavior that their hypothesis may overlook.\n",
            1,
        )
    args.output.mkdir(parents=True, exist_ok=False)
    row = {
        "instance_id": case.instance_id,
        "original_inst_id": case.original_inst_id,
        "status": "starting",
        "calls": [],
        "execution_receipts": [],
        "initial_source_receipt": initial_source_receipt(
            context, source_repo, case.base_commit
        ),
        "actual_bill_usd": None,
        "native_resolution": None,
        "retrieval_notes": notes,
    }
    result_path = args.output / "solver.json"

    def checkpoint():
        write_json(result_path, row)

    write_json(
        args.output / "config.json",
        {
            "retrieval": str(args.retrieval.resolve()),
            "retrieval_sha256": retrieval_sha,
            "context_budget": args.budget,
            "initial_context": initial_context,
            "billing_route": billing_route,
            "retrieval_notes_token_limit": getattr(args, "retrieval_notes_tokens", 0),
            "context_budget_scope": (
                "Source budget plus separately bounded unverified retrieval notes. "
                "Report their additional input tokens; notes receive no source credit."
            ),
            "source_commit": case.base_commit,
            "source_tree": source_tree,
            "source": case.source,
            "repo": case.repo,
            "solver_cwd": cwd,
            "upstream_commit": commit,
            "mini_config_sha256": digest(config_path),
            "image_id": image["Id"],
            "image_repo_digests": image.get("RepoDigests", []),
            "auth": auth,
            "model_requested": args.model,
            "max_turns": args.max_turns,
            "max_meter_usd": args.max_meter_usd,
            "patch_collection_policy": (
                "Export executed edits after normal termination or a fully metered "
                "SDK budget stop. Budget exceptions remain errors; terminal patch "
                "export is not model-observed source."
            ),
            "sdk_version": importlib.metadata.version("claude-agent-sdk"),
            "scope": (
                "Downstream solver; native grading required. Controlled reads/searches "
                "have partial attribution; arbitrary shell output has no line credit."
            ),
            "implementation": freeze_solver(args.output, mini_root),
        },
    )
    environment, agent, model = None, None, None
    started = time.perf_counter()
    try:
        environment = DockerEnvironment(
            image=image["Id"],
            cwd=cwd,
            timeout=120,
            env=config["environment"]["env"],
            run_args=[
                "--rm",
                "--network",
                "none",
                "--memory",
                "4g",
                "--cpus",
                "2",
                "--pids-limit",
                "512",
            ],
        )
        # This invocation owns this one container. Persist its identity before
        # model work; synchronous removal below is the lifecycle completion point.
        # No shared lock or container is reused. Docker's two-hour timeout bounds
        # an orphan if the owning process is killed before its finally block.
        row["container_id"] = environment.container_id
        checkpoint()
        head = environment.execute(
            "git rev-parse HEAD HEAD^{tree} && git status --porcelain --untracked-files=all"
        )
        row["image_snapshot"] = validate_image_snapshot(head, source_tree)
        if case.source == "Multi":
            row["source_only_image"] = validate_source_only_image(
                environment, row["image_snapshot"]["commit"]
            )
            checkpoint()
        with tempfile.TemporaryDirectory(prefix="codenib-solver-model-") as directory:
            model = SubscriptionModel(
                cli=cli,
                model=args.model,
                cwd=Path(directory),
                max_meter_usd=args.max_meter_usd,
                calls=row["calls"],
                checkpoint=checkpoint,
                billing_route=billing_route,
            )

            def observation(command, output, timeout):
                if timeout:
                    rendered = agent.render_template(
                        agent.config.timeout_template,
                        action={"action": command},
                        output=output["output"],
                    )
                else:
                    rendered = agent.render_template(
                        agent.config.action_observation_template, output=output
                    )
                return len(agent.messages), rendered

            observed_environment = SourceEnvironment(
                environment,
                root=cwd,
                commit=row["image_snapshot"]["commit"],
                receipts=row["execution_receipts"],
                checkpoint=checkpoint,
                observation=observation,
            )
            agent = DefaultAgent(model, observed_environment, **config["agent"])
            status, answer = agent.run(
                case.problem_statement,
                retrieved_source=context,
                retrieval_notes=notes["text"] if notes else "",
            )
            row.update(status=status, answer=answer)
        row["patch_export_reason"] = "agent_termination"
        export_prediction(args, environment, row)
    except Exception as exc:
        row.update(status="error", error=f"{type(exc).__name__}: {exc}")
        if (
            model is not None
            and model.budget_exhausted
            and all("sdk_reported_cost_usd" in call for call in row["calls"])
        ):
            # The SDK can raise after its metered budget result, bypassing mini's
            # normal limit return. Retain the error and already-executed edits;
            # never execute the failed reply or make a further model request.
            row["budget_exhausted"] = True
            row["patch_export_reason"] = "metered_sdk_budget_stop"
            try:
                export_prediction(args, environment, row)
            except Exception as patch_exc:
                row["patch_export_error"] = f"{type(patch_exc).__name__}: {patch_exc}"
    finally:
        if agent is not None:
            row["messages"] = agent.messages
        row["wall_s"] = time.perf_counter() - started
        missing = sum("sdk_reported_cost_usd" not in call for call in row["calls"])
        row["missing_metering_calls"] = missing
        row["known_sdk_reported_cost_usd"] = model.cost if model else 0.0
        row["sdk_reported_cost_usd"] = (
            None if missing else row["known_sdk_reported_cost_usd"]
        )
        checkpoint()
        if environment is not None and environment.container_id:
            subprocess.run(
                ["docker", "rm", "-f", environment.container_id],
                capture_output=True,
                check=True,
                timeout=60,
            )
            environment.container_id = None
            row["container_removed"] = True
            checkpoint()
    write_json(args.output / "model-observations.json", model_observations(row))
    print(
        json.dumps(
            {
                key: row.get(key)
                for key in (
                    "instance_id",
                    "status",
                    "error",
                    "sdk_reported_cost_usd",
                    "native_resolution",
                )
            }
        )
    )


def grade(args):
    """Use the selected benchmark's native test script and resolution grader."""
    config = json.loads((args.solver_run / "config.json").read_text())
    if config.get("source", "Verified") == "Multi":
        from scripts.contextbench_multiswe import grade_multi

        grade_multi(args)
        return
    if config.get("source", "Verified") != "Verified":
        raise ValueError("Unsupported native solver benchmark")
    if importlib.metadata.version("swebench") != "4.1.0":
        raise ValueError("Native grading requires isolated swebench==4.1.0")
    import docker
    from swebench.harness import run_evaluation
    from swebench.harness.test_spec.test_spec import TestSpec, make_test_spec

    solver = json.loads((args.solver_run / "solver.json").read_text())
    predictions = json.loads((args.solver_run / "predictions.json").read_text())
    if (
        len(predictions) != 1
        or predictions[0]["instance_id"] != solver["original_inst_id"]
    ):
        raise ValueError("Native prediction identities differ")
    if predictions[0]["model_patch"] != solver["model_patch"]:
        raise ValueError("Native prediction differs from the solver patch")
    selected = [
        row
        for row in json.loads(args.dataset.read_text())
        if row["instance_id"] == solver["original_inst_id"]
    ]
    if len(selected) != 1 or selected[0]["base_commit"] != config["source_commit"]:
        raise ValueError("Native task differs from the selected source snapshot")
    for name, sha in config["implementation"].items():
        if digest(args.solver_run / "implementation" / name) != sha:
            raise ValueError("Archived solver implementation changed")
    image_id = config["image_id"]
    client = docker.from_env()
    client.images.get(image_id)  # Require the exact already-local solver image.

    class PinnedImageSpec(TestSpec):
        @property
        def instance_image_key(self):
            return image_id

    native_spec = make_test_spec(selected[0], namespace="swebench")
    spec = PinnedImageSpec(**dataclasses.asdict(native_spec))
    args.output.mkdir(parents=True, exist_ok=False)
    run_id = "codenib-" + uuid.uuid4().hex[:12]
    write_json(
        args.output / "config.json",
        {
            "solver_run": str(args.solver_run.resolve()),
            "solver_sha256": digest(args.solver_run / "solver.json"),
            "prediction_sha256": digest(args.solver_run / "predictions.json"),
            "dataset_sha256": digest(args.dataset),
            "native_harness_version": "4.1.0",
            "image_id": image_id,
            "run_id": run_id,
            "timeout": args.timeout,
            "adapter_sha256": digest(Path(__file__)),
            "scope": (
                "Native Verified patch application, reference test patch "
                "and resolution grading"
            ),
        },
    )
    (args.output / "eval.sh").write_text(spec.eval_script)
    run_evaluation.RUN_EVALUATION_LOG_DIR = args.output / "logs"
    result = run_evaluation.run_instance(
        spec, predictions[0], False, False, client, run_id, timeout=args.timeout
    )
    if not result or not result.get("completed"):
        record = {
            "instance_id": solver["instance_id"],
            "status": "error",
            "resolved": False,
        }
    else:
        report_path = (
            args.output
            / "logs"
            / run_id
            / predictions[0]["model_name_or_path"].replace("/", "__")
            / solver["original_inst_id"]
            / "report.json"
        )
        report = json.loads(report_path.read_text())
        record = {
            "instance_id": solver["instance_id"],
            "status": "graded",
            "resolved": report[solver["original_inst_id"]]["resolved"],
            "native_report": report,
        }
    write_json(args.output / "resolution.json", record)
    print(
        json.dumps({key: record[key] for key in ("instance_id", "status", "resolved")})
    )


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "grade":
        parser = argparse.ArgumentParser(description=grade.__doc__)
        parser.add_argument("--solver-run", type=Path, required=True)
        parser.add_argument("--dataset", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)
        parser.add_argument("--timeout", type=int, default=900)
        parser.add_argument(
            "--native-image",
            help="Multi only: separate, pinned local native grading image",
        )
        grade(parser.parse_args(sys.argv[2:]))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retrieval", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument(
        "--billing-route",
        choices=("subscription", "anthropic-api"),
        default="subscription",
    )
    parser.add_argument(
        "--initial-context", choices=("retrieved", "none"), default="retrieved"
    )
    parser.add_argument("--budget", type=int, choices=(16384, 32768), default=16384)
    parser.add_argument("--retrieval-notes-tokens", type=int, default=0)
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--max-meter-usd", type=float, default=3.0)
    parser.add_argument("--live", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
