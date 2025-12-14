#!/usr/bin/env python3
"""
Test script for C++ Python bindings by comparing Python and C++ backends.

This script verifies that the pybind11 bindings work correctly by:
1. Testing basic binding functionality (import, operations, etc.)
2. Comparing outputs from Python backend vs C++ backend on real SCIP files
3. Measuring performance differences between backends

Usage Examples:
    # Quick binding test only (no SCIP decoding)
    python test/scip/test_scip_core_binding.py

    # Test with single SWE-bench instance
    python test/scip/test_scip_core_binding.py --instance pydata__xarray-3993

    # Test with default set of instances (7 predefined)
    python test/scip/test_scip_core_binding.py --multiple-instances

    # Test with N random instances
    python test/scip/test_scip_core_binding.py --num-instances 10

    # Test with local project
    python test/scip/test_scip_core_binding.py /path/to/project

    # Skip basic test and only do SCIP decoding comparison
    python test/scip/test_scip_core_binding.py --instance pydata__xarray-3993 --skip-basic-test

Output:
    Results are saved in /tmp/codeminer_binding_test/
    - For each instance: python_backend.graphml, cpp_backend.graphml
    - Console output shows pass/fail and performance comparison
"""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Tuple

# Import unified wrapper
from codeminer.core import (
    SCIPGraphDecoder,
    CodeGraph,
    get_backend_info,
)

# Import helper functions from existing test
HAS_SWEBENCH = False
try:
    # Try relative import first (when run as module)
    try:
        from .test_scip_core_standalone import (
            get_or_generate_scip_file,
            ensure_decoded_scip,
            normalize_graph_data,
            compare_nodes,
            compare_edges,
        )
    except ImportError:
        # Fall back to direct import (when run as script)
        script_dir = Path(__file__).parent
        if str(script_dir) not in sys.path:
            sys.path.insert(0, str(script_dir))
        from test_scip_core_standalone import (
            get_or_generate_scip_file,
            ensure_decoded_scip,
            normalize_graph_data,
            compare_nodes,
            compare_edges,
        )

    from codeminer.env.process_swebench_data import (
        load_filter_swebench_dataset_explicit,
        process_swebench_instance,
    )
    HAS_SWEBENCH = True
except ImportError as e:
    print(f"Warning: SWE-bench support not available: {e}")
    print("  (Only basic binding tests will run)")


def test_basic_bindings():
    """Test basic C++ binding functionality."""
    print("=" * 80)
    print("Test 1: Basic Binding Functionality")
    print("=" * 80)

    # Test 1.1: Direct C++ module import
    print("\n1.1 Testing direct C++ module import...")
    try:
        import codeminer_core
        print("  ✓ codeminer_core imported successfully")
        print(f"    Version: {codeminer_core.__version__}")
        print(f"    Location: {codeminer_core.__file__}")
    except ImportError as e:
        print(f"  ✗ Failed to import codeminer_core: {e}")
        return False

    # Test 1.2: Wrapper import
    print("\n1.2 Testing Python wrapper...")
    info = get_backend_info()
    print(f"  ✓ Wrapper imported")
    print(f"    C++ available: {info['cpp_available']}")
    print(f"    Using C++: {info['using_cpp']}")

    if not info['cpp_available']:
        print(f"    Import error: {info['import_error']}")
        return False

    # Test 1.3: CodeGraph creation
    print("\n1.3 Testing CodeGraph creation...")
    try:
        graph = CodeGraph("/tmp/test")
        print(f"  ✓ CodeGraph created")
        print(f"    Backend: {graph.backend}")

        # Test basic operations
        graph.add_root_node(".")
        graph.add_directory_node("src")
        graph.add_file_node("src/main.py")
        graph.add_symbol_node("src/main.py:main", 1, symbol_type="function")

        # Get node info
        root_info = graph.get_node_info_by_name(".")
        if root_info is None:
            print(f"  ✗ Failed to get node info")
            return False

        print(f"  ✓ Basic operations work")
        print(f"    Root node: {root_info}")

    except Exception as e:
        print(f"  ✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

    return True


def decode_with_backend(scip_file: Path, project_root: Path, use_cpp: bool, output_file: Path) -> Tuple[bool, Dict[str, Any]]:
    """Decode SCIP file with specified backend."""
    backend_name = "C++" if use_cpp else "Python"

    # Set environment variable to control backend
    if use_cpp:
        os.environ.pop("CODEMINER_USE_PYTHON", None)
    else:
        os.environ["CODEMINER_USE_PYTHON"] = "1"

    try:
        # Ensure SCIP file is decoded
        decoded_scip = ensure_decoded_scip(scip_file)

        # Create decoder
        start_time = time.time()
        decoder = SCIPGraphDecoder(str(decoded_scip), str(project_root))

        # Verify backend
        if decoder.backend != ("cpp" if use_cpp else "python"):
            print(f"    ✗ Expected {backend_name} backend but got {decoder.backend}")
            return False, {}

        # Decode
        print(f"    Decoding with {backend_name} backend...")
        graph = decoder.decode()
        elapsed = time.time() - start_time

        # Save as GraphML (both backends now support GraphML)
        graph.save_graph(str(output_file))

        # Load and count vertices/edges from GraphML
        import igraph as ig
        loaded_graph = ig.Graph.Read_GraphML(str(output_file))
        num_vertices = loaded_graph.vcount()
        num_edges = loaded_graph.ecount()

        stats = {
            'backend': backend_name,
            'time': elapsed,
            'vertices': num_vertices,
            'edges': num_edges,
            'success': True
        }

        print(f"    ✓ {backend_name} completed in {elapsed:.2f}s")
        print(f"      {num_vertices} vertices, {num_edges} edges")

        return True, stats

    except Exception as e:
        print(f"    ✗ {backend_name} backend error: {e}")
        import traceback
        traceback.print_exc()
        return False, {'backend': backend_name, 'success': False, 'error': str(e)}
    finally:
        # Reset environment
        os.environ.pop("CODEMINER_USE_PYTHON", None)


def compare_outputs(py_file: Path, cpp_file: Path) -> Tuple[bool, Dict[str, Any]]:
    """Compare outputs from Python and C++ backends."""
    print("\n  Comparing outputs...")

    try:
        # Load GraphML files
        import igraph as ig
        py_graph = ig.Graph.Read_GraphML(str(py_file))
        cpp_graph = ig.Graph.Read_GraphML(str(cpp_file))

        # Convert to normalized data format for comparison
        def graphml_to_data(graph):
            import math
            vertices = []
            for v in graph.vs:
                node_data = {"id": v.index, "name": v["name"], "type": v["type"]}

                # Handle file attribute
                if "file" in v.attributes():
                    file_val = v["file"]
                    # Filter out None, empty strings, and "None" string
                    if file_val and file_val not in ["None", ""]:
                        node_data["file"] = file_val

                # Handle start_line attribute (convert to int, filter NaN/None)
                if "start_line" in v.attributes():
                    start_line = v["start_line"]
                    if start_line is not None and not (isinstance(start_line, float) and math.isnan(start_line)):
                        node_data["start_line"] = int(start_line)

                # Handle end_line attribute (convert to int, filter NaN/None)
                if "end_line" in v.attributes():
                    end_line = v["end_line"]
                    if end_line is not None and not (isinstance(end_line, float) and math.isnan(end_line)):
                        node_data["end_line"] = int(end_line)

                vertices.append(node_data)

            edges = []
            for i, e in enumerate(graph.es):
                edges.append({
                    "id": i,
                    "source": e.source,
                    "target": e.target,
                    "type": e["type"],
                })

            return {"vertices": vertices, "edges": edges}

        py_data = graphml_to_data(py_graph)
        cpp_data = graphml_to_data(cpp_graph)

        # Normalize
        py_data = normalize_graph_data(py_data)
        cpp_data = normalize_graph_data(cpp_data)

        # Extract data
        py_nodes = py_data.get('nodes', py_data.get('vertices', []))
        cpp_nodes = cpp_data.get('nodes', cpp_data.get('vertices', []))
        py_edges = py_data.get('edges', [])
        cpp_edges = cpp_data.get('edges', [])

        # Compare
        node_count_mismatch, node_diffs = compare_nodes(py_nodes, cpp_nodes)
        edge_count_mismatch, edge_diffs = compare_edges(py_edges, cpp_edges, py_nodes, cpp_nodes)

        total_issues = node_count_mismatch + edge_count_mismatch + len(node_diffs) + len(edge_diffs)

        stats = {
            'python_vertices': len(py_nodes),
            'cpp_vertices': len(cpp_nodes),
            'python_edges': len(py_edges),
            'cpp_edges': len(cpp_edges),
            'node_count_mismatch': node_count_mismatch,
            'edge_count_mismatch': edge_count_mismatch,
            'node_diffs': len(node_diffs),
            'edge_diffs': len(edge_diffs),
            'total_issues': total_issues,
            'perfect_match': total_issues == 0,
        }

        # Print results
        if total_issues == 0:
            print(f"    ✓ PERFECT MATCH!")
            print(f"      Both backends produced identical output")
        else:
            print(f"    ⚠️  Found {total_issues} differences:")
            if node_count_mismatch:
                print(f"      - Node count: Python={len(py_nodes)}, C++={len(cpp_nodes)}")
            if edge_count_mismatch:
                print(f"      - Edge count: Python={len(py_edges)}, C++={len(cpp_edges)}")
            if node_diffs:
                print(f"      - Node differences: {len(node_diffs)}")
            if edge_diffs:
                print(f"      - Edge differences: {len(edge_diffs)}")

        return total_issues == 0, stats

    except Exception as e:
        print(f"    ✗ Comparison error: {e}")
        import traceback
        traceback.print_exc()
        return False, {}


def test_scip_decoding(project_root: Path, output_dir: Path) -> bool:
    """Test SCIP decoding with both backends and compare outputs."""
    print("\n" + "=" * 80)
    print(f"Test 2: SCIP Decoding Comparison ({project_root.name})")
    print("=" * 80)

    try:
        # Get SCIP file
        print("\n  Getting SCIP file...")
        scip_file, success = get_or_generate_scip_file(project_root)
        if not success:
            print(f"    ✗ Failed to get SCIP file")
            return False
        print(f"    ✓ SCIP file: {scip_file}")

        # Create output directory
        output_dir.mkdir(parents=True, exist_ok=True)

        py_output = output_dir / "python_backend.graphml"
        cpp_output = output_dir / "cpp_backend.graphml"

        # Decode with Python backend
        print("\n  Decoding with Python backend...")
        py_success, py_stats = decode_with_backend(scip_file, project_root, False, py_output)
        if not py_success:
            return False

        # Decode with C++ backend
        print("\n  Decoding with C++ backend...")
        cpp_success, cpp_stats = decode_with_backend(scip_file, project_root, True, cpp_output)
        if not cpp_success:
            return False

        # Compare
        match, _ = compare_outputs(py_output, cpp_output)

        # Print summary
        print("\n  " + "-" * 76)
        print(f"  Summary:")
        print(f"    Python: {py_stats['time']:.2f}s")
        print(f"    C++:    {cpp_stats['time']:.2f}s")
        speedup = py_stats['time'] / cpp_stats['time'] if cpp_stats['time'] > 0 else 0
        print(f"    Speedup: {speedup:.2f}x")
        print(f"    Result: {'✓ PASS' if match else '✗ FAIL'}")
        print("  " + "-" * 76)

        return match

    except Exception as e:
        print(f"  ✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Test C++ Python bindings by comparing Python and C++ backends"
    )

    parser.add_argument(
        "project_root",
        type=Path,
        nargs="?",
        help="Path to project root directory (for local testing)",
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

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "codeminer_binding_test",
        help="Output directory for test results",
    )

    parser.add_argument(
        "--skip-basic-test",
        action="store_true",
        help="Skip basic binding test and go straight to SCIP decoding",
    )

    args = parser.parse_args()

    # Default test instances (same as test_scip_core_standalone.py)
    DEFAULT_TEST_INSTANCES = [
        "astropy__astropy-13033",
        "django__django-11099",
        "scikit-learn__scikit-learn-10297",
        "mwaskom__seaborn-3187",
        "matplotlib__matplotlib-24570",
        "pydata__xarray-3993",
        "sphinx-doc__sphinx-7757",
    ]

    # Test 1: Basic bindings (unless skipped)
    if not args.skip_basic_test:
        if not test_basic_bindings():
            print("\n✗ Basic binding test FAILED")
            return 1
        print("\n✓ Basic binding test PASSED")

    # Determine which instances to test
    instance_ids = []

    if args.multiple_instances:
        instance_ids = DEFAULT_TEST_INSTANCES
        print(f"\nTesting {len(instance_ids)} default instances")
    elif args.num_instances:
        if not HAS_SWEBENCH:
            print("✗ SWE-bench support not available")
            return 1
        import random
        print(f"\nLoading dataset...")
        dataset = load_filter_swebench_dataset_explicit(
            dataset="princeton-nlp/SWE-bench_Verified",
            filter_instance=".*",
            split="test",
        )
        total_instances = len(dataset)
        num_to_select = min(args.num_instances, total_instances)
        selected_indices = random.sample(range(total_instances), num_to_select)
        dataset = dataset.select(selected_indices)
        instance_ids = [item["instance_id"] for item in dataset]
        print(f"Randomly selected {len(instance_ids)} instances from {total_instances} total")
    elif args.instance:
        instance_ids = [args.instance]

    # Handle batch testing
    if len(instance_ids) > 1:
        if not HAS_SWEBENCH:
            print("✗ SWE-bench support not available for batch testing")
            return 1

        print(f"\n{'='*80}")
        print(f"BATCH MODE: Testing {len(instance_ids)} instances")
        print(f"{'='*80}")

        all_results = []
        for i, instance_id in enumerate(instance_ids, 1):
            print(f"\n[{i}/{len(instance_ids)}] Testing {instance_id}")

            try:
                dataset = load_filter_swebench_dataset_explicit(
                    dataset="princeton-nlp/SWE-bench_Verified",
                    filter_instance=instance_id,
                    split="test",
                )

                if len(dataset) == 0:
                    print(f"  ✗ Instance not found")
                    all_results.append({
                        "instance_id": instance_id,
                        "success": False,
                        "error": "instance_not_found"
                    })
                    continue

                instance = dataset[0]
                project_root = Path(process_swebench_instance(instance, cache_dir="~/.codeminer"))

                success = test_scip_decoding(project_root, args.output_dir / instance_id)
                all_results.append({
                    "instance_id": instance_id,
                    "success": success,
                })

            except Exception as e:
                print(f"  ✗ Error: {e}")
                all_results.append({
                    "instance_id": instance_id,
                    "success": False,
                    "error": str(e)
                })

        # Print summary
        print(f"\n{'='*80}")
        print(f"BATCH TEST SUMMARY")
        print(f"{'='*80}")

        passed = sum(1 for r in all_results if r.get("success", False))
        failed = len(all_results) - passed

        print(f"Total instances tested: {len(all_results)}")
        print(f"✓ Passed: {passed}")
        print(f"✗ Failed: {failed}")

        if failed > 0:
            print("\nFailed instances:")
            for r in all_results:
                if not r.get("success", False):
                    error = r.get("error", "unknown")
                    print(f"  ✗ {r['instance_id']}: {error}")
        else:
            print("\n✓ All instances passed!")

        print(f"{'='*80}\n")

        return 0 if failed == 0 else 1

    # Handle single instance
    elif len(instance_ids) == 1:
        if not HAS_SWEBENCH:
            print("✗ SWE-bench support not available")
            return 1

        print(f"\nTesting with SWE-bench instance: {instance_ids[0]}")
        try:
            dataset = load_filter_swebench_dataset_explicit(
                dataset="princeton-nlp/SWE-bench_Verified",
                filter_instance=instance_ids[0],
                split="test",
            )

            if len(dataset) == 0:
                print(f"✗ Instance not found: {instance_ids[0]}")
                return 1

            instance = dataset[0]
            project_root = Path(process_swebench_instance(instance, cache_dir="~/.codeminer"))

            if not test_scip_decoding(project_root, args.output_dir / instance_ids[0]):
                print("\n✗ SCIP decoding test FAILED")
                return 1

        except Exception as e:
            print(f"✗ Error: {e}")
            import traceback
            traceback.print_exc()
            return 1

    # Handle local project
    elif args.project_root:
        if not args.project_root.exists():
            print(f"✗ Project not found: {args.project_root}")
            return 1

        if not test_scip_decoding(args.project_root, args.output_dir):
            print("\n✗ SCIP decoding test FAILED")
            return 1

    # All tests passed
    print("\n" + "=" * 80)
    print("✓ ALL TESTS PASSED")
    print("=" * 80)

    return 0


if __name__ == "__main__":
    sys.exit(main())
