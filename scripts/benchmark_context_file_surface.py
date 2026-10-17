#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Source-only development runner for on-demand file/function outlines.

No vector/graph build, retrieval fallback, repair or API-key route is selected.
Live runs retain the native pilot's pinned SDK/CLI, main model, turn/budget and
included-Max checks. Default execution prepares source and local metadata only.
"""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codenib.compiler.manifest import RepoManifest
from codenib.languages import get_language_spec
from codenib.mcp.context import ServerContext
from codenib.source_fingerprint import capture_repository_source
from scripts import benchmark_context_claude as claude
from scripts.benchmark_jev_base import write_json
from scripts.contextbench_file_surface import GUIDANCE, FileSurfaceContext


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args):
    record = json.loads(args.input.read_text())
    if set(record) != {
        "instance_id",
        "repo",
        "base_commit",
        "problem_statement",
        "language",
    }:
        raise ValueError("Input must contain only the five source/issue fields")
    repo = Path(record["repo"]).resolve()
    if args.output.resolve().is_relative_to(repo):
        raise ValueError("Experiment output must be outside the source checkout")
    spec = get_language_spec(record["language"])
    if spec is None:
        raise ValueError("Unsupported dataset language")
    cli = shutil.which("claude")
    sdk_version = cli_version = auth = None
    if args.live:
        import claude_agent_sdk  # noqa: F401

        if os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") != "4096":
            raise ValueError("This study requires the frozen 4096 output-token setting")
        if cli is None:
            raise ValueError("Claude CLI is required")
        sdk_version = importlib.metadata.version("claude-agent-sdk")
        cli_version = subprocess.check_output([cli, "--version"], text=True).strip()
        if sdk_version != "0.2.165" or cli_version != "2.1.301 (Claude Code)":
            raise ValueError("SDK/CLI must match the native control")
        auth = claude.subscription_auth(cli)
        if (
            auth.get("subscriptionType") != "max"
            or auth.get("apiProvider") != "firstParty"
        ):
            raise ValueError(
                "This development runner requires included first-party Max"
            )
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    dirty = bool(
        subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
    )
    if args.live and dirty:
        raise ValueError("Live development runs require a frozen clean runtime")
    implementation_paths = [
        Path(__file__).resolve(),
        ROOT / "scripts/contextbench_file_surface.py",
        ROOT / "scripts/contextbench_surface.py",
        ROOT / "scripts/benchmark_context_claude.py",
        ROOT / "scripts/benchmark_context_agent.py",
        ROOT / "codenib/languages.py",
        ROOT / "codenib/source_fingerprint.py",
        *sorted((ROOT / "codenib/code_chunking").glob("*.py")),
    ]
    implementation = {str(p.relative_to(ROOT)): digest(p) for p in implementation_paths}
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(
        args.output / "protocol.json",
        {
            "strategy": "on_demand_file_surface_v1",
            "scope": "Exposed development only; no independent or repair gain claim.",
            "input_sha256": digest(args.input),
            "source_commit": commit,
            "code_dirty": dirty,
            "implementation": implementation,
            "live": args.live,
            "views": [],
            "embedding_model": None,
            "graph_preparation_attempted": False,
            "BM25": False,
            "Jev": False,
            "model": "claude-opus-5-5",
            "max_turns": 20,
            "requested_max_output_tokens": 4096,
            "case_guard_usd": 0.75,
            "metadata_token_budget_cl100k": 2048,
            "body_page_lines": 200,
            "sdk_version": sdk_version,
            "cli_version": cli_version,
            "subscription_auth": auth,
            "new_API_usage_usd": 0,
            "repair_calls": 0,
        },
    )
    row = {
        "instance_id": record["instance_id"],
        "status": "preparing",
        "messages": [{"role": "user", "content": record["problem_statement"]}],
        "calls": [],
        "sdk_events": [],
    }
    path = args.output / "trajectory.json"
    binding = ctx = None
    started = time.perf_counter()
    try:
        row["snapshot_before"] = claude.native.audit_contextbench_snapshot(
            repo, record["base_commit"]
        )
        binding = capture_repository_source(repo)
        manifest = RepoManifest(
            repo_path=str(repo),
            commit=record["base_commit"],
            source_fingerprint=binding.fingerprint,
            file_count=binding.file_count,
            languages=[spec.chunker_language] if spec.chunker_language else [],
        )
        manifest.save(args.output / "repo_manifest.json")
        ctx = ServerContext.load(manifest, views=[], source_binding=binding)
        if ctx.source_verified is not True or ctx.loaded_views:
            raise ValueError("Source-only context admission failed")
        probe = FileSurfaceContext(ctx, observed_messages=lambda: ())
        candidates = sorted(
            (
                r.path
                for r in binding.file_records
                if "/" not in r.path
                and r.link_target is None
                and r.size <= 2 * 1024 * 1024
            ),
            key=lambda name: (not name.lower().startswith("readme"), name),
        )
        if not candidates:
            raise ValueError("No bounded root file is available for local preflight")
        preflight = probe.execute(file_path=candidates[0])
        write_json(args.output / "local-preflight.json", json.loads(preflight))
        # Local preflight candidates/handles never enter the model's history.
        surface = FileSurfaceContext(
            ctx, observed_messages=lambda: tuple(row["messages"])
        )
        row["source_context"] = {
            "verified": True,
            "loaded_views": [],
            "file_count": binding.file_count,
            "source_fingerprint": binding.fingerprint,
            "preparation_seconds": time.perf_counter() - started,
        }
        if args.live:
            claude.native.audit_contextbench_snapshot(repo, record["base_commit"])
            with (args.output / "model-intent.json").open("x") as stream:
                json.dump(
                    {"instance_id": record["instance_id"], "started_unix": time.time()},
                    stream,
                )
                stream.flush()
                os.fsync(stream.fileno())
            row["status"] = "running"
            asyncio.run(
                asyncio.wait_for(
                    claude.retrieve(
                        repo=repo,
                        query_text=record["problem_statement"],
                        row=row,
                        checkpoint=lambda: write_json(path, row),
                        cli=cli,
                        model="claude-opus-5-5",
                        max_turns=20,
                        case_meter_usd=0.75,
                        max_included_only=True,
                        context_tool=surface.tool(),
                        context_guidance=GUIDANCE,
                    ),
                    timeout=600,
                )
            )
            row["snapshot_after"] = claude.native.audit_contextbench_snapshot(
                repo, record["base_commit"]
            )
            history = row["messages"]
            confirmed = [
                m
                for i, m in enumerate(history)
                if m.get("role") != "tool"
                or any(x.get("role") == "assistant" for x in history[i + 1 :])
            ]
            write_json(
                args.output / "prediction.json",
                claude.native.contextbench_prediction(
                    instance_id=record["instance_id"],
                    messages=confirmed,
                    repo_path=repo,
                ),
            )
        else:
            row["status"] = "prepared_no_model_call"
            row["snapshot_after"] = claude.native.audit_contextbench_snapshot(
                repo, record["base_commit"]
            )
        if not all(
            digest(ROOT / name) == value for name, value in implementation.items()
        ):
            raise ValueError("Frozen runtime changed during execution")
    except Exception as exc:
        row.update(status="error", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        row["elapsed_seconds"] = time.perf_counter() - started
        write_json(path, row)
        if ctx is not None:
            ctx.close()
        elif binding is not None:
            binding.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    run(parser.parse_args())
