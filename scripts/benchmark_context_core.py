#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Source-checkout pilot of real CodeNib context, without repair or API fallback.

Each invocation owns one fresh output directory and one model intent. It builds
vector/symbol-graph views or explicitly checks a same-case local pilot producer,
then uses the existing SDK retrieval loop. Run on development tasks before claiming
independent benchmark gains. An interrupted model intent is never retried here.
"""

from __future__ import annotations

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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codenib.compiler.index_builders import (
    IndexBuilderRegistry,
    register_default_builders,
)
from codenib.compiler.index_compiler import IndexCompiler, IndexCompilerConfig
from codenib.compiler.manifest import RepoManifest
from codenib.index.embedding.artifact_integrity import capture_authenticated_vector_view
from codenib.languages import get_language_spec
from codenib.mcp.context import ServerContext
from codenib.native_index_authorization import _mint_trusted_local_admin_authorization
from codenib.provider_routes import resolve_embedding_artifact_route
from codenib.source_fingerprint import capture_repository_source
from scripts import benchmark_context_claude as claude
from scripts.benchmark_jev_base import write_json
from scripts.contextbench_codenib_context import make_context_tool
from scripts.contextbench_context_hints import ContextHints
from scripts.contextbench_surface import GUIDANCE as SURFACE_GUIDANCE
from scripts.contextbench_surface import SurfaceContext


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def index_languages(language):
    """Translate dataset labels through the registry's chunker surface."""
    spec = get_language_spec(language)
    if spec is None or spec.chunker_language is None:
        raise ValueError(f"No code chunker for dataset language: {language}")
    chunker_language = spec.chunker_language
    return (
        ["javascript", "typescript"]
        if chunker_language in {"javascript", "typescript"}
        else [chunker_language]
    )


def prepare_manifest(args, record, repo, languages):
    """Build locally, or explicitly reuse this same case's verified pilot output."""
    if args.reuse_from is not None:
        producer = args.reuse_from.resolve()
        protocol = json.loads((producer / "protocol.json").read_text())
        raw = json.loads((producer / "trajectory.json").read_text())
        manifest = RepoManifest.load(producer / "indexes/repo_manifest.json")
        recorded = json.loads((producer / "index-outcomes.json").read_text())
        prepared = (
            raw["status"] == "prepared_no_model_call"
            and protocol.get("live") is False
            and raw.get("sdk_events") == []
            and raw.get("sdk_result") is None
            and not (producer / "model-intent.json").exists()
        )
        if (
            raw["instance_id"] != record["instance_id"]
            or not (raw["status"] in {"ok", "budget_exhausted"} or prepared)
            or protocol["input_sha256"] != digest(args.input)
            or Path(manifest.repo_path).resolve() != repo
            or manifest.commit != record["base_commit"]
            or manifest.to_dict() != recorded
            or protocol["embedding_model"] != args.embedding_model
            or protocol["embedding_revision"] != args.embedding_revision
            or raw["snapshot_before"]["commit"] != record["base_commit"]
            or raw.get("snapshot_after", {}).get("commit") != record["base_commit"]
        ):
            raise ValueError("Cached producer must match the exact source/issue case")
        route = resolve_embedding_artifact_route(manifest.indexes["vector"].config)
        if (
            route.provider != "huggingface"
            or route.model != args.embedding_model
            or manifest.indexes["vector"]
            .config.get("embedding_kwargs", {})
            .get("revision")
            != args.embedding_revision
            or not manifest.index_is_current("vector")
        ):
            raise ValueError("Cached pilot requires the declared local embedding route")
        write_json(
            args.output / "cache-producer.json",
            {
                "producer": str(producer),
                "protocol_sha256": digest(producer / "protocol.json"),
                "trajectory_sha256": digest(producer / "trajectory.json"),
                "index_manifest_sha256": digest(
                    producer / "indexes/repo_manifest.json"
                ),
                "cold_index_build_cost_reused_not_zero": True,
            },
        )
        write_json(args.output / "index-outcomes.json", manifest.to_dict())
        # Retain manifest pointers so this verified preparation can itself be
        # reused. Payloads stay at their recorded producer paths, not copied.
        (args.output / "indexes").mkdir()
        manifest.save(args.output / "indexes/repo_manifest.json")
        return manifest, "contextbench-explicit-owned-cache-reuse"
    registry = IndexBuilderRegistry()
    register_default_builders(
        registry,
        languages=languages,
        embedding_model=args.embedding_model,
        embedding_revision=args.embedding_revision,
        trust_remote_code=True,
        embedding_batch_size=16,
        embedding_max_seq_length=1024,
        allow_graph_project_preparation=False,
    )
    compiler = IndexCompiler(
        registry,
        IndexCompilerConfig(
            index_types=(
                ["vector"] if args.vector_only else ["vector", "symbol_graph"]
            ),
            languages=languages,
        ),
    )
    manifest = compiler.compile_repo(
        str(repo), index_types=["vector"], cache_dir=str(args.output / "indexes")
    )
    write_json(args.output / "index-outcomes.json", manifest.to_dict())
    if not manifest.index_is_current("vector"):
        raise ValueError("Dense index build failed; do not substitute native tools")
    if not args.vector_only:
        manifest = compiler.compile_repo(
            str(repo),
            index_types=["symbol_graph"],
            cache_dir=str(args.output / "indexes"),
        )
        write_json(args.output / "index-outcomes.json", manifest.to_dict())
    return manifest, "contextbench-pilot-owned-build"


def run(args):
    if args.surface_context and args.read_reference_hints:
        raise ValueError("Surface-first and post-read hints are separate policies")
    record = json.loads(args.input.read_text())
    required = {"instance_id", "repo", "base_commit", "problem_statement", "language"}
    if set(record) != required:
        raise ValueError(
            "Input must contain only the five declared source/issue fields"
        )
    repo = Path(record["repo"]).resolve()
    cli = shutil.which("claude")
    if args.live and not cli:
        raise ValueError("Claude CLI is required")
    sdk_version = None
    cli_version = None
    if args.live:
        # Check the actual execution interpreter before indexing or claiming an
        # intent: the base GPU Python may lack the SDK installed in our venv.
        import claude_agent_sdk  # noqa: F401

        sdk_version = importlib.metadata.version("claude-agent-sdk")
        cli_version = subprocess.run(
            [cli, "--version"], check=True, capture_output=True, text=True, timeout=30
        ).stdout.strip()
        if sdk_version != "0.2.165" or cli_version != "2.1.301 (Claude Code)":
            raise ValueError("Pilot SDK/CLI versions must match the native control")
    auth = claude.subscription_auth(cli) if args.live else None
    if args.live and (
        auth.get("subscriptionType") != "max" or auth.get("apiProvider") != "firstParty"
    ):
        raise ValueError("This pilot requires existing first-party Max inclusion")
    args.output.mkdir(parents=True, exist_ok=False)
    languages = index_languages(record["language"])
    implementation = {
        str(path.relative_to(ROOT)): digest(path)
        for path in [
            Path(__file__).resolve(),
            ROOT / "scripts/contextbench_codenib_context.py",
            ROOT / "scripts/benchmark_context_claude.py",
            ROOT / "codenib/compiler/index_builders.py",
            ROOT / "codenib/mcp/tools/explore.py",
            ROOT / "scripts/contextbench_context_hints.py",
            ROOT / "scripts/contextbench_graph_navigation.py",
            ROOT / "scripts/contextbench_surface.py",
            ROOT / "codenib/code_chunker.py",
            ROOT / "codenib/index/embedding/builders.py",
        ]
    }
    write_json(
        args.output / "protocol.json",
        {
            "scope": (
                "Exposed development pilot of actual core context; "
                "no repair or independent gain claim."
            ),
            "input_sha256": digest(args.input),
            "implementation": implementation,
            "source_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "embedding_model": args.embedding_model,
            "embedding_revision": args.embedding_revision,
            "embedding_dimension": 768,
            "embedding_max_seq_length": 1024,
            "languages": languages,
            "local_model_cache": os.environ.get("HF_HUB_CACHE"),
            "offline_embeddings": os.environ.get("HF_HUB_OFFLINE") == "1",
            "views": ["vector"] if args.vector_only else ["vector", "symbol_graph"],
            "vector_only": args.vector_only,
            "reuse_from": str(args.reuse_from) if args.reuse_from else None,
            "read_reference_hints": args.read_reference_hints,
            "surface_context": args.surface_context,
            "surface_limits": (
                {"metadata_cl100k_tokens": 2048, "entries": 8, "body_page_lines": 200}
                if args.surface_context
                else None
            ),
            "hint_limits": (
                {
                    "tokens_cl100k_per_read": 256,
                    "symbols_per_read": 2,
                    "raw_edge_scans_per_read": 128,
                }
                if args.read_reference_hints
                else None
            ),
            "BM25": False,
            "Jev": False,
            "model": "claude-opus-5-5",
            "max_turns": 20,
            "case_guard_usd": 0.75,
            "subscription_auth": auth,
            "sdk_version": sdk_version,
            "cli_version": cli_version,
            "python": sys.executable,
            "new_API_usage_usd": 0,
            "live": args.live,
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
    started = time.perf_counter()
    ctx = None
    binding = None
    try:
        row["snapshot_before"] = claude.native.audit_contextbench_snapshot(
            repo, record["base_commit"]
        )
        manifest, trust_evidence = prepare_manifest(args, record, repo, languages)
        # The selected view is either this build or the explicitly checked local
        # producer above. Retain native byte authorization and source binding.
        entry = manifest.indexes["vector"]
        with capture_authenticated_vector_view(entry.path) as view:
            authorization = _mint_trusted_local_admin_authorization(
                view.ownership,
                view_type="vector",
                semantic_contract=entry.config,
                evidence=(
                    trust_evidence,
                    "captured-vector-tree-subject",
                ),
            )
            binding = capture_repository_source(
                repo,
                selection=manifest.source_selection,
            )
            ctx = ServerContext.load(
                manifest,
                views=["vector"] if args.vector_only else ["vector", "symbol_graph"],
                native_index_authorization=authorization,
                source_binding=binding,
            )
        if ctx.vector is None:
            raise ValueError(f"Dense view did not load: {ctx.errors}")
        tool = make_context_tool(ctx)
        hints = ContextHints(ctx, repo=repo) if args.read_reference_hints else None
        row["indexes"] = {
            "loaded": sorted(ctx.loaded_views),
            "errors": ctx.errors,
            "source_verified": ctx.source_verified,
            "preparation_seconds": time.perf_counter() - started,
        }
        # Separate local preflight evidence from source delivered to the agent.
        preflight = tool.executor_fn(query=record["problem_statement"], budget="fast")
        write_json(args.output / "local-preflight.json", json.loads(preflight))
        if not any(node.get("content") for node in json.loads(preflight)[1:]):
            raise ValueError("Core preflight returned no verified source")
        # Preflight bodies are local engineering checks, never model inputs.
        # Only actual surface tool calls issue handles in the agent session.
        if args.surface_context:
            probe = SurfaceContext(ctx, observed_messages=lambda: ())
            write_json(
                args.output / "surface-preflight.json",
                json.loads(probe.execute(query=record["problem_statement"])),
            )
            # Local preflight handles are not issued into the model session.
            surface = SurfaceContext(
                ctx, observed_messages=lambda: tuple(row["messages"])
            )
            tool = surface.tool()
        row["indexes"]["model_ready_seconds"] = time.perf_counter() - started
        row["indexes"]["local_preflight_seconds"] = (
            row["indexes"]["model_ready_seconds"]
            - row["indexes"]["preparation_seconds"]
        )
        if args.live:
            claude.native.audit_contextbench_snapshot(repo, record["base_commit"])
            intent = args.output / "model-intent.json"
            with intent.open("x") as stream:
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
                        context_tool=tool,
                        read_hints=hints,
                        context_guidance=(
                            SURFACE_GUIDANCE if args.surface_context else None
                        ),
                    ),
                    timeout=600,
                )
            )
            row["snapshot_after"] = claude.native.audit_contextbench_snapshot(
                repo, record["base_commit"]
            )
            # As in the control, only tool replies followed by assistant output
            # are confirmed as consumed; preserve the full raw trajectory too.
            confirmed = [
                m
                for i, m in enumerate(row["messages"])
                if m["role"] != "tool"
                or any(
                    later["role"] == "assistant" for later in row["messages"][i + 1 :]
                )
            ]
            prediction = claude.native.contextbench_prediction(
                instance_id=record["instance_id"], messages=confirmed, repo_path=repo
            )
            write_json(args.output / "prediction.json", prediction)
        else:
            row["snapshot_after"] = claude.native.audit_contextbench_snapshot(
                repo, record["base_commit"]
            )
            row["status"] = "prepared_no_model_call"
        assert all(
            digest(ROOT / name) == expected for name, expected in implementation.items()
        )
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
    parser.add_argument("--embedding-model", required=True)
    parser.add_argument("--embedding-revision", required=True)
    parser.add_argument("--reuse-from", type=Path)
    parser.add_argument("--read-reference-hints", action="store_true")
    parser.add_argument("--surface-context", action="store_true")
    parser.add_argument("--vector-only", action="store_true")
    run(parser.parse_args())
