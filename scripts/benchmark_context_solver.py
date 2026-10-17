#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Validate a delivered context with the pinned mini SWE-agent and Claude Max.

This first adapter supports one SWE-bench Verified task per invocation. It uses
the existing solver loop and a pinned, already-local Docker image. Native test
grading is separate; shell observations are not claimed as ContextBench reads.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
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

import yaml

from codenib.eval.benchmarks.contextbench import CONTEXTBENCH_UPSTREAM_REVISION
from scripts import benchmark_context_agent as native
from scripts.benchmark_context_claude import subscription_auth
from scripts.benchmark_jev_base import digest, write_json


class SubscriptionModel:
    """Adapt actual SDK replies to mini SWE-agent's synchronous model boundary."""

    def __init__(self, *, cli, model, cwd, max_meter_usd, calls, checkpoint):
        self.cli, self.model, self.cwd = cli, model, cwd
        self.max_meter_usd = max_meter_usd
        self.calls, self.checkpoint = calls, checkpoint
        self.cost, self.n_calls = 0.0, 0

    def get_template_vars(self):
        return {"model_name": self.model, "model_cost": self.cost}

    def query(self, messages):
        if self.cost >= self.max_meter_usd:
            raise RuntimeError("Solver SDK meter exhausted")
        self.n_calls += 1
        record = {"turn": self.n_calls, "events": []}
        self.calls.append(record)
        self.checkpoint()
        response = asyncio.run(asyncio.wait_for(self._query(messages, record), 180))
        return {"content": response}

    async def _query(self, messages, record):
        from claude_agent_sdk import ClaudeAgentOptions, query

        options = ClaudeAgentOptions(
            cli_path=self.cli,
            cwd=self.cwd,
            model=self.model,
            effort="medium",
            system_prompt=messages[0]["content"],
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
        history = [{"role": m["role"], "content": m["content"]} for m in messages[1:]]
        prompt = (
            "Continue this shell-agent conversation. Return the next assistant reply "
            "using the required bash format. Commands will be executed by the external "
            "solver; you have no tools in this model request.\n"
            + json.dumps(history, ensure_ascii=False)
        )
        response, terminal = [], None
        async for event in query(prompt=prompt, options=options):
            kind, data = type(event).__name__, dataclasses.asdict(event)
            record["events"].append({"kind": kind, "data": data})
            if kind == "SystemMessage" and data.get("subtype") == "init":
                if data["data"].get("tools"):
                    raise ValueError("Solver model unexpectedly received local tools")
            if kind == "AssistantMessage":
                record["model"] = data.get("model")
                for block in data["content"]:
                    if "name" in block or "input" in block:
                        raise ValueError("Solver model attempted a local tool call")
                    if "text" in block:
                        response.append(block["text"])
            if kind == "ResultMessage":
                terminal = data
            self.checkpoint()
        meter = terminal.get("total_cost_usd") if terminal else None
        if (
            not isinstance(meter, (int, float))
            or isinstance(meter, bool)
            or not math.isfinite(meter)
            or meter < 0
        ):
            raise ValueError("Missing or invalid solver SDK metering; stop inference")
        self.cost += meter
        record["sdk_reported_cost_usd"] = meter
        self.checkpoint()
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
    if case.source != "Verified":
        raise ValueError("The initial solver adapter supports Verified only")
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
    return case, payload["context"], digest(path), tree


def validate_image_snapshot(result, expected_tree):
    """Official images can add a synthetic commit while preserving the base tree."""
    lines = result["output"].strip().splitlines()
    if result["returncode"] or len(lines) != 2 or lines[1] != expected_tree:
        raise ValueError("Solver image has different or dirty source")
    return {"commit": lines[0], "tree": lines[1], "matches_selected_tree": True}


def freeze_solver(output, mini_root):
    implementation = native.freeze(output)
    paths = {
        "scripts/benchmark_context_solver.py": Path(__file__),
        **{
            f"mini/{name}": mini_root / "minisweagent" / name
            for name in (
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
        raise ValueError("Subscription solver inference requires --live")
    if not 1 <= args.max_turns <= 80 or not 0 < args.max_meter_usd <= 100:
        raise ValueError("Invalid solver turn or metering limit")
    if not args.image.startswith("sha256:") and "@sha256:" not in args.image:
        raise ValueError("Use a pinned local image ID or digest, not a mutable tag")
    cli = shutil.which("claude")
    if not cli:
        raise ValueError("Claude CLI unavailable")
    auth = subscription_auth(cli)
    commit = subprocess.check_output(
        ["git", "-C", str(args.upstream), "rev-parse", "HEAD"], text=True
    ).strip()
    if commit != CONTEXTBENCH_UPSTREAM_REVISION:
        raise ValueError("mini SWE-agent requires the pinned ContextBench checkout")
    case, context, retrieval_sha, source_tree = delivered_input(
        args.retrieval, args.instance_id, args.budget
    )
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
            "Work in /testbed. Verify this initial source against the checkout, "
            "implement a general fix in production source, and run relevant tests. "
            "Keep existing tests and configuration unchanged. The container has "
            "no network access. When finished, submit with:\n```bash\n"
            "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && git add -A && git diff --cached\n"
            "```\n"
        ),
        format_error_template="Return exactly one executable bash code block.",
    )
    args.output.mkdir(parents=True, exist_ok=False)
    row = {
        "instance_id": case.instance_id,
        "original_inst_id": case.original_inst_id,
        "status": "starting",
        "calls": [],
        "actual_bill_usd": None,
        "native_resolution": None,
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
            "source_commit": case.base_commit,
            "source_tree": source_tree,
            "upstream_commit": commit,
            "mini_config_sha256": digest(config_path),
            "image_id": image["Id"],
            "image_repo_digests": image.get("RepoDigests", []),
            "auth": auth,
            "model_requested": args.model,
            "max_turns": args.max_turns,
            "max_meter_usd": args.max_meter_usd,
            "sdk_version": importlib.metadata.version("claude-agent-sdk"),
            "scope": (
                "Downstream solver only; native grading required. Shell reads are "
                "not ContextBench trajectory credit."
            ),
            "implementation": freeze_solver(args.output, mini_root),
        },
    )
    environment, agent, model = None, None, None
    started = time.perf_counter()
    try:
        environment = DockerEnvironment(
            image=image["Id"],
            cwd="/testbed",
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
        with tempfile.TemporaryDirectory(prefix="codenib-solver-model-") as directory:
            model = SubscriptionModel(
                cli=cli,
                model=args.model,
                cwd=Path(directory),
                max_meter_usd=args.max_meter_usd,
                calls=row["calls"],
                checkpoint=checkpoint,
            )
            agent = DefaultAgent(model, environment, **config["agent"])
            status, answer = agent.run(case.problem_statement, retrieved_source=context)
            row.update(status=status, answer=answer)
        patch = environment.execute("git add -A && git diff --cached --binary")
        if patch["returncode"]:
            raise RuntimeError("Failed to export solver patch")
        row["model_patch"] = patch["output"]
        write_json(
            args.output / "predictions.json",
            [
                {
                    "instance_id": case.original_inst_id,
                    "model_name_or_path": args.model,
                    "model_patch": patch["output"],
                }
            ],
        )
    except Exception as exc:
        row.update(status="error", error=f"{type(exc).__name__}: {exc}")
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
    """Use the unmodified native SWE-bench test script and resolution grader."""
    if importlib.metadata.version("swebench") != "4.1.0":
        raise ValueError("Native grading requires isolated swebench==4.1.0")
    import docker
    from swebench.harness import run_evaluation
    from swebench.harness.test_spec.test_spec import TestSpec, make_test_spec

    config = json.loads((args.solver_run / "config.json").read_text())
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
        grade(parser.parse_args(sys.argv[2:]))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retrieval", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--budget", type=int, choices=(16384, 32768), default=16384)
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--max-meter-usd", type=float, default=3.0)
    parser.add_argument("--live", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
