# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Trigram-based search backends.

Currently provides a thin client over `sourcegraph/zoekt
<https://github.com/sourcegraph/zoekt>`_, a Go-based code search engine that
indexes source files with a positional trigram index.  Best for sub-50 ms
substring/regex search over millions of lines of code -- complementary to
the symbol-level BM25 and regex indexes, which match on CodeGraph node
content.
"""

from .zoekt_searcher import ZoektSearcher, ZoektUnavailableError

__all__ = ["ZoektSearcher", "ZoektUnavailableError"]
