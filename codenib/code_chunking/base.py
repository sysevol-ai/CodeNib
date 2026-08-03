#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Base code chunker class with common functionality.
"""

import json
import os
import sys
import threading
from abc import ABC, abstractmethod
from collections import namedtuple
from pathlib import Path
from typing import ClassVar, List, Optional, Tuple

from tree_sitter import Parser
from tree_sitter_language_pack import get_language

from ..log_utils import get_logger

logger = get_logger(__name__)

DEFAULT_L0_RAW_FALLBACK_MAX_LINES = 300

# Define structures for code chunks
CodeChunk = namedtuple(
    "CodeChunk",
    ["content", "start_line", "end_line", "chunk_type", "name", "file", "node_id"],
)


class BaseCodeChunker(ABC):
    """Base class for language-specific code chunkers."""

    _language_cache: ClassVar[dict[str, object]] = {}
    _language_cache_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        language: str,
        max_lines_per_chunk: Optional[int] = None,
        chunk_depth: int = 2,
        include_header_epilogue: bool = False,
        l2_level_exclusive: bool = True,
        skeleton_mode: bool = False,
        include_l2_in_file_skeleton: bool = True,
    ):
        """
        Initialize the code chunker for a specific language.

        Args:
            language: Programming language to parse ('python', 'cpp', 'java', etc.)
            max_lines_per_chunk: Maximum number of lines per emitted chunk. When set,
                large logical chunks (function/class) will be split into
                multiple sequential chunks of at most this many lines. node_id and name
                remain the same across the split pieces. Default: None (no splitting).
                Set to a number to enable. An L0 skeleton that cannot extract
                any declarations falls back to raw source capped at
                ``DEFAULT_L0_RAW_FALLBACK_MAX_LINES`` when this is None.
            chunk_depth: Depth of AST traversal for chunking:
                0 = Treat entire file as a single chunk
                1 = Top-level only (classes and top-level functions, no methods)
                2 = Method-level (classes, functions, and methods) [default]
            include_header_epilogue: Whether to include file header (imports, module
                docstrings) and epilogue (trailing code) in chunks. Default: False
                (skip them to reduce noise).
            l2_level_exclusive: When chunk_depth is 2 (method/member level), whether
                to exclude the containing type/scope chunks (classes, structs, impls).
                Default: True to keep L2-only output; set to False to emit both L1
                container chunks and L2 members.
            skeleton_mode: When True, emit skeletonized content (signatures only) for
                file- and type-level chunks. File-level skeletons list top-level
                definitions and, when include_l2_in_file_skeleton is True, member
                signatures for a hierarchical view. Type-level skeletons list member
                signatures. Defaults to False (emit full source).
            include_l2_in_file_skeleton: When chunk_depth is 0 and skeleton_mode is
                enabled, whether to include L2/member signatures in the file skeleton
                output. Defaults to True for richer, hierarchical skeletons.
        """
        self.language = language
        self.max_lines_per_chunk = max_lines_per_chunk
        self.chunk_depth = chunk_depth
        self.include_header_epilogue = include_header_epilogue
        self.l2_level_exclusive = l2_level_exclusive
        self.skeleton_mode = skeleton_mode
        self.include_l2_in_file_skeleton = include_l2_in_file_skeleton
        try:
            self.tree_sitter_language = self._get_tree_sitter_language(language)
            self.parser = self._create_parser(self.tree_sitter_language)
            logger.info(
                "Successfully loaded %s language parser from tree-sitter-language-pack",
                language,
            )
        except Exception as e:
            logger.error("Error loading %s parser: %s", language, e)
            sys.exit(1)

    @classmethod
    def _get_tree_sitter_language(cls, language: str):
        """Load each tree-sitter language once per process."""
        with cls._language_cache_lock:
            if language not in cls._language_cache:
                cls._language_cache[language] = get_language(language)
            return cls._language_cache[language]

    @staticmethod
    def _create_parser(language):
        """Create a standard tree_sitter.Parser across tree-sitter versions."""
        try:
            return Parser(language)
        except TypeError:
            parser = Parser()
            if hasattr(parser, "set_language"):
                parser.set_language(language)
            else:
                parser.language = language
            return parser

    def chunk_file(
        self,
        file_path: str,
        relative_path: Optional[str] = None,
        skeleton_mode: Optional[bool] = None,
    ) -> List[CodeChunk]:
        """
        Chunk a code file into function/class level pieces.

        Args:
            file_path: Absolute path to the code file to chunk
            relative_path: Relative path for node_id generation
            skeleton_mode: Override instance-level skeleton setting. When True, chunks contain
                signature-only skeletons instead of full bodies.

        Returns:
            List of CodeChunk objects representing the chunks
        """
        if not os.path.exists(file_path):
            logger.error(f"Error: File {file_path} not found")
            return []

        # logger.debug(f"Chunking file: {file_path}")

        # Read the file
        with open(file_path, "r", encoding="utf-8") as f:
            code_content = f.read()

        # Parse the code
        code_bytes = code_content.encode("utf-8")
        tree = self.parser.parse(code_bytes)
        root_node = tree.root_node

        use_skeleton = self.skeleton_mode if skeleton_mode is None else skeleton_mode
        include_l2_in_skeleton = (
            use_skeleton and self.chunk_depth == 0 and self.include_l2_in_file_skeleton
        )

        # Split content into lines for easier chunk extraction
        lines = code_content.split("\n")

        # Find all top-level functions and classes
        top_level_nodes = self._find_top_level_definitions(
            root_node, include_l2_in_file_skeleton=include_l2_in_skeleton
        )

        # Use relative_path for node_id generation, fallback to file_path
        path_for_node_id = relative_path if relative_path else file_path

        if self.chunk_depth == 0:
            if not lines:
                return []
            if use_skeleton:
                skeleton_content = self._build_file_skeleton(
                    top_level_nodes, code_content
                )
                if not skeleton_content:
                    # L0 is the file view. Declaration-only files and language
                    # constructs not recognized by the skeleton extractor must
                    # remain addressable, while still honoring the configured
                    # payload bound for a raw-source fallback.
                    return self._split_by_max_lines(
                        lines=lines,
                        start_line=0,
                        end_line=len(lines) - 1,
                        chunk_type="file",
                        name=Path(file_path).name,
                        file_path=file_path,
                        node_id=path_for_node_id,
                        line_limit=(
                            self.max_lines_per_chunk
                            if self.max_lines_per_chunk is not None
                            else DEFAULT_L0_RAW_FALLBACK_MAX_LINES
                        ),
                    )
                return [
                    CodeChunk(
                        skeleton_content,
                        0,
                        len(lines) - 1,
                        "file",
                        Path(file_path).name,
                        file_path,
                        path_for_node_id,
                    )
                ]
            return self._split_by_max_lines(
                lines=lines,
                start_line=0,
                end_line=len(lines) - 1,
                chunk_type="file",
                name=Path(file_path).name,
                file_path=file_path,
                node_id=path_for_node_id,
            )

        # Generate chunks
        chunks = self._generate_chunks(
            lines=lines,
            definitions=top_level_nodes,
            file_path=file_path,
            path_for_node_id=path_for_node_id,
            code_content=code_content,
            skeleton_mode=use_skeleton,
        )

        # logger.debug(f"Generated {len(chunks)} chunks from {file_path}")
        return chunks

    def _build_chunk_prefix(self, node_id: str, chunk_type: str, name: str) -> str:
        """
        Build prefix with node_id and class context for methods.

        Args:
            node_id: The node identifier for the chunk
            chunk_type: Type of the chunk (e.g., "method", "function", "class")
            name: Name of the symbol

        Returns:
            Prefix string to prepend to chunk content
        """
        prefix_lines = [node_id]
        if chunk_type == "method" and "." in name:
            class_name = name.split(".")[0]
            prefix_lines.append(f"class {class_name}:")
        return "\n".join(prefix_lines) + "\n"

    def _find_first_child(self, node, target_types: Tuple[str, ...]):
        """Depth-first search for the first child node matching one of target_types."""
        for child in node.children:
            if child.type in target_types:
                return child
            found = self._find_first_child(child, target_types)
            if found:
                return found
        return None

    def _extract_signature_text_default(
        self, node, stop_types: Tuple[str, ...], code_content: str
    ) -> str:
        """
        Default implementation for extracting a signature by trimming the body.
        Subclasses can delegate to this helper with language-specific stop types.
        """
        stop_child = self._find_first_child(node, stop_types)
        stop_byte = stop_child.start_byte if stop_child else node.end_byte
        return code_content[node.start_byte : stop_byte].rstrip()

    def _indent_lines(self, text: str, levels: int = 1) -> str:
        """Indent a block of text by the given number of indentation levels."""
        prefix = "    " * levels
        return "\n".join(
            f"{prefix}{line}" if line.strip() else line for line in text.splitlines()
        )

    def _get_child_definitions(self, node, def_type: str) -> List[Tuple]:
        """
        Return child definitions for container nodes (e.g., methods inside a class).
        Language-specific chunkers override this as needed. Defaults to no children.
        """
        return []

    def _build_definition_skeleton(
        self,
        node,
        def_type: str,
        code_content: str,
        include_children: bool = True,
    ) -> Optional[str]:
        """
        Build a skeleton string for a single definition. Container nodes append child signatures
        when include_children is True.
        """
        signature = self._extract_signature_text(node, def_type, code_content)
        if not signature:
            return None

        child_defs = (
            self._get_child_definitions(node, def_type) if include_children else []
        )
        if not child_defs:
            return signature

        lines = [signature]
        for child_node, _, child_type in child_defs:
            child_signature = self._extract_signature_text(
                child_node, child_type, code_content
            )
            if child_signature:
                lines.append(self._indent_lines(child_signature))
        return "\n".join(lines)

    def _split_by_max_lines(
        self,
        lines: List[str],
        start_line: int,
        end_line: int,
        chunk_type: str,
        name: str,
        file_path: str,
        node_id: str,
        line_limit: Optional[int] = None,
    ) -> List[CodeChunk]:
        """
        Split a logical chunk into pieces if it exceeds the effective line limit.
        Keeps node_id and name unchanged across pieces.
        Uses balanced splitting to distribute lines evenly across chunks.
        """
        prefix = self._build_chunk_prefix(node_id, chunk_type, name)
        total_lines = end_line - start_line + 1
        effective_limit = self.max_lines_per_chunk if line_limit is None else line_limit

        # Calculate number of chunks needed
        if not effective_limit or effective_limit <= 0:
            num_chunks = 1
        else:
            num_chunks = (total_lines + effective_limit - 1) // effective_limit

        # 1. Single chunk: no splitting needed
        if num_chunks == 1:
            content = prefix + "\n".join(lines[start_line : end_line + 1])
            return [
                CodeChunk(
                    content, start_line, end_line, chunk_type, name, file_path, node_id
                )
            ]

        # 2. Multiple chunks: balanced splitting
        base_chunk_size = total_lines // num_chunks
        extra_lines = total_lines % num_chunks
        pieces: List[CodeChunk] = []
        current_start = start_line

        for i in range(num_chunks):
            chunk_size = base_chunk_size + (1 if i < extra_lines else 0)
            current_end = current_start + chunk_size - 1
            piece_content = prefix + "\n".join(lines[current_start : current_end + 1])
            pieces.append(
                CodeChunk(
                    piece_content,
                    current_start,
                    current_end,
                    chunk_type,
                    name,
                    file_path,
                    node_id,
                )
            )
            current_start = current_end + 1

        return pieces

    def _generate_chunks(
        self,
        lines: List[str],
        definitions: List[Tuple],
        file_path: str,
        path_for_node_id: str,
        code_content: str,
        skeleton_mode: bool = False,
    ) -> List[CodeChunk]:
        """
        Generate code chunks from the file content and AST definitions.

        Args:
            lines: List of code lines
            definitions: List of (node, name, type) tuples
            file_path: Path to the source file
            path_for_node_id: Path to use for node_id generation
            code_content: Raw file content for signature extraction
            skeleton_mode: When True, emit skeletonized content

        Returns:
            List of CodeChunk objects
        """
        chunks: List[CodeChunk] = []
        current_line = 0

        for i, (node, name, def_type) in enumerate(definitions):
            start_line = node.start_point[0]  # 0-based
            end_line = node.end_point[0]  # 0-based

            # Create chunk for code before this definition (only for the first one)
            if i == 0 and start_line > current_line and self.include_header_epilogue:
                header_content_lines = lines[current_line:start_line]
                if "\n".join(header_content_lines).strip():  # Only add if not empty
                    # node_id for headers is file path (no symbol)
                    chunks.extend(
                        self._split_by_max_lines(
                            lines=lines,
                            start_line=current_line,
                            end_line=start_line - 1,
                            chunk_type="header",
                            name="header",
                            file_path=file_path,
                            node_id=path_for_node_id,
                        )
                    )

            # Create chunk for the function/class definition
            # Generate node_id in graph format: file_path:symbol_name
            node_id = self._generate_node_id(path_for_node_id, name, def_type)
            if skeleton_mode:
                skeleton = self._build_definition_skeleton(node, def_type, code_content)
                if skeleton:
                    chunks.append(
                        CodeChunk(
                            skeleton,
                            start_line,
                            end_line,
                            def_type,
                            name,
                            file_path,
                            node_id,
                        )
                    )
            else:
                chunks.extend(
                    self._split_by_max_lines(
                        lines=lines,
                        start_line=start_line,
                        end_line=end_line,
                        chunk_type=def_type,
                        name=name,
                        file_path=file_path,
                        node_id=node_id,
                    )
                )

            current_line = end_line + 1

        # Handle any remaining code after the last definition
        if current_line < len(lines) and self.include_header_epilogue:
            remaining_content_lines = lines[current_line:]
            if "\n".join(remaining_content_lines).strip():  # Only add if not empty
                chunks.extend(
                    self._split_by_max_lines(
                        lines=lines,
                        start_line=current_line,
                        end_line=len(lines) - 1,
                        chunk_type="epilogue",
                        name="epilogue",
                        file_path=file_path,
                        node_id=path_for_node_id,
                    )
                )

        return chunks

    def _generate_node_id(
        self, file_path: str, symbol_name: str, symbol_type: str
    ) -> str:
        """
        Generate node_id in the same format as code graph.

        Args:
            file_path: Path to the file (should be relative path)
            symbol_name: Name of the symbol (function/class/method name)
            symbol_type: Type of the symbol ("function", "method", or "class")

        Returns:
            Node ID in format: file_path:symbol_name
        """
        # For functions and methods, add parentheses to match graph format
        if symbol_type in ("function", "method"):
            formatted_name = f"{symbol_name}()"
        else:
            formatted_name = symbol_name

        return f"{file_path}:{formatted_name}"

    @abstractmethod
    def _build_file_skeleton(
        self,
        definitions: List[Tuple],
        code_content: str,
        include_l2: Optional[bool] = None,
    ) -> str:
        """
        Build a skeleton for a file chunk. Implemented per language.
        """
        raise NotImplementedError

    @abstractmethod
    def _find_top_level_definitions(
        self, root_node, include_l2_in_file_skeleton: bool = False
    ) -> List[Tuple]:
        """
        Find all top-level function and class definitions.

        Args:
            root_node: Root node of the AST
            include_l2_in_file_skeleton: Whether to include method/member nodes when
                constructing file-level skeletons.

        Returns:
            List of tuples (node, name, type) for each top-level definition
        """
        pass

    @abstractmethod
    def _extract_signature_text(self, node, def_type: str, code_content: str) -> str:
        """
        Slice out the signature/heading for a node, stopping before the first body-like child.
        Implemented per language for accurate body delimiters.
        """
        pass

    @abstractmethod
    def _extract_function_name(self, node) -> Optional[str]:
        """
        Extract function name from function definition node.

        Args:
            node: AST node representing a function definition

        Returns:
            Function name or None if extraction failed
        """
        pass

    @abstractmethod
    def _extract_class_name(self, node) -> Optional[str]:
        """
        Extract class name from class definition node.

        Args:
            node: AST node representing a class definition

        Returns:
            Class name or None if extraction failed
        """
        pass

    def _find_nodes_by_type(self, root_node, node_type: str):
        """Find all nodes with the given type in the tree."""
        nodes = []

        def traverse(node):
            if node.type == node_type:
                nodes.append(node)
            for child in node.children:
                traverse(child)

        traverse(root_node)
        return nodes

    def save_chunks_to_json(self, chunks: List[CodeChunk], output_path: str):
        """
        Save chunks to a JSON file.

        Args:
            chunks: List of CodeChunk objects
            output_path: Path to save the JSON file
        """
        # Convert chunks to dictionaries for JSON serialization
        chunk_dicts = [
            {
                "content": chunk.content,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "chunk_type": chunk.chunk_type,
                "name": chunk.name,
                "file": chunk.file,
                "node_id": chunk.node_id,
            }
            for chunk in chunks
        ]

        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(chunk_dicts, f, indent=2, ensure_ascii=False)
            logger.info(f"Chunks saved to {output_path}")
        except Exception as e:
            logger.error(f"Error saving chunks to file: {e}")

    def print_chunk_summary(self, chunks: List[CodeChunk]):
        """Print a summary of the generated chunks."""
        logger.info(f"\n=== Chunk Summary ===")
        logger.info(f"Total chunks: {len(chunks)}")

        for i, chunk in enumerate(chunks, 1):
            logger.info(
                "Chunk %s: %s %r (lines %s-%s, %s lines)",
                i,
                chunk.chunk_type,
                chunk.name,
                chunk.start_line,
                chunk.end_line,
                len(chunk.content.split(chr(10))),
            )
