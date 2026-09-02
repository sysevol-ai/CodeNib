# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
import sys


def test_storage_exports_stay_compatible_without_eager_runtime_imports() -> None:
    script = """
import sys

import codenib.storage as storage

storage_modules = {
    name for name in sys.modules if name.startswith("codenib.storage.")
}
if storage_modules:
    raise SystemExit(f"storage package imported submodules eagerly: {storage_modules}")

missing = set(storage.__all__) - set(storage._EXPORTS)
extra = set(storage._EXPORTS) - set(storage.__all__)
if missing or extra:
    raise SystemExit(f"lazy export mismatch: missing={missing}, extra={extra}")
if not set(storage.__all__) <= set(dir(storage)):
    raise SystemExit("dir(codenib.storage) omitted public lazy exports")

import codenib.web.app

allowed_modules = {
    "codenib.storage.models",
    "codenib.storage.protocols",
}
loaded = {
    name for name in sys.modules if name.startswith("codenib.storage.")
}
unexpected = loaded - allowed_modules
if unexpected:
    raise SystemExit(
        f"web app imported storage modules outside the portable allowlist: {unexpected}"
    )

for name in storage.__all__:
    getattr(storage, name)
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
