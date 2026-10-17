#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Source-checkout pilot of real CodeNib context, without repair or API fallback.

Each invocation owns one fresh output directory and one model intent. It builds
only vector and symbol-graph views with the existing compiler, then uses the
existing SDK retrieval loop. Run on disclosed development tasks before claiming
independent benchmark gains. An interrupted model intent is never retried here.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
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
from codenib.index.embedding.artifact_integrity import capture_authenticated_vector_view
from codenib.mcp.context import ServerContext
from codenib.native_index_authorization import _mint_trusted_local_admin_authorization
from codenib.source_fingerprint import capture_repository_source
from scripts import benchmark_context_claude as claude
from scripts.benchmark_jev_base import write_json
from scripts.contextbench_codenib_context import make_context_tool


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args):
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
    auth = claude.subscription_auth(cli) if args.live else None
    if args.live and (
        auth.get("subscriptionType") != "max" or auth.get("apiProvider") != "firstParty"
    ):
        raise ValueError("This pilot requires existing first-party Max inclusion")
    args.output.mkdir(parents=True, exist_ok=False)
    languages = (
        ["javascript", "typescript"]
        if record["language"] in {"javascript", "typescript"}
        else [record["language"]]
    )
    implementation = {
        str(path.relative_to(ROOT)): digest(path)
        for path in [
            Path(__file__).resolve(),
            ROOT / "scripts/contextbench_codenib_context.py",
            ROOT / "scripts/benchmark_context_claude.py",
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
            "embedding_model": args.embedding_model,
            "embedding_revision": args.embedding_revision,
            "embedding_dimension": 768,
            "embedding_max_seq_length": 1024,
            "languages": languages,
            "local_model_cache": os.environ.get("HF_HUB_CACHE"),
            "offline_embeddings": os.environ.get("HF_HUB_OFFLINE") == "1",
            "views": ["vector", "symbol_graph"],
            "BM25": False,
            "Jev": False,
            "model": "claude-opus-5-5",
            "max_turns": 20,
            "case_guard_usd": 0.75,
            "subscription_auth": auth,
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
                index_types=["vector", "symbol_graph"],
                languages=languages,
            ),
        )
        manifest = compiler.compile_repo(
            str(repo), index_types=["vector"], cache_dir=str(args.output / "indexes")
        )
        write_json(args.output / "index-outcomes.json", manifest.to_dict())
        # Only this invocation's captured compiler output is authorized for FAISS.
        entry = manifest.indexes["vector"]
        if not manifest.index_is_current("vector"):
            raise ValueError("Dense index build failed; do not substitute native tools")
        manifest = compiler.compile_repo(
            str(repo),
            index_types=["symbol_graph"],
            cache_dir=str(args.output / "indexes"),
        )
        write_json(args.output / "index-outcomes.json", manifest.to_dict())
        entry = manifest.indexes["vector"]
        with capture_authenticated_vector_view(entry.path) as view:
            authorization = _mint_trusted_local_admin_authorization(
                view.ownership,
                view_type="vector",
                semantic_contract=entry.config,
                evidence=(
                    "contextbench-pilot-owned-build",
                    "captured-vector-tree-subject",
                ),
            )
            binding = capture_repository_source(
                repo,
                selection=manifest.source_selection,
            )
            ctx = ServerContext.load(
                manifest,
                views=["vector", "symbol_graph"],
                native_index_authorization=authorization,
                source_binding=binding,
            )
        if ctx.vector is None:
            raise ValueError(f"Dense view did not load: {ctx.errors}")
        tool = make_context_tool(ctx)
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
    run(parser.parse_args())
