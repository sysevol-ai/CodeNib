"""
Python wrapper for the C++ CodeMiner core library.

This module provides a Python interface to the high-performance C++ implementation
of SCIP decoder and CodeGraph. It can fall back to the pure Python implementation
if the C++ module is not available.
"""

import os
import sys
from typing import Optional

# Try to import the C++ extension module
try:
    from codeminer_core import (
        CodeGraph as _CppCodeGraph,
        SCIPGraphDecoder as _CppSCIPGraphDecoder,
        VertexData,
        EdgeData,
        NODE_TYPE_CLASS,
        NODE_TYPE_DIRECTORY,
        NODE_TYPE_FIELD,
        NODE_TYPE_FILE,
        NODE_TYPE_FUNCTION,
        NODE_TYPE_METHOD,
        NODE_TYPE_SYMBOL,
        EDGE_TYPE_CONTAIN,
        EDGE_TYPE_REFERENCE,
        ROOT_NODE,
    )

    CPP_AVAILABLE = True
except ImportError as e:
    CPP_AVAILABLE = False
    _import_error = str(e)

    # Import constants from the Python version
    from .types import (
        NODE_TYPE_CLASS,
        NODE_TYPE_DIRECTORY,
        NODE_TYPE_FIELD,
        NODE_TYPE_FILE,
        NODE_TYPE_FUNCTION,
        NODE_TYPE_METHOD,
        NODE_TYPE_SYMBOL,
        EDGE_TYPE_CONTAIN,
        EDGE_TYPE_REFERENCE,
        ROOT_NODE,
    )


def use_cpp_backend() -> bool:
    """
    Check if the C++ backend is available and should be used.

    Set environment variable CODEMINER_USE_PYTHON=1 to force pure Python implementation.
    """
    if os.environ.get("CODEMINER_USE_PYTHON", "0") == "1":
        return False
    return CPP_AVAILABLE


def get_backend_info() -> dict:
    """Get information about the current backend being used."""
    return {
        "cpp_available": CPP_AVAILABLE,
        "using_cpp": use_cpp_backend(),
        "import_error": _import_error if not CPP_AVAILABLE else None,
    }


class CodeGraph:
    """
    Unified CodeGraph interface that uses C++ backend if available,
    otherwise falls back to Python implementation.
    """

    def __init__(self, project_root: Optional[str] = None):
        """
        Initialize CodeGraph.

        Args:
            project_root: Optional root directory of the project
        """
        if use_cpp_backend():
            if project_root is not None:
                self._impl = _CppCodeGraph(project_root)
            else:
                self._impl = _CppCodeGraph()
            self._backend = "cpp"
        else:
            # Import the Python implementation
            from .graph.code_graph import CodeGraph as PythonCodeGraph
            self._impl = PythonCodeGraph(project_root)
            self._backend = "python"

    @property
    def backend(self) -> str:
        """Return the backend being used ('cpp' or 'python')."""
        return self._backend

    def __getattr__(self, name):
        """Delegate all other attributes/methods to the underlying implementation."""
        return getattr(self._impl, name)

    def __repr__(self):
        return f"<CodeGraph backend={self._backend} {repr(self._impl)}>"


class SCIPGraphDecoder:
    """
    Unified SCIPGraphDecoder interface that uses C++ backend if available,
    otherwise falls back to Python implementation.
    """

    def __init__(self, index_file_path: str, project_root: Optional[str] = None):
        """
        Initialize SCIP decoder.

        Args:
            index_file_path: Path to the SCIP index file
            project_root: Optional root directory of the project
        """
        if use_cpp_backend():
            self._impl = _CppSCIPGraphDecoder(index_file_path, project_root)
            self._backend = "cpp"
        else:
            # Import the Python implementation
            from .scip_interface.scip_decode import SCIPGraphDecoder as PythonSCIPDecoder
            self._impl = PythonSCIPDecoder(index_file_path, project_root)
            self._backend = "python"

    @property
    def backend(self) -> str:
        """Return the backend being used ('cpp' or 'python')."""
        return self._backend

    def decode(self):
        """
        Decode the SCIP index and return a CodeGraph.

        Returns:
            CodeGraph instance (may be C++ or Python backed)
        """
        result = self._impl.decode()

        # If we're using C++ backend, wrap the result in our unified CodeGraph
        if self._backend == "cpp":
            # The C++ decode() returns a C++ CodeGraph
            # We need to wrap it properly
            wrapper = object.__new__(CodeGraph)
            wrapper._impl = result
            wrapper._backend = "cpp"
            return wrapper
        else:
            # Python implementation returns Python CodeGraph
            # Wrap it in our unified interface
            wrapper = object.__new__(CodeGraph)
            wrapper._impl = result
            wrapper._backend = "python"
            return wrapper

    def __getattr__(self, name):
        """Delegate all other attributes/methods to the underlying implementation."""
        return getattr(self._impl, name)

    def __repr__(self):
        return f"<SCIPGraphDecoder backend={self._backend} {repr(self._impl)}>"


# Export all constants and classes
__all__ = [
    "CodeGraph",
    "SCIPGraphDecoder",
    "use_cpp_backend",
    "get_backend_info",
    "CPP_AVAILABLE",
    "NODE_TYPE_CLASS",
    "NODE_TYPE_DIRECTORY",
    "NODE_TYPE_FIELD",
    "NODE_TYPE_FILE",
    "NODE_TYPE_FUNCTION",
    "NODE_TYPE_METHOD",
    "NODE_TYPE_SYMBOL",
    "EDGE_TYPE_CONTAIN",
    "EDGE_TYPE_REFERENCE",
    "ROOT_NODE",
]

if CPP_AVAILABLE:
    __all__.extend(["VertexData", "EdgeData"])
