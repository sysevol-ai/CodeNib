# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Worker that parses part of a pyrefly Glean report.

Started by ``pyrefly_glean._map_glean_files``: reads a JSON list of report
files on stdin and pickles, to the path in ``argv[1]``, one list of records per
file, in the same order.
"""

from __future__ import annotations

import json
import pickle
import sys

from .pyrefly_glean import _glean_file_records


def main() -> int:
    paths = json.load(sys.stdin)
    results = [_glean_file_records(path) for path in paths]
    with open(sys.argv[1], "wb") as handle:
        pickle.dump(results, handle, protocol=pickle.HIGHEST_PROTOCOL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
