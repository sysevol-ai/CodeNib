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
from codenib.types import EDGE_TYPE_REFERENCE, is_symbol_node, node_has_definition


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
    if not 1 <= tokens <= 8192:
        raise ValueError("Graph token budget must be between 1 and 8192")
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
    }


def graph_arguments(parser):
    parser.add_argument(
        "--graphs", type=Path, help="Frozen source-bound graph artifacts"
    )
    parser.add_argument("--graph-navigation", action="store_true")
    parser.add_argument("--graph-tokens", type=int, default=2048)


def load_navigation(record, graph_prepared, repo, *, token_budget=2048):
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
    return GraphNavigation(graph, repo, token_budget=token_budget)


class GraphNavigation:
    """Read exact definitions along one directed reference hop, including tests.

    No file hubs or containment edges participate. Scan at most ``edge_limit``
    raw edges and return at most the requested number of unique definitions.
    A definition is returned whole or with an explicit source-unavailable status;
    its declared range is never credited as read without actual numbered source.
    """

    def __init__(self, graph, repo, *, token_budget=2048, edge_limit=1000):
        if not 1 <= token_budget <= 8192 or not 1 <= edge_limit <= 10000:
            raise ValueError("Invalid graph-navigation budget")
        self.graph = graph
        self.repo = Path(repo).resolve()
        self.token_budget = token_budget
        self.edge_limit = edge_limit
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
        seed = min(seeds, key=lambda n: (n["end_line"] - n["start_line"], n["name"]))
        scanner = RepoDependencySearcher(self.graph)
        budget = NeighborScanBudget(self.edge_limit)
        records, seen = [], {seed["name"]}
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
            if (
                0 <= start <= end
                and end - start < 1500
                and target.stat().st_size <= 2_000_000
            ):
                lines = target.read_text(errors="replace").splitlines()
                if end < len(lines):
                    candidate = {
                        **record,
                        "content": "\n".join(
                            f"{i + 1} | {lines[i]}" for i in range(start, end + 1)
                        ),
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
            records = [{"status": "no_returned_neighbors", "content": ""}]
        return json.dumps(records, ensure_ascii=False)

    def tool_spec(self):
        return ToolSpec(
            tool_id="graph_neighbors",
            executor_fn=self.neighbors,
            description="Read definitions connected by one directed compiler reference hop.",
            tool_doc=(
                "Choose an observed source file and 1-based line inside a definition. "
                "Outgoing follows its references; incoming finds definitions referencing "
                "it, including tests. These are compiler references, not a complete "
                "runtime call graph. Only numbered content has been read. A missing "
                "graph or omitted source is explicit; continue with read/grep as needed."
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
    parser.add_argument("--reuse-cohort", type=Path)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 1800:
        parser.error("Graph timeout must be between 1 and 1800 seconds")
    prepare_graphs(args)


if __name__ == "__main__":
    main()
