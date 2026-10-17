# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Source-checkout, one-hop navigation over a verified compiler graph.

The caller owns snapshot and graph-digest validation. This tool neither builds
an index nor invents relationships. Reference edges are not runtime call traces.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tiktoken

from codenib.agent.runner import _MAX_RESULT_CHARS
from codenib.agent.tools.spec import ToolInputSpec, ToolSpec
from codenib.graph.traverse_graph import NeighborScanBudget, RepoDependencySearcher
from codenib.languages import normalize_graph_language
from codenib.types import (
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    is_symbol_node,
    node_has_definition,
)

GRAPH_QUERY_POLICIES = (
    "first_callable_source_reference_hop",
    "late_issue_reference_hop",
)


def prepare_graphs(args):
    """Bind local compiler artifacts to the exact selected source snapshots.

    Reuse only the authenticated historical development cohort, or build with
    the existing bounded private-checkout worker. No labels enter graph building.
    Missing graphs remain explicit records in the selected population.
    """
    from codenib.dataset.contextbench import cases_digest, load_contextbench_cases
    from scripts import benchmark_context_agent as native
    from scripts.benchmark_context_serving import bounded_graph
    from scripts.benchmark_jev_base import digest, write_json

    preparation = getattr(args, "allow_project_preparation", False)
    if preparation and args.reuse_cohort:
        raise ValueError("Project preparation requires fresh graph builds")
    manifest, cases = native.load_inputs(args.prepared)
    prior, provenance = {}, None
    if args.reuse_cohort:
        root = args.reuse_cohort.resolve()
        config = json.loads((root / "selection.json").read_text())
        receipt = json.loads((root / "prepared.json").read_text())
        selected = load_contextbench_cases(root / "selected.jsonl")
        if (
            cases_digest(selected) != config["case_digest"]
            or digest(root / "selection.json") != receipt["selection_sha256"]
        ):
            raise ValueError("Historical graph selection changed")
        previous_cases = {case.instance_id: case for case in selected}
        records = {Path(row["path"]).stem: row for row in receipt["cases"]}
        for ident, case in cases.items():
            if (
                ident not in previous_cases
                or previous_cases[ident].to_record() != case.to_record()
                or ident not in records
            ):
                raise ValueError("Historical graph task inputs differ")
            record = records[ident]
            path = (root / record["path"]).resolve()
            if not path.is_relative_to(root) or digest(path) != record["sha256"]:
                raise ValueError("Historical graph source record changed")
            source = json.loads(path.read_text())
            native.audit_contextbench_snapshot(Path(source["repo"]), case.base_commit)
            prior[ident] = source.get("graph", {"available": False})
        provenance = {
            "cohort": str(root),
            "prepared_sha256": digest(root / "prepared.json"),
            "selection_sha256": receipt["selection_sha256"],
        }
    args.output.mkdir(parents=True, exist_ok=False)
    records = []
    for entry in manifest["cases"]:
        case = cases[entry["instance_id"]]
        source = json.loads(Path(entry["source"]).read_text())
        record = {"instance_id": case.instance_id, "available": False}
        if source.get("status") != "ok":
            record["error"] = "Source preparation failed"
        else:
            repo = Path(source["repo"])
            snapshot = native.audit_contextbench_snapshot(repo, case.base_commit)
            output = args.output / case.instance_id
            if args.reuse_cohort:
                record.update(prior[case.instance_id])
                if record["available"]:
                    path = root / "graphs" / case.instance_id / "graph.pkl"
                    data = path.read_bytes()
                    if hashlib.sha256(data).hexdigest() != record["sha256"]:
                        raise ValueError("Historical graph bytes changed")
                    output.mkdir()
                    (output / "graph.pkl").write_bytes(data)
            else:
                record.update(
                    bounded_graph(
                        repo,
                        normalize_graph_language(case.language),
                        output,
                        args.timeout,
                        allow_project_preparation=preparation,
                    )
                )
            if record["available"]:
                record["path"] = f"{case.instance_id}/graph.pkl"
            record["snapshot"] = snapshot
            native.audit_contextbench_snapshot(repo, case.base_commit)
        records.append(record)
        print(
            json.dumps({"case": case.instance_id, "graph": record["available"]}),
            flush=True,
        )
    write_json(
        args.output / "manifest.json",
        {
            "source_manifest_sha256": digest(args.prepared / "manifest.json"),
            "reuse": provenance,
            "build_policy": (
                None
                if provenance
                else {
                    "allow_project_preparation": preparation,
                    "timeout_seconds": args.timeout,
                }
            ),
            "cases": records,
            "implementation": native.freeze(args.output),
            "scope": "Source-bound compiler artifacts; unavailable graphs stay selected",
        },
    )


def load_graph_records(graph_prepared, prepared, cases):
    from scripts.benchmark_jev_base import digest

    path = graph_prepared / "manifest.json"
    manifest = json.loads(path.read_text())
    if manifest["source_manifest_sha256"] != digest(prepared / "manifest.json"):
        raise ValueError("Graph artifacts refer to a different source manifest")
    for name, sha in manifest["implementation"].items():
        if digest(graph_prepared / "implementation" / name) != sha:
            raise ValueError("Archived graph preparation changed")
    records = {entry["instance_id"]: entry for entry in manifest["cases"]}
    if len(records) != len(manifest["cases"]) or set(records) != set(cases):
        raise ValueError("Graph preparation must retain all selected tasks")
    for entry in records.values():
        if entry["available"]:
            artifact = (graph_prepared / entry["path"]).resolve()
            if (
                not artifact.is_relative_to(graph_prepared.resolve())
                or digest(artifact) != entry["sha256"]
            ):
                raise ValueError("Frozen graph bytes changed")
    return records


def graph_options(args, cases):
    """Share one explicit graph configuration across the two agent runtimes."""
    from scripts.benchmark_jev_base import digest

    enabled = getattr(args, "graph_navigation", False)
    prepared = getattr(args, "graphs", None)
    tokens = getattr(args, "graph_tokens", 2048)
    source_policy = getattr(args, "graph_source_policy", "definitions")
    query_policy = getattr(args, "graph_query_policy", GRAPH_QUERY_POLICIES[0])
    if not 1 <= tokens <= 8192:
        raise ValueError("Graph token budget must be between 1 and 8192")
    if source_policy not in {"definitions", "unseen"}:
        raise ValueError("Unknown graph source policy")
    if query_policy not in GRAPH_QUERY_POLICIES:
        raise ValueError("Unknown graph query policy")
    if enabled and (not prepared or args.source_policy != "functions"):
        raise ValueError(
            "Graph navigation requires functions policy and prepared graphs"
        )
    records = load_graph_records(prepared, args.prepared, cases) if prepared else {}
    return records, {
        "enabled": enabled,
        "prepared": str(prepared.resolve()) if prepared else None,
        "manifest_sha256": digest(prepared / "manifest.json") if prepared else None,
        "tokens": tokens,
        "policy": query_policy,
        "source_policy": source_policy,
    }


def graph_arguments(parser):
    parser.add_argument(
        "--graphs", type=Path, help="Frozen source-bound graph artifacts"
    )
    parser.add_argument("--graph-navigation", action="store_true")
    parser.add_argument("--graph-tokens", type=int, default=2048)
    parser.add_argument(
        "--graph-query-policy",
        choices=GRAPH_QUERY_POLICIES,
        default=GRAPH_QUERY_POLICIES[0],
    )
    parser.add_argument(
        "--graph-source-policy",
        choices=("definitions", "unseen"),
        default="definitions",
    )


def load_navigation(
    record,
    graph_prepared,
    repo,
    *,
    token_budget=2048,
    source_policy="definitions",
    observed_source=None,
):
    from codenib.graph.code_graph import CodeGraph

    graph = None
    if record["available"]:
        path = (graph_prepared / record["path"]).resolve()
        if not path.is_relative_to(graph_prepared.resolve()):
            raise ValueError("Graph path escapes prepared artifacts")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError("Frozen graph bytes changed")
        # Decode the authenticated bytes, without reopening a mutable pathname.
        graph = CodeGraph.load_graph_stream(io.BytesIO(data), input_label=str(path))
    return GraphNavigation(
        graph,
        repo,
        token_budget=token_budget,
        source_policy=source_policy,
        observed_source=observed_source,
    )


class GraphNavigation:
    """Read exact definitions along one directed reference hop, including tests.

    No file hubs or containment edges participate. Scan at most ``edge_limit``
    raw edges and return at most the requested number of unique definitions.
    Within that bounded scan, callable definitions precede other symbols so
    fields and parameters cannot consume every result slot before a callee.
    A definition's source is returned whole, or as all its missing lines under
    ``unseen``, within the response budget. Its declared range is never credited
    as read without actual numbered source. ``observed_source`` supplies verified
    NodeInfo ranges from a snapshot of delivered messages; requested tool ranges
    and graph metadata cannot mark lines observed. No mutable ledger or shared
    lock is kept here, and parallel calls may conservatively repeat source.
    """

    def __init__(
        self,
        graph,
        repo,
        *,
        token_budget=2048,
        edge_limit=1000,
        source_policy="definitions",
        observed_source=None,
    ):
        if not 1 <= token_budget <= 8192 or not 1 <= edge_limit <= 10000:
            raise ValueError("Invalid graph-navigation budget")
        self.graph = graph
        self.repo = Path(repo).resolve()
        self.token_budget = token_budget
        self.edge_limit = edge_limit
        if source_policy not in {"definitions", "unseen"}:
            raise ValueError("Unknown graph source policy")
        if source_policy == "unseen" and not callable(observed_source):
            raise ValueError("Unseen graph source requires observed source receipts")
        self.source_policy = source_policy
        self.observed_source = observed_source
        self.encoder = tiktoken.get_encoding("cl100k_base")
        self.definitions = defaultdict(list)
        if graph is not None:
            for vertex in graph.graph.vs:
                attributes = vertex.attributes()
                if (
                    is_symbol_node(attributes.get("type"))
                    and node_has_definition(attributes)
                    and isinstance(attributes.get("file"), str)
                    and type(attributes.get("start_line")) is int
                    and type(attributes.get("end_line")) is int
                    and 0 <= attributes["start_line"] <= attributes["end_line"]
                ):
                    self.definitions[attributes["file"]].append(attributes)

    def _fits(self, records):
        text = json.dumps(records, ensure_ascii=False)
        return (
            len(text) <= _MAX_RESULT_CHARS
            and len(self.encoder.encode(text, disallowed_special=()))
            <= self.token_budget
        )

    def neighbors(self, file_path, line, direction="outgoing", limit=5):
        if direction not in {"incoming", "outgoing"}:
            raise ValueError("Direction must be incoming or outgoing")
        if (
            type(line) is not int
            or line < 1
            or type(limit) is not int
            or not 1 <= limit <= 10
        ):
            raise ValueError("Use a positive 1-based line and a limit from 1 to 10")
        path = (self.repo / file_path).resolve()
        if not path.is_relative_to(self.repo):
            raise ValueError("Graph source path escapes benchmark checkout")
        if self.graph is None:
            return json.dumps([{"status": "graph_unavailable", "content": ""}])
        relative = path.relative_to(self.repo).as_posix()
        seeds = [
            node
            for node in self.definitions.get(relative, [])
            if node["start_line"] <= line - 1 <= node["end_line"]
        ]
        if not seeds:
            return json.dumps([{"status": "no_definition_at_line", "content": ""}])
        # A local variable/parameter definition can occupy the queried line.
        # Prefer its enclosing callable so outgoing navigation follows that
        # callable's references instead of a leaf variable with no outgoing edge.
        callables = [
            node
            for node in seeds
            if node["type"] in {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}
        ]
        seed = min(
            callables or seeds,
            key=lambda n: (n["end_line"] - n["start_line"], n["name"]),
        )
        scanner = RepoDependencySearcher(self.graph)
        budget = NeighborScanBudget(self.edge_limit)
        records, candidates, seen = [], [], {seed["name"]}
        observed = defaultdict(set)
        if self.source_policy == "unseen":
            for node in self.observed_source():
                observed[node.file].update(range(node.start_line, node.end_line + 1))
        skipped_observed = 0
        for name, edge in scanner.iter_neighbors(
            seed["name"],
            direction="forward" if direction == "outgoing" else "backward",
            etype_filter={EDGE_TYPE_REFERENCE},
            ignore_test_file=False,
            scan_budget=budget,
        ):
            if name in seen:
                continue
            seen.add(name)
            node = self.graph.graph.vs[self.graph.name_to_vertex[name]].attributes()
            if not is_symbol_node(node.get("type")) or not node_has_definition(node):
                continue
            candidates.append((name, node, edge))
        # Preserve reference direction and edge order within each group. Ranking
        # never widens the raw-edge scan or turns a reference into a call edge.
        candidates.sort(
            key=lambda candidate: candidate[1].get("type")
            not in {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}
        )
        for name, node, edge in candidates:
            file = node.get("file")
            start, end = node.get("start_line"), node.get("end_line")
            if (
                not isinstance(file, str)
                or type(start) is not int
                or type(end) is not int
            ):
                continue
            target = (self.repo / file).resolve()
            if not target.is_relative_to(self.repo) or not target.is_file():
                continue
            relative_target = target.relative_to(self.repo).as_posix()
            if not 0 <= start <= end or end - start >= 1500:
                missing = None
            else:
                missing = [
                    number
                    for number in range(start, end + 1)
                    if number not in observed[relative_target]
                ]
            if missing == []:
                # Fully known neighbors do not consume the definition limit or
                # response budget. The shared raw-edge scan budget still applies.
                skipped_observed += 1
                continue
            record = {
                "node_id": name,
                "file": relative_target,
                "definition_start": start + 1,
                "definition_end": end + 1,
                "relationship": EDGE_TYPE_REFERENCE,
                "direction": direction,
                "anchor_file": edge[3].get("anchor_file"),
                "anchor_line": (
                    edge[3]["anchor_line"] + 1
                    if type(edge[3].get("anchor_line")) is int
                    else None
                ),
                "content": "",
                "status": "source_not_returned",
            }
            if missing is not None and target.stat().st_size <= 2_000_000:
                lines = target.read_text(errors="replace").splitlines()
                if end < len(lines):
                    candidate = {
                        **record,
                        "content": "\n".join(f"{i + 1} | {lines[i]}" for i in missing),
                        "status": "source_returned",
                    }
                    if self._fits(records + [candidate]):
                        record = candidate
            if not self._fits(records + [record]):
                break
            records.append(record)
            if len(records) >= limit:
                break
        if not records:
            records = [
                {
                    "status": (
                        "no_unseen_neighbors"
                        if skipped_observed
                        else "no_returned_neighbors"
                    ),
                    "content": "",
                }
            ]
        return json.dumps(records, ensure_ascii=False)

    def tool_spec(self):
        return ToolSpec(
            tool_id="graph_neighbors",
            executor_fn=self.neighbors,
            description="Read definitions connected by one directed compiler reference hop.",
            tool_doc=(
                "Choose an observed source file and 1-based line inside a definition. "
                "Inside a function or method, navigate from that enclosing callable. "
                "Outgoing follows its references; incoming finds definitions referencing "
                "it, including tests. Functions and methods are returned before other "
                "symbols within the bounded edge scan. These are compiler references, "
                "not a complete "
                "runtime call graph. Only numbered content has been read. A missing "
                "graph or omitted source is explicit; continue with read/grep as needed. "
                "The unseen policy skips fully observed definitions and returns only "
                "missing lines of partly observed definitions; earlier source remains "
                "in the conversation. Limit must be between 1 and 10."
            ),
            inputs=[
                ToolInputSpec("file_path", "str"),
                ToolInputSpec("line", "int"),
                ToolInputSpec(
                    "direction",
                    "str",
                    required=False,
                    default="outgoing",
                    enum=["incoming", "outgoing"],
                ),
                ToolInputSpec("limit", "int", required=False, default=5),
            ],
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    preparation = parser.add_mutually_exclusive_group()
    preparation.add_argument("--reuse-cohort", type=Path)
    preparation.add_argument(
        "--allow-project-preparation",
        action="store_true",
        help="Allow dependency/build preparation only inside each private graph clone",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 1800:
        parser.error("Graph timeout must be between 1 and 1800 seconds")
    prepare_graphs(args)


if __name__ == "__main__":
    main()
