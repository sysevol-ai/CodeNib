# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Code chunking module for splitting source code files into semantic chunks.
"""

from ..languages import chunker_language_aliases, normalize_chunker_language
from .base import BaseCodeChunker, CodeChunk
from .cpp_chunker import CppCodeChunker
from .go_chunker import GoCodeChunker
from .js_chunker import JsTsCodeChunker
from .python_chunker import PythonCodeChunker
from .rust_chunker import RustCodeChunker


# Factory function to create appropriate chunker
def create_chunker(
    language: str,
    max_lines_per_chunk: int | None = None,
    chunk_depth: int = 2,
    include_header_epilogue: bool = False,
    l2_level_exclusive: bool = True,
    skeleton_mode: bool = False,
    include_l2_in_file_skeleton: bool = True,
) -> BaseCodeChunker:
    """
    Create a code chunker for the specified language.

    Args:
        language: Programming language ('python', 'cpp', 'java', etc.)
        max_lines_per_chunk: Maximum number of lines per emitted chunk. Default:
            None (no splitting)
        chunk_depth: Granularity level
            0 = Entire file as a chunk
            1 = Top-level declarations only
            2 = Include methods/impl members
        include_header_epilogue: Whether to include file headers and epilogues.
            Default: False
        l2_level_exclusive: When chunk_depth is 2, whether to omit L1 container
            nodes (classes/structs/impls) and emit only L2 members. Default: True.
        skeleton_mode: Emit signature-only skeletons instead of full bodies when True.
        include_l2_in_file_skeleton: When chunk_depth is 0, include member
            signatures in file-level skeletons for a hierarchical view. Default: True.

    Returns:
        Language-specific code chunker instance

    Raises:
        ValueError: If the language is not supported
    """
    raw_language = language
    language = normalize_chunker_language(language)
    if language is None:
        supported = ", ".join(sorted(chunker_language_aliases()))
        raise ValueError(
            f"Unsupported language: {raw_language}. Supported: {supported}"
        )

    if language == "python":
        return PythonCodeChunker(
            max_lines_per_chunk=max_lines_per_chunk,
            chunk_depth=chunk_depth,
            include_header_epilogue=include_header_epilogue,
            l2_level_exclusive=l2_level_exclusive,
            skeleton_mode=skeleton_mode,
            include_l2_in_file_skeleton=include_l2_in_file_skeleton,
        )
    elif language == "cpp":
        return CppCodeChunker(
            max_lines_per_chunk=max_lines_per_chunk,
            chunk_depth=chunk_depth,
            include_header_epilogue=include_header_epilogue,
            l2_level_exclusive=l2_level_exclusive,
            skeleton_mode=skeleton_mode,
            include_l2_in_file_skeleton=include_l2_in_file_skeleton,
        )
    elif language == "rust":
        return RustCodeChunker(
            max_lines_per_chunk=max_lines_per_chunk,
            chunk_depth=chunk_depth,
            include_header_epilogue=include_header_epilogue,
            l2_level_exclusive=l2_level_exclusive,
            skeleton_mode=skeleton_mode,
            include_l2_in_file_skeleton=include_l2_in_file_skeleton,
        )
    elif language == "go":
        return GoCodeChunker(
            max_lines_per_chunk=max_lines_per_chunk,
            chunk_depth=chunk_depth,
            include_header_epilogue=include_header_epilogue,
            l2_level_exclusive=l2_level_exclusive,
            skeleton_mode=skeleton_mode,
            include_l2_in_file_skeleton=include_l2_in_file_skeleton,
        )
    elif language in ("javascript", "typescript"):
        return JsTsCodeChunker(
            language=language,
            max_lines_per_chunk=max_lines_per_chunk,
            chunk_depth=chunk_depth,
            include_header_epilogue=include_header_epilogue,
            l2_level_exclusive=l2_level_exclusive,
            skeleton_mode=skeleton_mode,
            include_l2_in_file_skeleton=include_l2_in_file_skeleton,
        )
    else:
        raise ValueError(f"Unsupported language: {language}")


__all__ = [
    "CodeChunk",
    "BaseCodeChunker",
    "PythonCodeChunker",
    "CppCodeChunker",
    "RustCodeChunker",
    "GoCodeChunker",
    "JsTsCodeChunker",
    "create_chunker",
]
