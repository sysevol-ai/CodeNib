# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts import verify_mcp_registry as registry


def test_registry_metadata_matches_package_and_uvx_contract() -> None:
    root = Path(__file__).resolve().parents[1]

    version, server = registry.validate_registry_metadata(root)

    assert version == "0.2.0"
    assert server["name"] == "ai.codenib/codenib"
    package = server["packages"][0]
    assert package["identifier"] == "codenib"
    assert package["runtimeArguments"][0]["value"] == ("codenib[mcp]==0.2.0")
    assert package["packageArguments"][1]["format"] == "filepath"


def test_registry_metadata_rejects_version_drift(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    for filename in ("pyproject.toml", "README.md", "server.json"):
        shutil.copy2(root / filename, tmp_path / filename)
    server = json.loads((tmp_path / "server.json").read_text(encoding="utf-8"))
    server["packages"][0]["version"] = "0.2.0a1"
    (tmp_path / "server.json").write_text(json.dumps(server), encoding="utf-8")

    with pytest.raises(registry.RegistryValidationError, match="package version"):
        registry.validate_registry_metadata(tmp_path)


def test_pypi_probe_requires_exact_version_and_ownership_marker(monkeypatch) -> None:
    monkeypatch.setattr(
        registry,
        "_get_json",
        lambda _url: {
            "info": {
                "version": "0.2.0",
                "description": "<!-- mcp-name: ai.codenib/codenib -->",
            }
        },
    )

    registry.wait_for_pypi("0.2.0", timeout=0)


def test_registry_probe_reads_nested_official_response(monkeypatch) -> None:
    monkeypatch.setattr(
        registry,
        "_get_json",
        lambda _url: {
            "servers": [
                {
                    "server": {
                        "name": "ai.codenib/codenib",
                        "version": "0.2.0",
                    }
                }
            ]
        },
    )

    registry.wait_for_registry("0.2.0", timeout=0)
