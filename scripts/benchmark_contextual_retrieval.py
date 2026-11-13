#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Frozen, source-only contextual-BM25 development pilot.

Preparation never calls a model. Generation consumes only prompt records;
query/gold records are consumed separately by scoring.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path, PurePosixPath

from filelock import FileLock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codenib.compiler.index_builders import BM25IndexBuilder
from codenib.index.contextual import ContextualizationConfig, FileContextualizer
from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer
from codenib.llm.litellm_chat import LiteLLMChat, RetryConfig
from codenib.llm.usage import UsageTracker
from codenib.source_fingerprint import capture_repository_source
from codenib.types import NODE_TYPE_FUNCTION, NODE_TYPE_METHOD
from scripts.benchmark_jev_retrieval import quality


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def request_key(messages, options):
    return digest(
        json.dumps({"messages": messages, "options": options}, sort_keys=True)
    )


def client(config, api_key=None):
    return LiteLLMChat(
        model=config["model"],
        api_key=api_key,
        temperature=None,
        extra_kwargs=config["model_options"],
        retry=RetryConfig(max_retries=0),
    )


def read_config(study):
    config = json.loads((study / "config.json").read_text())
    manifest = json.loads((study / "manifest.json").read_text())
    if digest(json.dumps(config, sort_keys=True)) != manifest["config_sha256"]:
        raise ValueError("study configuration changed after preparation")
    return config, manifest


def read_requests(study, manifest):
    text = (study / "requests.jsonl").read_text()
    if digest(text) != manifest["requests_sha256"]:
        raise ValueError("source prompt records changed after preparation")
    requests = [json.loads(line) for line in text.splitlines()]
    if any(r["key"] != request_key(r["messages"], r["options"]) for r in requests):
        raise ValueError("source prompt identity does not match its content")
    if len({r["key"] for r in requests}) != len(requests):
        raise ValueError("duplicate source prompt identity")
    return requests


def read_cases(study, manifest):
    text = (study / "cases.json").read_text()
    if digest(text) != manifest["cases_sha256"]:
        raise ValueError("evaluation cases changed after preparation")
    return json.loads(text)


def verify_arms(study, arms):
    """Only ranking metadata may vary; source text, identities and spans match."""
    baseline = json.loads((study / "B0" / "documents.json").read_text())
    for arm in arms:
        documents = json.loads((study / arm / "documents.json").read_text())
        canonical = [
            {
                **d,
                "metadata": {
                    k: v for k, v in d["metadata"].items() if k != "contextualization"
                },
            }
            for d in documents
        ]
        if canonical != baseline:
            raise ValueError("comparison arms differ in source chunks or metadata")
    return baseline


def context_inventory(source):
    """Parse query-independent file context once, outside per-chunk ranking."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    header = ast.get_docstring(tree, clean=False) or ""
    lines = source.splitlines()
    definitions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    definitions = [
        (node.lineno - 1, lines[node.lineno - 1], ast.get_docstring(node, clean=False))
        for node in definitions
    ]
    imports = [
        ast.get_source_segment(source, n) or ""
        for n in tree.body
        if isinstance(n, (ast.Import, ast.ImportFrom))
    ]
    return header + "\n" + "\n".join(imports), definitions


def deterministic_context(source, start_line, max_chars, inventory=None):
    """Module description plus nearby definition signatures/docstrings, no query."""
    inventory = inventory if inventory is not None else context_inventory(source)
    if inventory is None:
        return source[:max_chars]
    intro, definitions = inventory
    ordered = sorted(definitions, key=lambda item: (abs(item[0] - start_line), item[0]))
    result = intro[: max_chars // 2] + "\n"
    first = True
    for line, signature, description in ordered:
        if line == start_line:
            continue
        for part in (signature, description):
            if not part:
                continue
            piece = ("" if first else "\n") + part
            result += piece[: max_chars - len(result)]
            first = False
            if len(result) >= max_chars:
                return result
    return result[:max_chars]


def snapshot(repo, ref, subdir, output):
    subdirs = [subdir] if isinstance(subdir, str) else subdir
    for value in subdirs:
        subtree = PurePosixPath(value)
        if subtree.is_absolute() or ".." in subtree.parts or not subtree.parts:
            raise ValueError("source subtree must be repository-relative")
    commit = subprocess.check_output(
        ["git", "rev-parse", ref + "^{commit}"], cwd=repo, text=True
    ).strip()
    payload = subprocess.check_output(
        ["git", "archive", "--format=tar", commit, "--", *subdirs], cwd=repo
    )
    output.mkdir()
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        for member in archive:
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("unsafe source archive path")
            if not member.isfile():
                continue
            target = output.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open("xb") as dest:
                dest.write(source.read())
    return commit


def verify_snapshot(repo, commit, subdir, output):
    """Authenticate an existing preparation snapshot against immutable Git blobs."""
    subdirs = [subdir] if isinstance(subdir, str) else subdir
    payload = subprocess.check_output(
        ["git", "archive", "--format=tar", commit, "--", *subdirs], cwd=repo
    )
    expected = set()
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        for member in archive:
            if not member.isfile():
                continue
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("unsafe source archive path")
            path = output.joinpath(*relative.parts)
            with archive.extractfile(member) as original:
                if path.is_symlink() or path.read_bytes() != original.read():
                    raise ValueError("existing snapshot differs from the pinned source")
            expected.add(relative.as_posix())
    actual = {
        p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()
    }
    if actual != expected:
        raise ValueError("existing snapshot file inventory differs from pinned source")


def prepare(args):
    config = json.loads(args.config.read_text())
    output = args.output.resolve()
    source_location = output / "source-location.json"
    if args.resume_source:
        if (output / "manifest.json").exists() or (output / "responses.jsonl").exists():
            raise ValueError("cannot replace an already frozen or generated study")
        commit = subprocess.check_output(
            ["git", "rev-parse", args.ref + "^{commit}"], cwd=args.repo, text=True
        ).strip()
        source_root = Path(json.loads(source_location.read_text())["path"])
        verify_snapshot(args.repo.resolve(), commit, args.subdir, source_root)
    else:
        output.mkdir(parents=True, exist_ok=False)
        if args.source_snapshot:
            source_root = args.source_snapshot.resolve(strict=True)
            commit = subprocess.check_output(
                ["git", "rev-parse", args.ref + "^{commit}"], cwd=args.repo, text=True
            ).strip()
            verify_snapshot(args.repo.resolve(), commit, args.subdir, source_root)
        else:
            source_root = (
                Path(tempfile.mkdtemp(prefix="codenib-contextual-pilot-")).resolve()
                / "source"
            )
            commit = snapshot(args.repo.resolve(), args.ref, args.subdir, source_root)
        write_json(source_location, {"path": str(source_root.resolve())})
    cases_text = subprocess.check_output(
        ["git", "show", commit + ":" + args.cases], cwd=args.repo, text=True
    )
    cases = json.loads(cases_text)
    if config.get("case_ids"):
        selected = set(config["case_ids"])
        cases = [case for case in cases if case["id"] in selected]
        if {case["id"] for case in cases} != selected:
            raise ValueError("configured case IDs do not resolve")
    for case in cases:
        case["cohort"] = "existing_natural_language"
    write_json(output / "cases.json", cases)
    write_json(output / "config.json", config)
    context_config = ContextualizationConfig(**config["context"])
    contextualizer = FileContextualizer(client(config), context_config)
    builder = BM25IndexBuilder(**config["builder"])
    requests = []
    started = time.perf_counter()
    with (
        capture_repository_source(source_root) as source,
        source.read_session(),
    ):
        prepared = builder.prepare_from_repository_source(
            "pilot", repository_source=source
        )
        prepared.indexer.save_index(str(output / "B0"))
        documents = prepared.indexer.documents
        from codenib.code_chunker import CodeChunker, RepoChunkingConfig

        chunker = CodeChunker(
            language="python",
            max_lines_per_chunk=builder.max_lines_per_chunk,
            repo_config=RepoChunkingConfig(
                languages=builder.languages, filter_tests=False
            ),
            include_header_epilogue=True,
        )
        chunks = chunker.chunk_repository_source(source)
        candidates = {}
        for chunk in chunks:
            if chunk.chunk_type in {
                NODE_TYPE_FUNCTION,
                NODE_TYPE_METHOD,
            } and not chunk.name.rsplit(".", 1)[-1].startswith("_"):
                candidates.setdefault(chunk.node_id, chunk)
        selected_controls = sorted(
            candidates, key=lambda n: digest(config["control_seed"] + n)
        )[: config.get("exact_controls", 0)]
        for number, node in enumerate(selected_controls, 1):
            chunk = candidates[node]
            relative = chunk.file
            if Path(relative).is_absolute():
                relative = Path(relative).relative_to(source_root).as_posix()
            cases.append(
                {
                    "id": f"exact_symbol_{number:02}",
                    "query": chunk.name,
                    "required_path_groups": [[relative]],
                    "expected_node_id": node,
                    "cohort": "metadata_derived_exact_symbol",
                }
            )
        write_json(output / "cases.json", cases)
        files = {}
        parsed_files = {}
        contexts = {}
        invalid = []
        seen = set()
        for chunk in chunks:
            key = (chunk.node_id, chunk.start_line, chunk.end_line)
            if key in seen or chunk.chunk_type not in {
                NODE_TYPE_FUNCTION,
                NODE_TYPE_METHOD,
            }:
                continue
            seen.add(key)
            relative = chunk.file
            if Path(relative).is_absolute():
                relative = Path(relative).relative_to(source_root).as_posix()
            if relative not in files:
                files[relative] = source.read_bytes(
                    relative, max_bytes=64 * 1024 * 1024
                ).decode("utf-8", errors="replace")
                parsed_files[relative] = context_inventory(files[relative])
            contexts[key] = deterministic_context(
                files[relative],
                chunk.start_line,
                context_config.max_prefix_chars,
                parsed_files[relative],
            )
            try:
                file_prefix = (
                    files[relative]
                    .encode("utf-8")[: context_config.max_input_chars * 4]
                    .decode("utf-8", errors="replace")
                )
                messages, context_chars = contextualizer._messages(
                    chunk, relative, file_prefix
                )
            except ValueError as error:
                invalid.append({"node_id": chunk.node_id, "reason": str(error)})
                continue
            options = {
                "max_tokens": context_config.max_output_tokens,
                "temperature": context_config.temperature,
            }
            requests.append(
                {
                    "key": request_key(messages, options),
                    "messages": messages,
                    "options": options,
                    "node_id": chunk.node_id,
                    "file_context_chars": context_chars,
                }
            )
        for document in documents:
            metadata = document.metadata
            prefix = contexts.get(
                (metadata["node_id"], metadata["start_line"], metadata["end_line"])
            )
            if prefix is not None:
                metadata["contextualization"] = {
                    "method": "deterministic-file-context-v1",
                    "prefix": prefix,
                    "index_prefix": prepared.indexer._apply_stemming(prefix),
                }
        prepared.indexer.save_index(str(output / "B1"))
        source_identity = source.authenticated_identity_snapshot()
    with (output / "requests.jsonl").open("w") as handle:
        for request in requests:
            handle.write(json.dumps(request, ensure_ascii=False) + "\n")
    manifest = {
        "source_commit": commit,
        "source_root": str(source_root.resolve()),
        "source_subdir": args.subdir,
        "source_fingerprint": source_identity.fingerprint,
        "config_sha256": digest(json.dumps(config, sort_keys=True)),
        "source_cases_sha256": digest(cases_text),
        "cases_sha256": digest((output / "cases.json").read_text()),
        "requests_sha256": digest((output / "requests.jsonl").read_text()),
        "case_count": len(cases),
        "case_exposure": "development: existing published CodeNib questions, not held out",
        "documents": len(documents),
        "contextualized_chunks": len(requests),
        "invalid_targets": invalid,
        "source_files": len(files),
        "prompt_characters": sum(
            sum(len(m["content"]) for m in r["messages"]) for r in requests
        ),
        "prefix_policy": contextualizer.artifact_identity(),
        "preparation_seconds": time.perf_counter() - started,
    }
    write_json(output / "manifest.json", manifest)
    print(
        json.dumps(
            {
                k: manifest[k]
                for k in (
                    "documents",
                    "contextualized_chunks",
                    "case_count",
                    "source_files",
                    "prompt_characters",
                )
            },
            indent=2,
        )
    )
    print("Invalid complete targets:", len(invalid))


class PilotBudget:
    """One-process conservative reservation meter; unknown calls keep their ceiling."""

    def __init__(self, limit, spent="0"):
        self.limit = Decimal(str(limit))
        self.spent = Decimal(str(spent))
        self.reserved = Decimal("0")
        self.lock = threading.Lock()
        if self.limit <= 0 or self.spent < 0 or self.spent > self.limit:
            raise ValueError("invalid pilot budget")

    def reserve(self, amount):
        amount = Decimal(str(amount))
        with self.lock:
            if amount <= 0 or self.spent + self.reserved + amount > self.limit:
                raise RuntimeError("pilot spending cap would be exceeded")
            self.reserved += amount
        return amount

    def finish(self, reservation, known_cost=None):
        charge = reservation if known_cost is None else Decimal(str(known_cost))
        with self.lock:
            if charge < 0 or charge > reservation:
                raise RuntimeError("provider usage exceeds the reserved ceiling")
            self.reserved -= reservation
            self.spent += charge
        return charge


def generate(args):
    """Record a reservation before each paid call; never retry a saved response.

    FileLock admits one generation process per study. An unfinished reservation
    remains charged at its ceiling on recovery, owned by this CLI's next run.
    No queries or annotations are read by the generation stage.
    """
    study = args.study.resolve()
    config, manifest = read_config(study)
    context_config = ContextualizationConfig(**config["context"])
    if manifest["invalid_targets"]:
        raise ValueError("strict contextual profile has oversized complete targets")
    key = args.credential_file.read_text().strip()
    chat = client(config, key)
    if (
        FileContextualizer(
            chat, ContextualizationConfig(**config["context"])
        ).artifact_identity()
        != manifest["prefix_policy"]
    ):
        raise ValueError("generation model/profile differs from preparation")
    requests = read_requests(study, manifest)
    responses_path = study / "responses.jsonl"
    ledger_path = study / "cost-ledger.jsonl"
    with FileLock(str(study / "generation.lock"), timeout=0):
        responses = (
            [json.loads(line) for line in responses_path.open()]
            if responses_path.exists()
            else []
        )
        existing = {row["key"] for row in responses if row["status"] == "response"}
        events = (
            [json.loads(line) for line in ledger_path.open()]
            if ledger_path.exists()
            else []
        )
        reserved = {
            e.get("attempt_id", e["key"]): Decimal(e["upper_usd"])
            for e in events
            if e["event"] == "reserved"
        }
        finished = {
            e.get("attempt_id", e["key"]): Decimal(e["charged_ceiling_usd"])
            for e in events
            if e["event"] == "finished"
        }
        unresolved = set(reserved) - set(finished)
        unresolved_keys = {
            e["key"] for e in events if e.get("attempt_id", e["key"]) in unresolved
        }
        spent = (
            Decimal(config["budget"].get("initial_spent_ceiling_usd", "0"))
            + sum(finished.values(), Decimal("0"))
            + sum((reserved[k] for k in unresolved), Decimal("0"))
        )
        budget = PilotBudget(config["budget"]["max_usd"], spent)
        counts = Counter(r["key"] for r in responses)
        permanent = {
            r["key"]
            for r in responses
            if r["status"] == "error"
            and not (
                r.get("http_status") in {429, 500, 502, 503, 504}
                or r.get("error_type")
                in {"Timeout", "TimeoutError", "APIConnectionError"}
            )
        }
        pending = [
            r
            for r in requests
            if r["key"] not in existing
            and r["key"] not in unresolved_keys
            and r["key"] not in permanent
            and counts[r["key"]] < 3
        ]
        if args.limit:
            pending = pending[: args.limit]
        lock = threading.Lock()
        stop = threading.Event()
        started = time.perf_counter()
        input_rate = Decimal(config["budget"]["cache_write_per_million"]) / 1_000_000
        output_rate = Decimal(config["budget"]["output_per_million"]) / 1_000_000

        def append(path, value):
            with lock:
                with path.open("a") as handle:
                    handle.write(json.dumps(value, ensure_ascii=False) + "\n")
                    handle.flush()

        def run_one(request):
            if stop.is_set():
                return
            byte_count = sum(
                len(m["content"].encode("utf-8")) for m in request["messages"]
            )
            token_ceiling = byte_count * 2 + 4096
            if token_ceiling > 100_000:
                stop.set()
                raise ValueError(
                    "request exceeds the frozen short-context pricing tier"
                )
            upper = (
                token_ceiling * input_rate
                + request["options"]["max_tokens"] * output_rate
            )
            try:
                reservation = budget.reserve(upper)
            except RuntimeError:
                stop.set()
                return
            attempt_id = request["key"] + ":" + str(counts[request["key"]] + 1)
            append(
                ledger_path,
                {
                    "event": "reserved",
                    "key": request["key"],
                    "attempt_id": attempt_id,
                    "upper_usd": str(upper),
                },
            )
            tracker = UsageTracker()
            record = {
                "key": request["key"],
                "attempt_id": attempt_id,
                "node_id": request["node_id"],
            }
            start = time.perf_counter()
            known = None
            try:
                response = chat._call_raw(
                    request["messages"],
                    **request["options"],
                    num_retries=0,
                    usage_tracker=tracker,
                )
                prefix = (response.choices[0].message.content or "").strip()
                usage = response.usage.model_dump()
                prompt = int(usage.get("prompt_tokens", 0))
                completion = int(usage.get("completion_tokens", 0))
                if prompt <= 0 or completion < 0:
                    raise RuntimeError("provider did not report usable token counts")
                known = prompt * input_rate + completion * output_rate
                try:
                    context_config.validate_prefix(prefix)
                    valid = True
                except ValueError:
                    valid = False
                headers = (
                    getattr(response, "_hidden_params", {}).get(
                        "additional_headers", {}
                    )
                    or {}
                )
                rate_headers = (
                    {
                        str(k): str(v)
                        for k, v in headers.items()
                        if "ratelimit" in str(k).lower()
                    }
                    if isinstance(headers, dict)
                    else {}
                )
                record.update(
                    status="response",
                    prefix=prefix,
                    valid_prefix=valid,
                    usage=usage,
                    returned_model=response.model,
                    finish_reason=response.choices[0].finish_reason,
                    rate_headers=rate_headers,
                )
            except Exception as error:
                record.update(
                    status="error",
                    error_type=type(error).__name__,
                    http_status=getattr(error, "status_code", None),
                )
                stop.set()
            finally:
                charge = budget.finish(reservation, known)
                record.update(
                    elapsed_ms=(time.perf_counter() - start) * 1000,
                    charged_ceiling_usd=str(charge),
                )
                append(responses_path, record)
                append(
                    ledger_path,
                    {
                        "event": "finished",
                        "key": request["key"],
                        "attempt_id": attempt_id,
                        "charged_ceiling_usd": str(charge),
                    },
                )

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            list(executor.map(run_one, pending))
        all_responses = (
            [json.loads(line) for line in responses_path.open()]
            if responses_path.exists()
            else []
        )
        summary = {
            "attempts": len(all_responses),
            "responses": sum(r["status"] == "response" for r in all_responses),
            "expected": len(requests),
            "errors": sum(r["status"] == "error" for r in all_responses),
            "invalid_prefixes": sum(
                r.get("valid_prefix") is False for r in all_responses
            ),
            "conservative_spend_ceiling_usd": str(budget.spent),
            "run_wall_seconds": time.perf_counter() - started,
            "unresolved_prior_reservations": len(unresolved),
        }
        write_json(study / "generation-summary.json", summary)
        print(json.dumps(summary, indent=2))


def build_generated(args):
    """Replay frozen real responses through the production builder, with no calls."""
    study = args.study.resolve()
    config, manifest = read_config(study)
    requests = read_requests(study, manifest)
    responses = [json.loads(line) for line in (study / "responses.jsonl").open()]
    lookup = {r["key"]: r for r in responses if r["status"] == "response"}
    if any(r["key"] not in lookup for r in requests):
        raise ValueError(
            "strict contextual profile is incomplete; no fallback is scored"
        )
    identity = client(config).cache_identity

    class ReplayChat:
        cache_identity = identity

        def complete(self, messages, **kwargs):
            options = {
                "max_tokens": kwargs["max_tokens"],
                "temperature": kwargs.get("temperature"),
            }
            return lookup[request_key(messages, options)]["prefix"]

    source_root = Path(manifest["source_root"])
    verify_snapshot(
        args.repo.resolve(),
        manifest["source_commit"],
        manifest["source_subdir"],
        source_root,
    )
    builder = BM25IndexBuilder(
        **config["builder"],
        contextualizer=FileContextualizer(
            ReplayChat(), ContextualizationConfig(**config["context"])
        ),
    )
    start = time.perf_counter()
    with capture_repository_source(source_root) as source:
        result = builder.build_from_repository_source(
            "pilot", repository_source=source, output_dir=str(study / "B2")
        )
    write_json(
        study / "B2-build.json",
        {
            "seconds": time.perf_counter() - start,
            "source_fingerprint": manifest["source_fingerprint"],
            "generation_mode": "frozen real-model responses; no new inference",
            "metadata": result.metadata,
        },
    )


def score(args):
    import statistics

    import tiktoken

    study = args.study.resolve()
    config, manifest = read_config(study)
    cases = read_cases(study, manifest)
    baseline = verify_arms(study, args.arms)
    paths = {d["metadata"]["file"] for d in baseline}
    if any(
        not paths.intersection(group)
        for c in cases
        for group in c["required_path_groups"]
    ):
        raise ValueError("evaluation expects source files outside the selected corpus")
    source_root = Path(manifest["source_root"])
    verify_snapshot(
        args.repo.resolve(),
        manifest["source_commit"],
        manifest["source_subdir"],
        source_root,
    )
    tokenizer = tiktoken.get_encoding(config["evaluation"]["tokenizer"])
    rows = []
    for arm in args.arms:
        index = BM25CodeIndexer()
        index.load_index(str(study / arm))
        for case in cases:
            index.search(case["query"], top_k=config["evaluation"]["candidates"])
            timings = []
            for _ in range(10):
                start = time.perf_counter()
                index.search(case["query"], top_k=config["evaluation"]["candidates"])
                timings.append((time.perf_counter() - start) * 1000)
            hits = index.search(
                case["query"],
                top_k=config["evaluation"]["candidates"],
                return_code_content=True,
                wrap_with_ln=False,
            )
            elapsed = statistics.median(timings)
            seen, packet, tokens = set(), [], 0
            for hit in hits:
                key = (hit.file, hit.start_line, hit.end_line)
                if key in seen or not hit.content:
                    continue
                seen.add(key)
                text = f"{hit.file}:{hit.start_line+1}-{hit.end_line+1}\n{hit.content}"
                size = len(tokenizer.encode(text, disallowed_special=()))
                if tokens + size > config["evaluation"]["evidence_tokens"]:
                    continue
                packet.append(hit)
                tokens += size
            row = {
                "id": case["id"],
                "cohort": case["cohort"],
                "arm": arm,
                "query_ms": elapsed,
                "metrics": {
                    str(k): quality(case, hits, k) for k in config["evaluation"]["ks"]
                },
                "budgeted": quality(case, packet, len(packet)),
                "delivered_tokens": tokens,
                "hits": [
                    {
                        "node_id": h.node_id,
                        "file": h.file,
                        "start_line": h.start_line,
                        "end_line": h.end_line,
                    }
                    for h in hits[:20]
                ],
                "packet_files": list(dict.fromkeys(h.file for h in packet)),
            }
            if case.get("expected_node_id"):
                row["exact_symbol"] = {
                    str(k): float(
                        any(h.node_id == case["expected_node_id"] for h in hits[:k])
                    )
                    for k in config["evaluation"]["ks"]
                }
            rows.append(row)
    summaries = {}
    cohorts = {}
    for arm in args.arms:
        selected = [r for r in rows if r["arm"] == arm]
        summaries[arm] = {
            "cases": len(selected),
            "coverage_at_5": statistics.mean(
                r["metrics"]["5"]["coverage"] for r in selected
            ),
            "all_groups_at_5": statistics.mean(
                r["metrics"]["5"]["success"] for r in selected
            ),
            "mrr": statistics.mean(r["metrics"]["5"]["mrr"] for r in selected),
            "budgeted_coverage": statistics.mean(
                r["budgeted"]["coverage"] for r in selected
            ),
            "query_median_ms": statistics.median(r["query_ms"] for r in selected),
        }
        for cohort in sorted({r["cohort"] for r in selected}):
            items = [r for r in selected if r["cohort"] == cohort]
            cohorts.setdefault(cohort, {})[arm] = {
                "cases": len(items),
                "coverage_at_5": statistics.mean(
                    r["metrics"]["5"]["coverage"] for r in items
                ),
                "all_groups_at_5": statistics.mean(
                    r["metrics"]["5"]["success"] for r in items
                ),
                "mrr_at_5": statistics.mean(r["metrics"]["5"]["mrr"] for r in items),
                "budgeted_coverage": statistics.mean(
                    r["budgeted"]["coverage"] for r in items
                ),
                "query_median_ms": statistics.median(r["query_ms"] for r in items),
            }
            if all("exact_symbol" in r for r in items):
                cohorts[cohort][arm]["exact_symbol_at_1"] = statistics.mean(
                    r["exact_symbol"]["1"] for r in items
                )
                cohorts[cohort][arm]["exact_symbol_at_5"] = statistics.mean(
                    r["exact_symbol"]["5"] for r in items
                )
    verify_snapshot(
        args.repo.resolve(),
        manifest["source_commit"],
        manifest["source_subdir"],
        source_root,
    )
    write_json(
        study / "scores.json", {"summary": summaries, "cohorts": cohorts, "rows": rows}
    )
    print(json.dumps(cohorts, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--repo", type=Path, required=True)
    prep.add_argument("--ref", required=True)
    prep.add_argument("--subdir", nargs="+", required=True)
    prep.add_argument("--cases", required=True)
    prep.add_argument("--config", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--resume-source", action="store_true")
    prep.add_argument("--source-snapshot", type=Path)
    prep.set_defaults(func=prepare)
    scoring = sub.add_parser("score")
    scoring.add_argument("--study", type=Path, required=True)
    scoring.add_argument("--repo", type=Path, default=ROOT)
    scoring.add_argument("--arms", nargs="+", choices=["B0", "B1", "B2"], required=True)
    scoring.set_defaults(func=score)
    generation = sub.add_parser("generate")
    generation.add_argument("--study", type=Path, required=True)
    generation.add_argument("--credential-file", type=Path, required=True)
    generation.add_argument("--workers", type=int, default=4)
    generation.add_argument("--limit", type=int)
    generation.set_defaults(func=generate)
    building = sub.add_parser("build-generated")
    building.add_argument("--study", type=Path, required=True)
    building.add_argument("--repo", type=Path, default=ROOT)
    building.set_defaults(func=build_generated)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
