#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
SCIP decoder for Go projects.

Decodes SCIP index files into CodeGraph format, focusing on:
- Structs and interfaces (treated as classes)
- Methods (receiver functions)
- Free functions
- Fields and constants
- References between symbols

Note: scip-go does NOT emit enclosing_range (as of v0.1.26),
so scope boundaries are unavailable. Nodes are attached to the
current scope via contain edges without start_line/end_line.
"""
import re
from pathlib import Path

from ..graph.code_graph import CodeGraph
from ..log_utils import get_logger, register_scip_logger
from ..types import (
    EDGE_TYPE_CONTAIN,
    NODE_TYPE_CLASS,
    NODE_TYPE_FIELD,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    ROOT_NODE,
)
from .scip_indexer_base import extract_scip_blocks, extract_symbol


class SCIPGoGraphDecoder:
    """
    Decoder for Go SCIP indices.

    Parses decoded SCIP index files and builds a CodeGraph representing:
    - Structs and interfaces as classes
    - Methods (with receivers) as methods
    - Package-level functions
    - Fields, constants, and variables
    - References between symbols
    """

    def __init__(self, index_file_path, project_root=None):
        self.index_file_path = index_file_path
        self.project_root = Path(project_root) if project_root else None
        self.code_graph = CodeGraph(project_root)
        self.indexed_directories = set()
        self.logger = get_logger(__name__)
        register_scip_logger(__name__)

        # Internal module path (loaded lazily from go.mod)
        self.internal_module = None

    def decode(self):
        """Decode the SCIP index and build the CodeGraph."""
        self.logger.info(f"Starting SCIP Go decode from {self.index_file_path}")
        try:
            with open(self.index_file_path, "r") as f:
                content = f.read()
        except Exception as e:
            self.logger.error(f"Error reading SCIP index file: {e}")
            raise

        # Parse documents
        document_blocks = re.findall(
            r"documents\s*{(.*?)(?=documents\s*{|$)", content, re.DOTALL
        )

        # Add the root node to the graph
        self.code_graph.add_root_node(ROOT_NODE)

        # Process all documents. Batch edge insertion: per-edge add_edges is
        # O(E^2) (igraph reindexes each call); buffering flushes once as O(E).
        with self.code_graph.batch_edges():
            for document in document_blocks:
                self._process_document(document)

        return self.code_graph

    def _process_document(self, document_text):
        """Process a single document block from the SCIP index."""
        # Extract file path
        file_match = re.search(r'relative_path:\s*"([^"]+)"', document_text)
        if not file_match:
            return

        file_path = file_match.group(1)

        # Only process Go files
        if not file_path.endswith(".go"):
            return

        # Skip test files
        if file_path.endswith("_test.go"):
            return

        # Add directory hierarchy
        dir_path = Path(file_path).parent
        while dir_path != dir_path.parent:
            dir_path_str = str(dir_path)
            if dir_path_str not in self.indexed_directories:
                self.code_graph.add_directory_node(dir_path_str)
                self.indexed_directories.add(dir_path_str)
                self.code_graph._add_edge(
                    str(dir_path.parent), dir_path_str, EDGE_TYPE_CONTAIN
                )
            dir_path = dir_path.parent

        # Add file node
        self.code_graph.add_file_node(file_path)

        # Add file containment edge
        self.code_graph._add_edge(
            str(Path(file_path).parent), file_path, EDGE_TYPE_CONTAIN
        )

        # Load module path once (for third-party filtering)
        if self.internal_module is None:
            self.internal_module = self._load_internal_module()

        # Process occurrences
        # Brace-balanced: see extract_scip_blocks docstring (regex truncates
        # on symbols containing literal "{" / "}").
        occurrences = extract_scip_blocks(document_text, "occurrences")
        for occurrence in occurrences:
            self._process_occurrence(occurrence, file_path)

    def _load_internal_module(self):
        """Parse go.mod to extract the module path for filtering third-party symbols."""
        if not self.project_root:
            return ""

        go_mod_path = self.project_root / "go.mod"
        if not go_mod_path.exists():
            return ""

        try:
            with open(go_mod_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("module "):
                        module_path = line[len("module ") :].strip()
                        self.logger.info(f"Loaded Go module path: {module_path}")
                        return module_path
        except Exception as e:
            self.logger.warning(f"Failed to parse {go_mod_path}: {e}")

        return ""

    def _process_occurrence(self, occurrence_text, file_path):
        """Process a single occurrence from the SCIP index."""
        # Extract ranges
        ranges = re.findall(r"range:\s*(\d+)", occurrence_text)
        if len(ranges) < 3:
            return

        line = int(ranges[0])

        # Extract symbol (unescape-aware; see extract_symbol docstring).
        symbol = extract_symbol(occurrence_text)
        if not symbol:
            return

        # Skip local symbols
        if symbol.startswith("local "):
            return

        # Filter symbols by origin
        # Format: "scip-go gomod <module_path> <version> <descriptor>"
        parts = symbol.split(" ")
        if len(parts) >= 5 and parts[0] == "scip-go" and parts[1] == "gomod":
            module_path = parts[2]
            # Skip stdlib (module path = "github.com/golang/go/src" or similar)
            if module_path.startswith("github.com/golang/go"):
                return
            # Skip third-party modules (module path doesn't match our project)
            if self.internal_module and not module_path.startswith(
                self.internal_module
            ):
                return

        # Skip package-only symbols (descriptor ends with "/" after backtick prefix)
        descriptor = parts[-1] if len(parts) >= 5 else symbol
        # Remove backtick module prefix: `module/path`/... -> ...
        descriptor_clean = re.sub(r"`[^`]+`/", "", descriptor)
        if not descriptor_clean or descriptor_clean == "/":
            return

        # Extract symbol_roles
        symbol_roles_match = re.search(r"symbol_roles:\s*(\d+)", occurrence_text)
        symbol_roles = int(symbol_roles_match.group(1)) if symbol_roles_match else 0

        # Extract enclosing range if available (scip-go currently does NOT emit this)
        enclosing_ranges = re.findall(r"enclosing_range:\s*(\d+)", occurrence_text)

        # Process the symbol
        self._process_symbol(symbol, file_path, line, symbol_roles, enclosing_ranges)

    def _make_symbol_key(self, symbol):
        """
        Generate a unique graph vertex key from a Go SCIP symbol.

        Extracts the package-relative path from the backtick module prefix
        and combines it with the cleaned descriptor to ensure uniqueness
        across packages.

        Go SCIP symbols use backticks around the full package path:
            scip-go gomod github.com/user/proj abc `github.com/user/proj/calc`/Calculator#Add().
            -> calc/Calculator#Add

            scip-go gomod github.com/user/proj abc `github.com/user/proj`/NewCalculator().
            -> NewCalculator

            scip-go gomod github.com/user/proj abc `github.com/user/proj`/Calculator#
            -> Calculator

            scip-go gomod github.com/user/proj abc
              `github.com/user/proj/calc`/Calculator#History.
            -> calc/Calculator#History

        Returns:
            Symbol key string (used as graph vertex key), or None if invalid.
        """
        parts = symbol.split(" ")
        if len(parts) < 5:
            return None

        # The descriptor is the last part, with backtick module prefix
        descriptor = parts[-1]

        # Extract the package path from backticks: `github.com/user/proj/calc`
        bt_match = re.search(r"`([^`]+)`", descriptor)
        if not bt_match:
            return None

        full_pkg_path = bt_match.group(1)

        # Compute relative package path by stripping internal_module prefix
        if self.internal_module and full_pkg_path.startswith(self.internal_module):
            rel_pkg = full_pkg_path[len(self.internal_module) :].lstrip("/")
        else:
            rel_pkg = ""

        # Extract the symbol part after backtick prefix: `...`/Symbol#Method().
        symbol_part = re.sub(r"`[^`]+`/", "", descriptor)

        if not symbol_part:
            return None

        # Strip trailing "." (SCIP terminator)
        symbol_part = symbol_part.rstrip(".")

        # Strip trailing "()" for function/method signatures
        if symbol_part.endswith("()"):
            symbol_part = symbol_part[:-2]

        # Strip trailing "#" for type-only descriptors (e.g. Calculator#)
        if symbol_part.endswith("#"):
            symbol_part = symbol_part[:-1]

        if not symbol_part:
            return None

        # Combine: rel_pkg/SymbolDescriptor
        if rel_pkg:
            return f"{rel_pkg}/{symbol_part}"
        return symbol_part

    def _classify_symbol_type(self, symbol_key, original_symbol):
        """
        Classify the symbol type based on Go conventions and SCIP descriptor.

        SCIP descriptor suffixes:
        - "#"   -> type (struct, interface)
        - "()." -> method (if after #) or function
        - "."   -> field, variable, constant
        """
        # Use the original SCIP symbol for classification
        original_part = original_symbol.split(" ")[-1] if original_symbol else ""

        # Method: Type#Method().
        if "#" in original_part and original_part.endswith("()."):
            return NODE_TYPE_METHOD

        # Type: ends with #
        if original_part.endswith("#"):
            return NODE_TYPE_CLASS

        # Field: Type#field.
        if "#" in original_part and original_part.endswith("."):
            return NODE_TYPE_FIELD

        # Function: ends with ().
        if original_part.endswith("()."):
            return NODE_TYPE_FUNCTION

        # Package-level variable/constant: ends with .
        if original_part.endswith("."):
            return NODE_TYPE_FIELD

        return NODE_TYPE_FUNCTION

    def _extract_symbol_display(self, symbol_key, symbol_type=None):
        """Extract human-readable symbol display from the symbol key.

        The symbol_key may contain a package prefix (e.g. calc/Calculator#Add).
        We strip it and format for display.

        Examples:
            calc/Calculator#Add  -> Calculator.Add()
            NewCalculator        -> NewCalculator()
            Calculator           -> Calculator
            calc/Calculator#History -> Calculator.History
        """
        sym = symbol_key

        # Strip package prefix (everything before the last /)
        # But preserve Type#Member structure
        if "/" in sym:
            # Find the symbol part: last segment that contains # or is the leaf
            sym = sym.rsplit("/", 1)[-1]

        # Replace # with .
        sym = sym.replace("#", ".")

        # Remove leading/trailing dots
        sym = sym.strip(".")

        # Add () suffix for functions/methods
        if symbol_type in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD):
            if not sym.endswith("()"):
                sym = f"{sym}()"

        return sym

    def _get_unified_name(self, symbol_key, file_path, symbol_type=None):
        """Generate unified_name in format file_path:SymbolDisplay."""
        symbol_display = self._extract_symbol_display(symbol_key, symbol_type)
        if file_path and symbol_display:
            return f"{file_path}:{symbol_display}"
        return file_path or symbol_display or symbol_key

    def _set_unified_name(self, symbol_key, file_path, symbol_type):
        """Set unified_name attribute on a vertex if it exists."""
        if symbol_key in self.code_graph.name_to_vertex:
            vertex_id = self.code_graph.name_to_vertex[symbol_key]
            self.code_graph.graph.vs[vertex_id]["unified_name"] = (
                self._get_unified_name(symbol_key, file_path, symbol_type)
            )

    def _process_symbol(self, symbol, file_path, line, symbol_roles, enclosing_ranges):
        """Process a symbol occurrence and add it to the graph."""
        self.logger.scip_debug(
            f"Processing Go symbol: {symbol} at {file_path}:{line}, roles: {symbol_roles}"
        )

        # Generate symbol key (graph vertex key)
        symbol_key = self._make_symbol_key(symbol)
        if not symbol_key:
            return

        # Classify symbol type
        symbol_type = self._classify_symbol_type(symbol_key, symbol)

        # Exit scopes based on current line
        try:
            self.code_graph.exit_scopes_by_line(line)
        except Exception as e:
            self.logger.error(f"Error exiting scopes at line {line}: {e}")
            raise

        # Handle definition (check if definition bit is set)
        is_definition = symbol_roles & 1
        if is_definition:
            if enclosing_ranges and len(enclosing_ranges) >= 4:
                # Multi-line enclosing range (scip-go doesn't currently emit this,
                # but handle it for future compatibility)
                scope_start_line = int(enclosing_ranges[0])
                scope_end_line = int(enclosing_ranges[2])

                self.code_graph.add_symbol_node(
                    symbol_key, line, scope_start_line, scope_end_line, symbol_type
                )
                self._set_unified_name(symbol_key, file_path, symbol_type)
                self.code_graph.add_containment_edge(symbol_key)

                if symbol_type in [
                    NODE_TYPE_CLASS,
                    NODE_TYPE_FUNCTION,
                    NODE_TYPE_METHOD,
                ]:
                    self.code_graph.update_current_scope(
                        symbol_key, scope_start_line, scope_end_line
                    )
            elif enclosing_ranges and len(enclosing_ranges) == 3:
                # Single-line enclosing range
                enc_line = int(enclosing_ranges[0])
                self.code_graph.add_symbol_node(
                    symbol_key, line, enc_line, enc_line, symbol_type
                )
                self._set_unified_name(symbol_key, file_path, symbol_type)
                self.code_graph.add_containment_edge(symbol_key)
            else:
                # No enclosing range (current scip-go behavior)
                self.code_graph.add_symbol_node(
                    symbol_key, line, symbol_type=symbol_type
                )
                self._set_unified_name(symbol_key, file_path, symbol_type)
                self.code_graph._add_edge(
                    self.code_graph.current_scope, symbol_key, EDGE_TYPE_CONTAIN
                )

        # Handle reference (not a definition)
        else:
            self.code_graph.add_symbol_reference(
                symbol_key, file_path, symbol_type, anchor_line=line
            )
            if symbol_key in self.code_graph.name_to_vertex:
                vid = self.code_graph.name_to_vertex[symbol_key]
                if not self.code_graph.graph.vs[vid].attributes().get("unified_name"):
                    self.code_graph.graph.vs[vid]["unified_name"] = (
                        self._get_unified_name(symbol_key, file_path, symbol_type)
                    )

    def save_graph(self, output_path):
        """Save the code graph to a file."""
        self.code_graph.save_graph(output_path)
