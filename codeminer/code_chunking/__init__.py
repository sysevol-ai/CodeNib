# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Code chunking module for splitting source code files into semantic chunks.
"""

from importlib import import_module
from typing import Type

from ..languages import chunker_language_aliases, get_chunker_spec
from .base import BaseCodeChunker, CodeChunk
from .cpp_chunker import CppCodeChunker
from .csharp_chunker import CSharpCodeChunker
from .go_chunker import GoCodeChunker
from .java_chunker import JavaCodeChunker
from .js_chunker import JsTsCodeChunker
from .python_chunker import PythonCodeChunker
from .ruby_chunker import RubyCodeChunker
from .rust_chunker import RustCodeChunker


def _load_chunker_class(path: str) -> Type[BaseCodeChunker]:
    """Import a chunker class from a ``module:Class`` registry path."""

    module_name, separator, class_name = path.partition(":")
    if not separator or not module_name or not class_name:
        raise ValueError(f"Invalid chunker path: {path!r}")
    module = import_module(module_name)
    cls = getattr(module, class_name)
    if not issubclass(cls, BaseCodeChunker):
        raise TypeError(f"Registered chunker is not a BaseCodeChunker subclass: {path}")
    return cls


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
    spec = get_chunker_spec(language)
    if spec is None or spec.chunker_language is None or spec.chunker_class is None:
        supported = ", ".join(sorted(chunker_language_aliases()))
        raise ValueError(f"Unsupported language: {language}. Supported: {supported}")

    cls = _load_chunker_class(spec.chunker_class)
    kwargs = {
        "max_lines_per_chunk": max_lines_per_chunk,
        "chunk_depth": chunk_depth,
        "include_header_epilogue": include_header_epilogue,
        "l2_level_exclusive": l2_level_exclusive,
        "skeleton_mode": skeleton_mode,
        "include_l2_in_file_skeleton": include_l2_in_file_skeleton,
    }
    if spec.chunker_pass_language:
        kwargs["language"] = spec.chunker_language
    return cls(**kwargs)


__all__ = [
    "CodeChunk",
    "BaseCodeChunker",
    "PythonCodeChunker",
    "CppCodeChunker",
    "CSharpCodeChunker",
    "RustCodeChunker",
    "GoCodeChunker",
    "JavaCodeChunker",
    "RubyCodeChunker",
    "JsTsCodeChunker",
    "create_chunker",
]
