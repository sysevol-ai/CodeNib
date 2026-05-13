# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Language-Server Index Module (non-SCIP)

C/C++ indexing and decoding via clangd .idx files.
"""

from .clangd_decode import ClangdGraphDecoder
from .clangd_indexer import ClangdIndexer

__all__ = [
    "ClangdIndexer",
    "ClangdGraphDecoder",
]
