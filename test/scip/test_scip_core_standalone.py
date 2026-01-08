#!/usr/bin/env python3
"""
Test correctness of C++ SCIP decoder. (compare with Python decoder)

This script:
1. Uses Python to decode a SCIP file and saves graph as JSON
2. Uses C++ standalone decoder to decode the same SCIP file and saves graph as JSON
3. Compares the two JSON outputs and generates a diff report

Usage Examples:
    # Test single SWE-bench instance
    python test/scip/test_scip_core_standalone.py --instance django__django-11099

    # Test default instances (7 predefined instances)
    python test/scip/test_scip_core_standalone.py --multiple-instances

    # Test random N instances from SWE-bench_Verified
    python test/scip/test_scip_core_standalone.py --num-instances 10

    # Test local project
    python test/scip/test_scip_core_standalone.py /tmp/httpie-cli

Output:
    Results are saved in test/scip/comparison_results/
    - For each instance: <instance_id>/python_output.json, cpp_output.json
    - For batch testing: summary_report.json
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from codeminer.dataset.swebench import SwebenchDataset
from codeminer.scip_interface.scip_decode import SCIPGraphDecoder
from codeminer.scip_interface.scip_indexer import SCIPIndexer

# Default instances for testing
DEFAULT_TEST_INSTANCES = [
    "astropy__astropy-13033",
    "django__django-11099",
    "scikit-learn__scikit-learn-10297",
    "mwaskom__seaborn-3187",
    "matplotlib__matplotlib-24570",
    "pydata__xarray-3993",
    "sphinx-doc__sphinx-7757",
]


def get_or_generate_scip_file(
    project_root: Path, instance_id: str = None
) -> Tuple[Path | None, bool]:
    """
    Get cached SCIP file or generate new one.

    Args:
        project_root: Project root directory
        instance_id: Optional instance ID to use as cache key (recommended for
            SWE-bench instances)

    Returns:
        Tuple of (scip_file_path, success)
        If failed, returns (None, False)
    """
    # Use platform-independent temporary directory
    # Use instance_id if provided (for SWE-bench), otherwise use project_root.name
    # (for local projects).
    cache_dir_name = instance_id if instance_id else project_root.name
    scip_cache_dir = Path(tempfile.gettempdir()) / cache_dir_name
    scip_cache_dir.mkdir(parents=True, exist_ok=True)

    scip_file = scip_cache_dir / "index.scip"
    decoded_scip = scip_cache_dir / "index.decoded"

    # Check if cached SCIP index exists (prefer decoded version)
    if decoded_scip.exists():
        print(f"  Found cached decoded SCIP at {decoded_scip}")
        return decoded_scip, True
    elif scip_file.exists():
        print(f"  Found cached SCIP index at {scip_file}")
        return scip_file, True
    else:
        # Generate SCIP index to cache directory
        try:
            scip_file = generate_scip_index(project_root, scip_cache_dir)
            print(f"  ✓ Generated SCIP index to cache")
            return scip_file, True
        except Exception as e:
            print(f"  ❌ Failed to generate SCIP index: {e}")
            return None, False


def generate_scip_index(project_root: Path, output_dir: Path) -> Path:
    """
    Generate SCIP index for the project if it doesn't exist.

    Args:
        project_root: Project root directory (where Python source code is located)
        output_dir: Output directory for SCIP files

    Returns:
        Path to the generated SCIP index file (decoded)
    """
    try:
        # Create indexer
        indexer = SCIPIndexer(project_root=project_root, output_dir=output_dir)

        # Generate and decode index
        # Use skip_level='raw' to reuse existing index.scip if it exists
        if indexer.index_file.exists():
            # Ensure it's decoded
            if not indexer.decoded_file.exists():
                if not indexer.decode_index():
                    raise RuntimeError("Failed to decode SCIP index")
            return indexer.decoded_file

        # Generate new index
        # IMPORTANT: Pass project_root as cwd so scip-python indexes the correct
        # directory.
        if not indexer.generate_index(cwd=project_root):
            raise RuntimeError("Failed to generate SCIP index")

        # Decode the index
        if not indexer.decode_index():
            raise RuntimeError("Failed to decode SCIP index")

        return indexer.decoded_file

    except Exception as e:
        raise RuntimeError(f"Error generating SCIP index: {e}") from e


def ensure_decoded_scip(scip_file: Path) -> Path:
    """Ensure SCIP file is in decoded text format."""
    # Check if file is binary by trying to read as text
    try:
        with open(scip_file, "r", encoding="utf-8") as f:
            f.read(100)
        # If successful, it's already decoded
        return scip_file
    except UnicodeDecodeError:
        # It's binary, need to decode
        decoded_file = scip_file.with_suffix(".scip.decoded")

        # Find scip.proto file - go up from test/scip to project root
        module_dir = (
            Path(__file__).parent.parent.parent / "codeminer" / "scip_interface"
        )
        proto_file = module_dir / "scip.proto"

        if not proto_file.exists():
            raise FileNotFoundError(f"scip.proto not found at {proto_file}") from None

        # Use protoc to decode binary SCIP to text format (safe from shell injection)
        with (
            open(scip_file, "rb") as stdin_file,
            open(decoded_file, "wb") as stdout_file,
        ):
            result = subprocess.run(
                [
                    "protoc",
                    "--decode=scip.Index",
                    f"--proto_path={module_dir}",
                    "scip.proto",
                ],
                stdin=stdin_file,
                stdout=stdout_file,
                stderr=subprocess.PIPE,
                text=False,
            )

        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to decode SCIP file: {result.stderr.decode('utf-8')}"
            ) from None

        return decoded_file


def normalize_graph_data(graph_data: Dict) -> Dict:
    """Normalize graph data for consistent comparison.

    Sorts vertices and edges by their IDs to ensure deterministic ordering.
    """
    # Sort nodes/vertices by ID
    for key in ["nodes", "vertices"]:
        if key in graph_data:
            graph_data[key] = sorted(
                graph_data[key], key=lambda x: x.get("id", float("inf"))
            )

    # Sort edges by edge ID
    if "edges" in graph_data:
        graph_data["edges"] = sorted(
            graph_data["edges"], key=lambda x: x.get("id", float("inf"))
        )

    return graph_data


def python_decode(scip_file: Path, project_root: Path, output_file: Path) -> bool:
    """Decode SCIP file using Python decoder."""
    print(f"Python decoder...")

    try:
        # Ensure SCIP file is decoded (text format)
        decoded_scip = ensure_decoded_scip(scip_file)

        # Decode using Python
        start_time = time.time()
        decoder = SCIPGraphDecoder(str(decoded_scip), str(project_root))
        print(f"  Decoding into a graph...")
        graph = decoder.decode()
        elapsed = time.time() - start_time
        print(f"  ✓ Decoding completed in {elapsed:.1f}s")

        # Convert to JSON format (matching C++ output format)
        vertices = []
        edges = []

        # Extract nodes (vertices) with ID
        for v in graph.graph.vs:
            node_data = {"id": v.index, "name": v["name"], "type": v["type"]}
            if "file" in v.attributes():
                node_data["file"] = v["file"]
            if "start_line" in v.attributes():
                node_data["start_line"] = v["start_line"]
            if "end_line" in v.attributes():
                node_data["end_line"] = v["end_line"]
            vertices.append(node_data)

        # Extract edges (using vertex IDs, not names, to match C++)
        for i, e in enumerate(graph.graph.es):
            edges.append(
                {
                    "id": i,  # Add edge ID to match C++
                    "source": e.source,  # Use vertex ID (int) instead of name
                    "target": e.target,  # Use vertex ID (int) instead of name
                    "type": e["type"],
                }
            )

        graph_data = {
            "project_root": str(project_root) if project_root else None,
            "vertices": vertices,  # Changed from "nodes" to "vertices" to match C++
            "edges": edges,
        }

        # Normalize and save
        graph_data = normalize_graph_data(graph_data)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w") as f:
            json.dump(graph_data, f, indent=2)

        print(f"  ✓ {len(vertices)} vertices, {len(edges)} edges")
        return True

    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False


def cpp_decode(
    scip_file: Path, project_root: Path, output_file: Path, cpp_decoder: Path
) -> bool:
    """Decode SCIP file using C++ decoder."""
    print(f"C++ decoder...")

    try:
        # Ensure SCIP file is decoded (text format)
        decoded_scip = ensure_decoded_scip(scip_file)

        # Run C++ decoder
        print(f"  Decoding with C++ decoder...")
        start_time = time.time()

        project_root_arg = str(project_root) if project_root else "null"
        cmd = [str(cpp_decoder), str(decoded_scip), project_root_arg, str(output_file)]

        result = subprocess.run(cmd, capture_output=True, text=True)
        elapsed = time.time() - start_time

        if result.returncode != 0:
            error_msg = result.stderr if result.stderr else "unknown error"
            print(f"  ❌ Error (after {elapsed:.1f}s): {error_msg}")
            return False

        print(f"  ✓ C++ decoding completed in {elapsed:.1f}s")

        # Normalize the C++ output
        if output_file.exists():
            with open(output_file, "r") as f:
                graph_data = json.load(f)

            # Count nodes and edges
            nodes = graph_data.get("nodes", graph_data.get("vertices", []))
            edges = graph_data.get("edges", [])

            graph_data = normalize_graph_data(graph_data)
            with open(output_file, "w") as f:
                json.dump(graph_data, f, indent=2)

            print(f"  ✓ {len(nodes)} nodes, {len(edges)} edges")

        return True

    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False


def compare_nodes(py_nodes: List[Dict], cpp_nodes: List[Dict]) -> Tuple[int, List[str]]:
    """Compare nodes between Python and C++ outputs using name-based comparison.

    Args:
        py_nodes: Python decoder's nodes
        cpp_nodes: C++ decoder's nodes

    Returns:
        Tuple of (num_count_mismatch, node_differences)
        - num_count_mismatch: 0 if counts match, otherwise the difference
        - node_differences: List of detailed difference descriptions
    """
    differences = []

    # First check if node counts match
    if len(py_nodes) != len(cpp_nodes):
        count_diff = abs(len(py_nodes) - len(cpp_nodes))
        differences.append(
            f"Node count mismatch: Python has {len(py_nodes)} nodes, "
            f"C++ has {len(cpp_nodes)} nodes (diff: {count_diff})"
        )
        return count_diff, differences

    # Build name-to-node mappings
    py_by_name = {node.get("name"): node for node in py_nodes}
    cpp_by_name = {node.get("name"): node for node in cpp_nodes}

    # Check for nodes present in one but not the other
    py_names = set(py_by_name.keys())
    cpp_names = set(cpp_by_name.keys())

    only_in_py = py_names - cpp_names
    only_in_cpp = cpp_names - py_names

    if only_in_py:
        differences.append(f"Nodes only in Python: {sorted(only_in_py)}")
    if only_in_cpp:
        differences.append(f"Nodes only in C++: {sorted(only_in_cpp)}")

    if only_in_py or only_in_cpp:
        return len(only_in_py) + len(only_in_cpp), differences

    # Compare nodes by name (for nodes present in both)
    for name in sorted(py_names & cpp_names):
        py_node = py_by_name[name]
        cpp_node = cpp_by_name[name]

        # Compare all fields except 'id' (IDs can differ, we care about content)
        node_diffs = []
        all_keys = set(py_node.keys()) | set(cpp_node.keys())
        all_keys.discard("id")  # Ignore ID differences

        for key in sorted(all_keys):
            py_val = py_node.get(key)
            cpp_val = cpp_node.get(key)

            if py_val != cpp_val:
                node_diffs.append(f"    {key}: Python={py_val}, C++={cpp_val}")

        if node_diffs:
            differences.append(f"  Node {name!r}:\n" + "\n".join(node_diffs) + "\n")

    return 0, differences


def compare_edges(
    py_edges: List[Dict],
    cpp_edges: List[Dict],
    py_nodes: List[Dict] = None,
    cpp_nodes: List[Dict] = None,
) -> Tuple[int, List[str]]:
    """Compare edges between Python and C++ outputs using name-based comparison.

    Args:
        py_edges: Python decoder's edges
        cpp_edges: C++ decoder's edges
        py_nodes: Python decoder's nodes (for ID-to-name mapping)
        cpp_nodes: C++ decoder's nodes (for ID-to-name mapping)

    Returns:
        Tuple of (num_count_mismatch, edge_differences)
        - num_count_mismatch: 0 if counts match, otherwise the difference
        - edge_differences: List of detailed difference descriptions
    """
    differences = []

    # Build ID-to-name mappings
    py_id_to_name = {}
    cpp_id_to_name = {}

    if py_nodes:
        for node in py_nodes:
            node_id = node.get("id")
            if node_id is not None and "name" in node:
                py_id_to_name[node_id] = node["name"]

    if cpp_nodes:
        for node in cpp_nodes:
            node_id = node.get("id")
            if node_id is not None and "name" in node:
                cpp_id_to_name[node_id] = node["name"]

    # First check if edge counts match
    if len(py_edges) != len(cpp_edges):
        count_diff = abs(len(py_edges) - len(cpp_edges))
        differences.append(
            f"Edge count mismatch: Python has {len(py_edges)} edges, "
            f"C++ has {len(cpp_edges)} edges (diff: {count_diff})"
        )
        return count_diff, differences

    # Convert edges to name-based representation for comparison
    def edge_to_tuple(edge, id_to_name):
        """Convert edge to (source_name, target_name, type) tuple."""
        source_id = edge.get("source")
        target_id = edge.get("target")
        source_name = id_to_name.get(source_id, f"UNKNOWN_ID_{source_id}")
        target_name = id_to_name.get(target_id, f"UNKNOWN_ID_{target_id}")
        edge_type = edge.get("type")
        return (source_name, target_name, edge_type)

    # Build sets of edges (by node names)
    py_edge_set = {edge_to_tuple(e, py_id_to_name) for e in py_edges}
    cpp_edge_set = {edge_to_tuple(e, cpp_id_to_name) for e in cpp_edges}

    # Find differences
    only_in_py = py_edge_set - cpp_edge_set
    only_in_cpp = cpp_edge_set - py_edge_set

    if only_in_py:
        differences.append(f"Edges only in Python ({len(only_in_py)}):")
        for src, tgt, typ in sorted(only_in_py)[:5]:
            differences.append(f"  {src} -> {tgt} [{typ}]")
        if len(only_in_py) > 5:
            differences.append(f"  ... and {len(only_in_py) - 5} more")

    if only_in_cpp:
        differences.append(f"Edges only in C++ ({len(only_in_cpp)}):")
        for src, tgt, typ in sorted(only_in_cpp)[:5]:
            differences.append(f"  {src} -> {tgt} [{typ}]")
        if len(only_in_cpp) > 5:
            differences.append(f"  ... and {len(only_in_cpp) - 5} more")

    if only_in_py or only_in_cpp:
        return len(only_in_py) + len(only_in_cpp), differences

    return 0, differences


def generate_report(
    py_file: Path, cpp_file: Path, json_report_file: Path = None
) -> Tuple[bool, Dict[str, Any]]:
    """
    Generate comparison report.

    Args:
        py_file: Path to Python decoder output JSON
        cpp_file: Path to C++ decoder output JSON
        json_report_file: Optional path to save detailed JSON report

    Returns:
        Tuple of (success, stats_dict) where stats_dict contains comparison statistics
    """
    print(f"\nComparing...")

    try:
        # Load JSON files
        with open(py_file, "r") as f:
            py_data = json.load(f)

        with open(cpp_file, "r") as f:
            cpp_data = json.load(f)

        # Compare - handle both "nodes" and "vertices" keys
        py_nodes = py_data.get("nodes", py_data.get("vertices", []))
        cpp_nodes = cpp_data.get("nodes", cpp_data.get("vertices", []))
        py_edges = py_data.get("edges", [])
        cpp_edges = cpp_data.get("edges", [])

        # Compare nodes using strict ID-based comparison
        node_count_mismatch, node_diffs = compare_nodes(py_nodes, cpp_nodes)

        # Compare edges using strict ID-based comparison
        edge_count_mismatch, edge_diffs = compare_edges(
            py_edges, cpp_edges, py_nodes, cpp_nodes
        )

        total_issues = (
            node_count_mismatch
            + edge_count_mismatch
            + len(node_diffs)
            + len(edge_diffs)
        )

        # Statistics dictionary
        stats = {
            "py_nodes": len(py_nodes),
            "cpp_nodes": len(cpp_nodes),
            "py_edges": len(py_edges),
            "cpp_edges": len(cpp_edges),
            "node_count_mismatch": node_count_mismatch,
            "edge_count_mismatch": edge_count_mismatch,
            "node_diffs": len(node_diffs),
            "edge_diffs": len(edge_diffs),
            "total_issues": total_issues,
            "perfect_match": total_issues == 0,
        }

        # Generate simplified report
        report = []
        report.append("=" * 80)
        report.append(f"Python: {len(py_nodes)} nodes, {len(py_edges)} edges")
        report.append(f"C++:    {len(cpp_nodes)} nodes, {len(cpp_edges)} edges")
        report.append("")

        if total_issues == 0:
            report.append("✅ PERFECT MATCH")
        else:
            report.append(f"⚠️  {total_issues} differences found")
        report.append("")

        # Only show differences if there are issues
        if total_issues > 0:
            # Show count mismatches
            if node_count_mismatch > 0:
                report.append(
                    (
                        f"❌ Node count mismatch: Python={len(py_nodes)}, "
                        f"C++={len(cpp_nodes)}"
                    )
                )
                report.append("")

            if edge_count_mismatch > 0:
                report.append(
                    (
                        f"❌ Edge count mismatch: Python={len(py_edges)}, "
                        f"C++={len(cpp_edges)}"
                    )
                )
                report.append("")

            # Show node differences
            if node_diffs:
                report.append(f"Node differences: {len(node_diffs)}")
                for diff in node_diffs[:5]:
                    report.append(f"{diff}")
                if len(node_diffs) > 5:
                    report.append(f"  ... and {len(node_diffs) - 5} more")
                report.append("")

            # Show edge differences
            if edge_diffs:
                report.append(f"Edge differences: {len(edge_diffs)}")
                for diff in edge_diffs[:5]:
                    report.append(f"{diff}")
                if len(edge_diffs) > 5:
                    report.append(f"  ... and {len(edge_diffs) - 5} more")
                report.append("")

        report.append("=" * 80)

        # Print report to console
        report_text = "\n".join(report)
        print(f"\n{report_text}")

        return total_issues == 0, stats

    except Exception as e:
        print(f"❌ Error: {e}")
        return False, {}


def process_single_instance(
    instance_id: str, args: argparse.Namespace, cpp_decoder: Path
) -> Tuple[bool, Dict[str, Any]]:
    """
    Process a single SWE-bench instance.

    Returns:
        Tuple of (success, stats_dict)
    """
    print(f"\n{'='*80}")
    print(f"Processing instance: {instance_id}")
    print(f"{'='*80}")

    try:
        # Load the dataset and filter by instance ID
        dataset_obj = SwebenchDataset(
            dataset=args.swebench_dataset,
            split=args.swebench_split,
            filter_instance=instance_id,
            root=args.cache_dir,
            repo_root=args.cache_dir,
        )
        dataset = dataset_obj.load()

        if len(dataset) == 0:
            print(f"❌ Instance {instance_id} not found in {args.swebench_dataset}")
            return False, {"error": "instance_not_found"}

        instance = dataset[0]
        print(f"  Repo: {instance['repo']}")
        print(f"  Base commit: {instance['base_commit']}")

        # Process the instance (download and checkout)
        dataset_obj.process_instance(instance, repo_root=args.cache_dir)
        project_root = Path(
            dataset_obj.get_repo_path(instance, repo_root=args.cache_dir)
        )
        print(f"  Project root: {project_root}")

        # Get or generate SCIP file
        scip_file, success = get_or_generate_scip_file(project_root, instance_id)
        if not success:
            return False, {"error": "scip_generation_failed"}

        # Output files
        instance_output_dir = args.output_dir / instance_id
        instance_output_dir.mkdir(parents=True, exist_ok=True)
        print(f"  Output directory: {instance_output_dir}")

        py_output = instance_output_dir / "python_output.json"
        cpp_output = instance_output_dir / "cpp_output.json"
        json_report = instance_output_dir / "comparison_report.json"

        # Run decoders
        if not python_decode(scip_file, project_root, py_output):
            print(f"  ❌ Python decoder failed")
            return False, {"error": "python_decode_failed"}

        if not cpp_decode(scip_file, project_root, cpp_output, cpp_decoder):
            print(f"  ❌ C++ decoder failed")
            return False, {"error": "cpp_decode_failed"}

        # Generate comparison report with JSON output
        success, stats = generate_report(py_output, cpp_output, json_report)
        stats["instance_id"] = instance_id
        stats["repo"] = instance["repo"]

        return success, stats

    except Exception as e:
        print(f"  ❌ Error processing instance: {e}")
        import traceback

        traceback.print_exc()
        return False, {"error": str(e), "instance_id": instance_id}


def process_local_project(
    project_root: Path, output_dir: Path, cpp_decoder: Path
) -> bool:
    """
    Process a local project for testing.

    Args:
        project_root: Path to project root directory
        output_dir: Output directory for results
        cpp_decoder: Path to C++ decoder executable

    Returns:
        bool: True if test passed, False otherwise
    """
    print(f"\n{'='*80}")
    print(f"Processing local project: {project_root.name}")
    print(f"{'='*80}")

    try:
        # Get or generate SCIP file
        scip_file, success = get_or_generate_scip_file(project_root)
        if not success:
            return False

        # Create output directory
        output_dir.mkdir(parents=True, exist_ok=True)

        # Output files
        output_name = project_root.name
        py_output = output_dir / f"{output_name}_python.json"
        cpp_output = output_dir / f"{output_name}_cpp.json"
        json_report = output_dir / f"{output_name}_comparison_report.json"

        # Run decoders
        if not python_decode(scip_file, project_root, py_output):
            print(f"  ❌ Python decoder failed")
            return False

        if not cpp_decode(scip_file, project_root, cpp_output, cpp_decoder):
            print(f"  ❌ C++ decoder failed")
            return False

        # Generate comparison report with JSON output
        success, stats = generate_report(py_output, cpp_output, json_report)
        return success

    except Exception as e:
        print(f"  ❌ Error processing local project: {e}")
        import traceback

        traceback.print_exc()
        return False


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Compare C++ and Python SCIP decoders. See file docstring for usage "
            "examples."
        )
    )

    parser.add_argument(
        "project_root",
        type=Path,
        nargs="?",
        help="Path to project root directory (for local testing)",
    )

    parser.add_argument(
        "--cpp-decoder",
        type=Path,
        default=Path(__file__).parent.parent.parent
        / "build"
        / "core"
        / "scip_decode_repo",
        help="Path to C++ decoder executable (default: build/core/scip_decode_repo)",
    )

    # SWE-bench testing modes
    parser.add_argument(
        "--instance",
        type=str,
        help="Test single SWE-bench instance (e.g., django__django-11099)",
    )

    parser.add_argument(
        "--multiple-instances",
        action="store_true",
        help="Test default set of instances (7 predefined instances)",
    )

    parser.add_argument(
        "--num-instances",
        type=int,
        help="Test N random instances from SWE-bench_Verified",
    )

    args = parser.parse_args()

    # Set default values
    output_dir = Path(tempfile.gettempdir()) / "codeminer_scip_test_results"
    swebench_dataset = "princeton-nlp/SWE-bench_Verified"
    swebench_split = "test"
    cache_dir = "~/.codeminer"

    # Add these as args attributes for compatibility with process_single_instance
    args.output_dir = output_dir
    args.swebench_dataset = swebench_dataset
    args.swebench_split = swebench_split
    args.cache_dir = cache_dir

    # Validate C++ decoder
    if not args.cpp_decoder.exists():
        print(f"❌ C++ decoder not found: {args.cpp_decoder}")
        return 1

    # Determine which instances to test
    instance_ids = []

    # Priority: multiple-instances > num-instances > single instance
    if args.multiple_instances:
        # Use default predefined instances
        instance_ids = DEFAULT_TEST_INSTANCES
        print(f"Testing {len(instance_ids)} default instances")

    elif args.num_instances:
        # Load dataset and randomly select N instances
        import random

        print(f"Loading dataset {swebench_dataset}...")
        dataset_obj = SwebenchDataset(
            dataset=swebench_dataset,
            split=swebench_split,
            filter_instance=".*",
            root=cache_dir,
        )
        dataset = dataset_obj.load()

        # Randomly select N instances
        total_instances = len(dataset)
        num_to_select = min(args.num_instances, total_instances)
        selected_indices = random.sample(range(total_instances), num_to_select)
        dataset = dataset.select(selected_indices)

        instance_ids = [item["instance_id"] for item in dataset]
        print(
            (
                f"Randomly selected {len(instance_ids)} instances from "
                f"{total_instances} total"
            )
        )

    elif args.instance:
        # Single instance
        instance_ids = [args.instance]

    # Handle batch testing
    if len(instance_ids) > 1:
        print(f"\n{'='*80}")
        print(f"BATCH MODE: Testing {len(instance_ids)} instances")
        print(f"{'='*80}")

        all_stats = []
        for i, instance_id in enumerate(instance_ids, 1):
            print(f"\n[{i}/{len(instance_ids)}] Testing {instance_id}")

            success, stats = process_single_instance(
                instance_id, args, args.cpp_decoder
            )
            all_stats.append(stats)

        # Generate summary report
        print(f"\n{'='*80}")
        print(f"BATCH TEST SUMMARY")
        print(f"{'='*80}")

        passed_count = sum(1 for s in all_stats if s.get("perfect_match", False))
        failed_count = len(all_stats) - passed_count

        print(f"Total instances tested: {len(all_stats)}")
        print(f"✅ Passed: {passed_count}")
        print(f"❌ Failed: {failed_count}")
        print()

        if failed_count > 0:
            print("Failed instances:")
            for stats in all_stats:
                if not stats.get("perfect_match", False):
                    instance_id = stats.get("instance_id", "unknown")
                    total_issues = stats.get("total_issues", 0)
                    node_diffs = stats.get("node_diffs", 0)
                    edge_diffs = stats.get("edge_diffs", 0)
                    print(
                        f"  ❌ {instance_id}: {total_issues} issues "
                        f"({node_diffs} node diffs, {edge_diffs} edge diffs)"
                    )
        else:
            print("✅ All instances passed!")

        print(f"{'='*80}\n")

        # Return success if all passed
        all_success = passed_count == len(all_stats)
        return 0 if all_success else 1

    # Handle single instance
    elif len(instance_ids) == 1:
        success, stats = process_single_instance(
            instance_ids[0], args, args.cpp_decoder
        )
        return 0 if success else 1

    # Traditional mode: test local project
    if args.project_root is None:
        print(
            "❌ Error: Either provide project_root or use "
            "--instance/--multiple-instances/--num-instances"
        )
        parser.print_help()
        return 1

    if not args.project_root.exists():
        print(f"❌ Project root not found: {args.project_root}")
        return 1

    # Process local project
    success = process_local_project(args.project_root, output_dir, args.cpp_decoder)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
