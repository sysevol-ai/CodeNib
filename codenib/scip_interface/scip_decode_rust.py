#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
SCIP decoder for Rust projects.

Decodes SCIP index files into CodeGraph format, focusing on:
- Structs (treated as classes)
- Impl blocks and methods
- Free functions
- Traits (treated as classes)
"""
import re

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
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


class SCIPRustGraphDecoder:
    """
    Decoder for Rust SCIP indices.

    Parses decoded SCIP index files and builds a CodeGraph representing:
    - Structs as classes
    - Traits as classes
    - Impl blocks and their methods
    - Free functions
    - References between symbols
    """

    def __init__(self, index_file_path, project_root=None):
        """
        Initialize the Rust SCIP decoder.

        Args:
            index_file_path: Path to the decoded SCIP index file
            project_root: Root directory of the Rust project
        """
        self.index_file_path = index_file_path
        self.project_root = Path(project_root) if project_root else None
        self.code_graph = CodeGraph(project_root)
        self.indexed_directories = set()
        self.logger = get_logger(__name__)
        # Register this module for SCIP debug logging
        register_scip_logger(__name__)

        # Internal workspace crate names (loaded lazily on first document)
        self.internal_crates = None

    def decode(self):
        """
        Decode the SCIP index and build the CodeGraph.

        Returns:
            CodeGraph: The constructed code graph
        """
        self.logger.info(f"Starting SCIP Rust decode from {self.index_file_path}")
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
        """
        Process a single document block from the SCIP index.

        Args:
            document_text: Text content of the document block
        """
        # Extract file path
        file_match = re.search(r'relative_path:\s*"([^"]+)"', document_text)
        if not file_match:
            return

        file_path = file_match.group(1)

        # Only process Rust files
        if not file_path.endswith(".rs"):
            return

        # Add directory hierarchy
        dir_path = Path(file_path).parent
        while dir_path != dir_path.parent:  # Stop at the root directory
            dir_path_str = str(dir_path)
            if dir_path_str not in self.indexed_directories:
                self.code_graph.add_directory_node(dir_path_str)
                self.indexed_directories.add(dir_path_str)
                # Add containment edge from parent directory
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

        # Load workspace crate names once (for third-party filtering)
        if self.internal_crates is None:
            self.internal_crates = self._load_workspace_crates()

        # Process occurrences
        # Brace-balanced: see extract_scip_blocks docstring (regex truncates
        # on symbols containing literal "{" / "}").
        occurrences = extract_scip_blocks(document_text, "occurrences")
        for occurrence in occurrences:
            self._process_occurrence(occurrence, file_path)

    def _load_workspace_crates(self):
        """
        Parse Cargo.toml to extract internal workspace crate names.

        Reads [workspace.members] glob patterns, resolves them to directories,
        then reads each member's Cargo.toml to get [package].name.

        Returns:
            Set of internal crate names, or empty set if parsing fails.
        """
        if not self.project_root:
            return set()

        cargo_toml_path = self.project_root / "Cargo.toml"
        if not cargo_toml_path.exists():
            return set()

        try:
            with open(cargo_toml_path, "rb") as f:
                cargo_data = tomllib.load(f)
        except Exception as e:
            self.logger.warning(f"Failed to parse {cargo_toml_path}: {e}")
            return set()

        # Get workspace member patterns
        workspace = cargo_data.get("workspace", {})
        member_patterns = workspace.get("members", [])
        if not member_patterns:
            # Single-crate project: use [package].name
            pkg_name = cargo_data.get("package", {}).get("name")
            return {pkg_name} if pkg_name else set()

        # Resolve glob patterns to actual member directories
        internal_crates = set()

        # Include the root package itself (e.g. ripgrep defines both
        # [package] name="ripgrep" and [workspace] members=["crates/*"]).
        root_pkg_name = cargo_data.get("package", {}).get("name")
        if root_pkg_name:
            internal_crates.add(root_pkg_name)

        for pattern in member_patterns:
            # Malformed member globs ("", trailing slash, absolute paths) make
            # Path.glob raise on Python 3.12; skip them instead of aborting.
            pattern = str(pattern).strip().rstrip("/")
            if not pattern or pattern.startswith("/"):
                continue
            try:
                members = list(self.project_root.glob(pattern))
            except (ValueError, IndexError, OSError):
                continue
            for member_dir in members:
                if not member_dir.is_dir():
                    continue
                member_cargo = member_dir / "Cargo.toml"
                if not member_cargo.exists():
                    continue
                try:
                    with open(member_cargo, "rb") as f:
                        member_data = tomllib.load(f)
                    crate_name = member_data.get("package", {}).get("name")
                    if crate_name:
                        internal_crates.add(crate_name)
                except Exception:
                    continue

        self.logger.info(f"Loaded {len(internal_crates)} internal workspace crates")
        return internal_crates

    def _process_occurrence(self, occurrence_text, file_path):
        """
        Process a single occurrence from the SCIP index.

        Args:
            occurrence_text: Text content of the occurrence
            file_path: Path to the file containing this occurrence
        """
        # Extract ranges
        ranges = re.findall(r"range:\s*(\d+)", occurrence_text)
        if len(ranges) < 3:
            return

        line = int(ranges[0])
        col_start = int(ranges[1])
        col_end = int(ranges[2])

        # Extract symbol (unescape-aware; see extract_symbol docstring).
        symbol = extract_symbol(occurrence_text)
        if not symbol:
            return

        # Skip local symbols (anonymous/unnamed)
        if "local " in symbol:
            return

        # Skip module symbols — last SCIP descriptor ends with "/" (e.g. "linter/", "crate/").
        # In Rust SCIP, "/" suffix is exclusively used for Module kind.
        if symbol.split()[-1].endswith("/"):
            return

        # Filter symbols by origin
        # Format: "rust-analyzer <manager> <crate_name> ..."
        #   cargo <crate>  -> internal or third-party crate
        #   crate          -> current project crate (always keep)
        #   rust-lang/rust -> stdlib (always skip)
        parts = symbol.split(" ")
        if len(parts) >= 3:
            if parts[1] == "cargo":
                if self.internal_crates and parts[2] not in self.internal_crates:
                    return
            elif parts[1] != "crate":
                return

        # Extract symbol_roles
        symbol_roles_match = re.search(r"symbol_roles:\s*(\d+)", occurrence_text)
        if symbol_roles_match:
            symbol_roles = int(symbol_roles_match.group(1))
        else:
            symbol_roles = 0  # Reference

        # Extract enclosing range if available
        enclosing_ranges = re.findall(r"enclosing_range:\s*(\d+)", occurrence_text)

        # Process the symbol
        self._process_symbol(
            symbol, file_path, line, col_start, col_end, symbol_roles, enclosing_ranges
        )

    def _extract_impl_display(self, impl_member, symbol_type=None):
        """Extract display name from impl pattern like [ImplType][Trait]method.

        Args:
            impl_member: Everything after 'impl#', e.g. '[VecBuffer][Buffer]snapshot'
                         or '[Error][From<io::Error>]from'

        Returns:
            Display string like 'VecBuffer.snapshot()' or 'Error<From<io::Error>>.from()'
        """
        impl_member = impl_member.replace("`", "")

        # Parse bracket segments: [ImplType], [Trait], then remaining method
        brackets = []
        rest = impl_member
        while rest.startswith("["):
            # Find matching close bracket (handle nested <> inside)
            depth = 0
            end = -1
            for i, ch in enumerate(rest):
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end == -1:
                break
            brackets.append(rest[1:end])  # content inside [ ]
            rest = rest[end + 1 :]

        # brackets[0] = ImplType, brackets[1] = Trait (if present)
        impl_type = brackets[0] if brackets else ""
        trait_name = brackets[1] if len(brackets) > 1 else ""
        method_name = rest

        # Remove lifetime-only generics (e.g. <'a> or <'a, 'db>)
        # Note: escaped quotes (\\') appear in SCIP symbols
        trait_name = re.sub(r"^<['\\'a-z_,\s]+>$", "", trait_name)
        impl_type = re.sub(r"<['\\'a-z_,\s]+>", "", impl_type)

        # Build display: ImplType[<Trait>].method()
        if trait_name:
            type_display = f"{impl_type}<{trait_name}>"
        else:
            type_display = impl_type

        if method_name:
            suffix = "" if symbol_type == NODE_TYPE_FIELD else "()"
            return f"{type_display}.{method_name}{suffix}"
        else:
            return type_display

    def _extract_symbol_display(self, unified_symbol, symbol_type=None):
        """Extract human-readable symbol display from the unified_symbol key.

        The unified_symbol key is in SCIP format: crate/module/Type#member
        This extracts just the symbol part (Type.member) without crate/module prefix.

        Examples:
            ruff_linter/Checker#check      → Checker.check()
            ruff_linter/check_path         → check_path()
            ruff_linter/Checker            → Checker
            ruff_linter/SourceKind#Jupyter  → SourceKind.Jupyter
            ruff_linter/impl#[VecBuffer][Buffer]snapshot → VecBuffer.snapshot()
        """
        sym = unified_symbol

        if "#" in sym:
            type_path, member = sym.split("#", 1)
            type_name = type_path.rsplit("/", 1)[-1]  # last segment before #

            if type_name == "impl":
                return self._extract_impl_display(member, symbol_type)

            # Clean backticks and lifetime-only generics from member
            member = member.replace("`", "")
            member = re.sub(r"<['\\'a-z_,\s]+>", "", member)

            # Nested type: Type#Variant# → member still has trailing content
            # Strip trailing # from member (e.g. enum variant Type#Variant#)
            member = member.rstrip("#")

            if member:
                suffix = "" if symbol_type == NODE_TYPE_FIELD else "()"
                return f"{type_name}.{member}{suffix}"
            else:
                return type_name
        else:
            func_name = sym.rsplit("/", 1)[-1]
            func_name = func_name.replace("`", "")
            if symbol_type in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD):
                return f"{func_name}()"
            return func_name

    def _get_unified_name(self, unified_symbol, file_path, symbol_type=None):
        """Generate unified_name in format file_path:SymbolDisplay.

        Args:
            unified_symbol: The name key (e.g. 'ruff_linter/Checker#check')
            file_path: File path of the symbol (e.g. 'crates/ruff_linter/src/checker.rs')
            symbol_type: NODE_TYPE_* constant

        Returns:
            Unified name like 'crates/ruff_linter/src/checker.rs:Checker.check()'
        """
        symbol_display = self._extract_symbol_display(unified_symbol, symbol_type)
        if file_path and symbol_display:
            return f"{file_path}:{symbol_display}"
        return file_path or symbol_display or unified_symbol

    def _unify_symbol_name(self, symbol, file_path):
        """
        Unify Rust symbol names, preserving SCIP format for uniqueness.
        Prepends crate name to avoid cross-crate collisions in workspace projects.

        Examples:
        - rust-analyzer cargo nu-cli 0.93.1 commands/main().
          -> nu-cli/commands/main
        - rust-analyzer cargo nu-cli 0.93.1 Options#
          -> nu-cli/Options
        - rust-analyzer cargo nu-cli 0.93.1 Options#area().
          -> nu-cli/Options#area

        Args:
            symbol: Original symbol name from SCIP
            file_path: File path where the symbol was found (stored as node attribute)

        Returns:
            Unified symbol name in SCIP format
            (``crate/module/Type#method`` or ``crate/module/function``)
        """
        # Extract the actual symbol part (after version or URL)
        # Format: "rust-analyzer cargo <crate_name> <version> <symbol_path>"
        # Split into at most 5 parts so that symbol_path (which may contain
        # spaces in generics like `<B, E>`) stays intact.
        parts = symbol.split(" ", 4)
        if len(parts) < 5:
            return None

        # Extract crate name for scoping
        crate_prefix = parts[2] if parts[1] == "cargo" else None

        symbol_part = parts[4].rstrip(".")

        # Clean up trailing markers
        unified = symbol_part.rstrip("#/()")

        # Prepend crate name to avoid cross-crate collisions
        if crate_prefix:
            unified = f"{crate_prefix}/{unified}"

        return unified

    def _classify_symbol_type(self, unified_symbol, original_symbol):
        """
        Classify the symbol type based on Rust conventions.

        Args:
            unified_symbol: Unified symbol name in SCIP format (like "shapes/Rectangle#area")
            original_symbol: Original symbol from SCIP

        Returns:
            Symbol type constant (NODE_TYPE_CLASS, NODE_TYPE_METHOD, NODE_TYPE_FUNCTION)
        """
        # Member: has # with something after it
        if "#" in unified_symbol:
            parts = unified_symbol.split("#")
            if len(parts) > 1 and parts[1]:
                # Distinguish field vs method using original SCIP symbol:
                # method ends with "()." (e.g. "impl#[Type]method().")
                # field ends with "."  (e.g. "Type#field.")
                if original_symbol and original_symbol.endswith("()."):
                    return NODE_TYPE_METHOD
                else:
                    return NODE_TYPE_FIELD
            else:
                # Just type (ends with #)
                return NODE_TYPE_CLASS

        # Check last component to determine type
        last_component = unified_symbol.split("/")[-1]

        # Struct/Trait: starts with uppercase
        if last_component and last_component[0].isupper():
            return NODE_TYPE_CLASS

        # Default to function
        return NODE_TYPE_FUNCTION

    def _process_symbol(
        self,
        symbol,
        file_path,
        line,
        col_start,
        col_end,
        symbol_roles,
        enclosing_ranges,
    ):
        """
        Process a symbol occurrence and add it to the graph.

        Args:
            symbol: Symbol name from SCIP
            file_path: File containing the symbol
            line: Line number
            col_start: Column start
            col_end: Column end
            symbol_roles: Symbol roles (1=definition, 0=reference)
            enclosing_ranges: Enclosing scope ranges
        """
        self.logger.scip_debug(
            f"Processing Rust symbol: {symbol} at {file_path}:{line}, roles: {symbol_roles}"
        )

        # Unify symbol name
        unified_symbol = self._unify_symbol_name(symbol, file_path)
        if not unified_symbol:
            return

        # Classify symbol type
        symbol_type = self._classify_symbol_type(unified_symbol, symbol)

        # Exit scopes based on current line
        try:
            self.code_graph.exit_scopes_by_line(line)
        except Exception as e:
            self.logger.error(f"Error exiting scopes at line {line}: {e}")
            raise

        # Handle definition (check if definition bit is set)
        is_definition = symbol_roles & 1  # Bitwise AND to check definition bit
        if is_definition:
            if enclosing_ranges and len(enclosing_ranges) >= 4:
                # Multi-line enclosing range: [start_line, start_col, end_line, end_col]
                scope_start_line = int(enclosing_ranges[0])
                scope_end_line = int(enclosing_ranges[2])

                self.code_graph.add_symbol_node(
                    unified_symbol, line, scope_start_line, scope_end_line, symbol_type
                )
                if unified_symbol in self.code_graph.name_to_vertex:
                    vertex_id = self.code_graph.name_to_vertex[unified_symbol]
                    self.code_graph.graph.vs[vertex_id]["unified_name"] = (
                        self._get_unified_name(unified_symbol, file_path, symbol_type)
                    )

                self.code_graph.add_containment_edge(unified_symbol)

                if symbol_type in [
                    NODE_TYPE_CLASS,
                    NODE_TYPE_FUNCTION,
                    NODE_TYPE_METHOD,
                ]:
                    self.code_graph.update_current_scope(
                        unified_symbol, scope_start_line, scope_end_line
                    )
            elif enclosing_ranges and len(enclosing_ranges) == 3:
                # Single-line enclosing range: [line, col_start, col_end]
                # Fill range into node but don't push onto scope stack
                enc_line = int(enclosing_ranges[0])
                self.code_graph.add_symbol_node(
                    unified_symbol, line, enc_line, enc_line, symbol_type
                )
                if unified_symbol in self.code_graph.name_to_vertex:
                    vertex_id = self.code_graph.name_to_vertex[unified_symbol]
                    self.code_graph.graph.vs[vertex_id]["unified_name"] = (
                        self._get_unified_name(unified_symbol, file_path, symbol_type)
                    )

                self.code_graph.add_containment_edge(unified_symbol)
            else:
                # No enclosing range (stable rust-analyzer)
                self.code_graph.add_symbol_node(
                    unified_symbol, line, symbol_type=symbol_type
                )
                if unified_symbol in self.code_graph.name_to_vertex:
                    vertex_id = self.code_graph.name_to_vertex[unified_symbol]
                    self.code_graph.graph.vs[vertex_id]["unified_name"] = (
                        self._get_unified_name(unified_symbol, file_path, symbol_type)
                    )

                self.code_graph._add_edge(
                    self.code_graph.current_scope, unified_symbol, EDGE_TYPE_CONTAIN
                )

        # Handle reference (not a definition)
        else:
            self.code_graph.add_symbol_reference(
                unified_symbol, file_path, symbol_type, anchor_line=line
            )
            if unified_symbol in self.code_graph.name_to_vertex:
                vid = self.code_graph.name_to_vertex[unified_symbol]
                if not self.code_graph.graph.vs[vid].attributes().get("unified_name"):
                    self.code_graph.graph.vs[vid]["unified_name"] = (
                        self._get_unified_name(unified_symbol, file_path, symbol_type)
                    )

    def save_graph(self, output_path):
        """
        Save the code graph to a file.

        Args:
            output_path: Path where the graph should be saved
        """
        self.code_graph.save_graph(output_path)
