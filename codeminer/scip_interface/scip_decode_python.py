#!/usr/bin/env python3
"""
SCIP decoder for Python projects.

Decodes SCIP index files into CodeGraph format, focusing on:
- Classes
- Methods and fields
- Functions
- Module-level references
"""
import re
from pathlib import Path

from ..graph.code_graph import CodeGraph
from ..log_utils import get_logger, register_scip_logger
from ..types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_CLASS,
    NODE_TYPE_FIELD,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    ROOT_NODE,
)
from .scip_indexer_base import extract_scip_blocks, extract_symbol


class SCIPPythonGraphDecoder:
    """
    Decoder for Python SCIP indices.

    Parses decoded SCIP index files and builds a CodeGraph representing:
    - Classes
    - Methods and fields within classes
    - Module-level functions
    - References between symbols
    """

    def __init__(self, index_file_path, project_root=None):
        """
        Initialize the Python SCIP decoder.

        Args:
            index_file_path: Path to the decoded SCIP index file
            project_root: Root directory of the Python project
        """
        self.index_file_path = index_file_path
        self.project_root = project_root
        self.code_graph = CodeGraph(project_root)
        self.indexed_directories = set()
        self.logger = get_logger(__name__)
        register_scip_logger(__name__)

    def decode(self):
        """
        Decode the SCIP index and build the CodeGraph.

        Returns:
            CodeGraph: The constructed code graph
        """
        self.logger.info(f"Starting SCIP Python decode from {self.index_file_path}")
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

        # Process all documents
        for document in document_blocks:
            self._process_document(document)

        self._fix_unified_names()
        return self.code_graph

    def _fix_unified_names(self):
        """Replace module-derived file paths in unified_name with actual file paths.

        During decode, unified_name is built from SCIP's module path
        (e.g. ``sklearn.cluster.k_means_`` → ``sklearn/cluster/k_means_.py``).
        This can produce incorrect paths for ``__init__.py`` packages.
        The vertex ``file`` attribute (from SCIP document ``relative_path``)
        is always correct, so we use it to fix the file path portion.
        """
        for v in self.code_graph.graph.vs:
            name = v["name"]
            file_path = v.attributes().get("file", "")
            node_type = v.attributes().get("type", "")
            if node_type in ("file", "directory"):
                v["unified_name"] = name
            elif file_path and ":" in name:
                symbol_part = name.split(":", 1)[1]
                v["unified_name"] = f"{file_path}:{symbol_part}"

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

        # Iteratively extract the directory path from the file path
        dir_path = Path(file_path).parent
        while dir_path != dir_path.parent:  # Stop at the root directory
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

        # Process occurrences
        # Brace-balanced: see extract_scip_blocks docstring (regex truncates
        # on symbols containing literal "{" / "}").
        occurrences = extract_scip_blocks(document_text, "occurrences")
        for occurrence in occurrences:
            self._process_occurrence(occurrence)

    def _process_occurrence(self, occurrence_text):
        """
        Process a single occurrence from the SCIP index.

        Args:
            occurrence_text: Text content of the occurrence
        """
        # Skip stdlib symbols
        if "python-stdlib" in occurrence_text:
            return

        # Extract ranges
        ranges = re.findall(r"range:\s*(\d+)", occurrence_text)
        if len(ranges) < 3:
            return

        line = int(ranges[0])

        # Extract symbol (unescape-aware; see extract_symbol docstring).
        symbol = extract_symbol(occurrence_text)
        if not symbol:
            return

        # Skip local symbols (scip represents them as 'local <id>')
        if symbol.startswith("local "):
            return

        # Extract symbol_roles
        symbol_roles_match = re.search(r"symbol_roles:\s*(\d+)", occurrence_text)
        if not symbol_roles_match:
            return

        symbol_roles = int(symbol_roles_match.group(1))

        # Extract enclosing range if available
        enclosing_ranges = re.findall(r"enclosing_range:\s*(\d+)", occurrence_text)

        # Process the symbol
        self._process_symbol(symbol, line, symbol_roles, enclosing_ranges)

    def _unify_symbol_name(self, symbol):
        """
        Unify symbol names to a consistent format.

        Examples:
        - src.calculator`/Calculator# -> src/calculator.py:Calculator
        - src.calculator`/Calculator#add(). -> src/calculator.py:Calculator.add()
        - src.utils.helpers`/validate_input(). -> src/utils/helpers.py:validate_input()
        - src.calculator`/Calculator#history. -> src/calculator.py:Calculator.history

        Args:
            symbol: Original symbol name

        Returns:
            Unified symbol name
        """
        # Remove backticks
        clean_symbol = symbol.replace("`", "")

        # Replace dots with slashes in module path and use colon as separator
        if "/" in clean_symbol:
            parts = clean_symbol.split("/", 1)
            module_path = parts[0].replace(".", "/") + ".py"  # Add .py suffix
            if len(parts) > 1:
                symbol_part = parts[1]

                # Handle class and method patterns
                if "#" in symbol_part:
                    # Split on # to separate class from method
                    class_method_parts = symbol_part.split("#", 1)
                    class_name = class_method_parts[0]

                    if len(class_method_parts) > 1 and class_method_parts[1]:
                        # Has method after #
                        method_part = class_method_parts[1].rstrip(".")
                        unified = f"{module_path}:{class_name}.{method_part}"
                    else:
                        # Just class (ends with #)
                        unified = f"{module_path}:{class_name}"
                else:
                    # Function (no # symbol)
                    func_name = symbol_part.rstrip(".")
                    unified = f"{module_path}:{func_name}"
            else:
                unified = module_path
        else:
            # No module path separator, use as-is but clean
            unified = clean_symbol.rstrip(".")

        return unified

    def _classify_symbol_type(self, unified_symbol, original_symbol=None):
        """
        Classify symbol type based on unified symbol format.

        Args:
            unified_symbol: Unified symbol name
            original_symbol: Original symbol name (for additional context)

        Returns:
            Symbol type: NODE_TYPE_CLASS, NODE_TYPE_METHOD, NODE_TYPE_FIELD,
            or NODE_TYPE_FUNCTION
        """
        if ":" in unified_symbol:
            symbol_part = unified_symbol.split(":", 1)[1]
            if "." in symbol_part:
                # Has a dot - could be method or field
                has_parentheses = False
                if original_symbol:
                    has_parentheses = "()" in original_symbol or "(" in original_symbol

                if has_parentheses:
                    return NODE_TYPE_METHOD
                else:
                    return NODE_TYPE_FIELD
            else:
                # Could be class or function
                # Classes typically start with capital letter
                if symbol_part and symbol_part[0].isupper():
                    return NODE_TYPE_CLASS
                else:
                    return NODE_TYPE_FUNCTION
        else:
            return NODE_TYPE_FUNCTION

    def _process_symbol(self, symbol, line, symbol_roles, enclosing_ranges):
        """
        Process a symbol and add it to the code graph.

        Args:
            symbol: Original symbol name from SCIP
            line: Line number of the symbol
            symbol_roles: Symbol roles bitfield (1=definition, 8=reference)
            enclosing_ranges: Enclosing scope range values
        """
        self.logger.scip_debug(
            f"Processing symbol: {symbol} at line {line}, roles: {symbol_roles}"
        )

        # Skip function arguments (symbols ending with .(xxx))
        if re.search(r"\.\([^)]+\)$", symbol):
            return

        # Exit scopes that have ended based on current line
        try:
            stack = self.code_graph.scope_stack
            self.logger.scip_debug(
                f"Scope stack before exit: {[list(s.keys())[0] for s in stack]}"
            )
            self.code_graph.exit_scopes_by_line(line)
            stack = self.code_graph.scope_stack
            self.logger.scip_debug(
                f"Scope stack after exit: {[list(s.keys())[0] for s in stack]}"
            )
        except Exception as e:
            self.logger.error(f"Error exiting scopes at line {line}: {e}")
            raise

        # Parse the symbol
        match = re.search(r"`?([^`]+)`?/([^.]+)(?:\.|\(|#)", symbol)
        if not match:
            return

        module_path = match.group(1)

        # Clean up the symbol by splitting on spaces and taking the last part
        cleaned_symbol = symbol.split(" ")[-1]
        cleaned_symbol = re.sub(r"`", "", cleaned_symbol)

        # Unify symbol name format
        unified_symbol = self._unify_symbol_name(cleaned_symbol)

        # Classify symbol type (pass both original and unified for context)
        symbol_type = self._classify_symbol_type(unified_symbol, cleaned_symbol)

        # Handle __init__ symbols - convert to file reference
        if "/__init__" in cleaned_symbol:
            module_match = re.search(r"(.+)/(?:__init__)", cleaned_symbol)
            if module_match:
                module_path = module_match.group(1)
                file_path = module_path.replace(".", "/") + ".py"

                if symbol_roles == 8:
                    self.code_graph._add_edge(
                        self.code_graph.current_scope, file_path, EDGE_TYPE_REFERENCE
                    )
                return

        # Update current scope if this is a definition with enclosing range
        if symbol_roles == 1 and enclosing_ranges and len(enclosing_ranges) >= 4:
            scope_start_line = int(enclosing_ranges[0])
            scope_end_line = int(enclosing_ranges[2])

            self.code_graph.add_symbol_node(
                unified_symbol, line, scope_start_line, scope_end_line, symbol_type
            )

            self.logger.scip_debug(
                f"Adding containment edge for {unified_symbol}, "
                f"current scope: {self.code_graph.current_scope}"
            )
            self.code_graph.add_containment_edge(unified_symbol)

            if symbol_type in [NODE_TYPE_CLASS, NODE_TYPE_FUNCTION, NODE_TYPE_METHOD]:
                try:
                    self.logger.scip_debug(
                        f"Updating scope to {unified_symbol} "
                        f"[{scope_start_line}-{scope_end_line}]"
                    )
                    self.code_graph.update_current_scope(
                        unified_symbol, scope_start_line, scope_end_line
                    )
                except Exception as e:
                    self.logger.error(f"Error updating scope for {unified_symbol}: {e}")
                    raise

        # Handle definition with no enclosing range
        elif symbol_roles == 1:
            self.logger.scip_debug(
                f"Adding symbol without enclosing range: {unified_symbol}, "
                f"current scope: {self.code_graph.current_scope}"
            )
            self.code_graph.add_symbol_node(
                unified_symbol, line, symbol_type=symbol_type
            )
            self.code_graph._add_edge(
                self.code_graph.current_scope, unified_symbol, EDGE_TYPE_CONTAIN
            )

        # Handle reference (symbol_roles == 8)
        elif symbol_roles == 8:
            self.code_graph.add_symbol_reference(
                unified_symbol, module_path, symbol_type
            )

    def save_graph(self, output_path):
        """
        Save the code graph to a file.

        Args:
            output_path: Path where the graph should be saved
        """
        self.code_graph.save_graph(output_path)
