# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Isolated process entry point for the Python regex grep fallback."""

from __future__ import annotations

import json
import sys

from .defaults import _grep_python


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = _grep_python(**payload)
    except Exception as exc:
        print(f"grep fallback rejected its request: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(result)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the parent
    raise SystemExit(main())
