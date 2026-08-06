# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


def _project() -> dict:
    path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with path.open("rb") as handle:
        return tomllib.load(handle)["project"]


def _names(requirements: list[str]) -> set[str]:
    return {
        requirement.split("[", 1)[0]
        .split(";", 1)[0]
        .split("=", 1)[0]
        .split("<", 1)[0]
        .split(">", 1)[0]
        .strip()
        .lower()
        for requirement in requirements
    }


def test_base_install_is_the_lightweight_wiki_runtime() -> None:
    project = _project()
    base = _names(project["dependencies"])

    assert {
        "fastapi",
        "pydantic",
        "pyyaml",
        "rank_bm25",
        "rich",
        "tree-sitter",
        "tree-sitter-language-pack",
        "uvicorn",
    } <= base
    assert {
        "datasets",
        "faiss-cpu",
        "igraph",
        "litellm",
        "matplotlib",
        "mcp",
        "pytest",
        "sentence-transformers",
    }.isdisjoint(base)


def test_optional_capabilities_have_named_extras() -> None:
    extras = _project()["optional-dependencies"]

    assert {
        "agent",
        "datasets",
        "graph",
        "mcp",
        "semantic",
        "semantic-remote",
        "vertex",
        "zoekt",
        "full",
    } <= set(extras)
    assert "litellm" in _names(extras["agent"])
    assert {"faiss-cpu", "sentence-transformers"} <= _names(extras["semantic"])
    assert {"faiss-cpu", "numpy", "openai"} <= _names(extras["semantic-remote"])
    assert "sentence-transformers" not in _names(extras["semantic-remote"])
    assert "igraph" in _names(extras["graph"])
    assert "mcp" in _names(extras["mcp"])


def test_vertex_capability_uses_litellm_provider_constraints() -> None:
    extras = _project()["optional-dependencies"]
    runtime_requirement = "litellm>=1.93.0"
    provider_requirement = "litellm[google]>=1.93.0"

    assert runtime_requirement in extras["agent"]
    assert runtime_requirement in extras["test"]
    # The provider-specific preset must install both LiteLLM and the Google SDK
    # versions LiteLLM declares compatible. The full preset promises every
    # supported backend, so it must carry the identical coupled requirement.
    assert extras["vertex"] == [provider_requirement]
    assert provider_requirement in extras["full"]
    assert "google-cloud-aiplatform" not in _names(extras["vertex"])


def test_runtime_version_comes_from_distribution_metadata() -> None:
    import codenib
    from codenib._version import package_version

    assert codenib.__version__ == _project()["version"]
    assert package_version() == _project()["version"]


def test_public_release_surfaces_match_project_identity() -> None:
    root = Path(__file__).resolve().parents[1]
    version = _project()["version"]
    readme = (root / "README.md").read_text(encoding="utf-8")
    pages = (root / "docs" / "github_pages.md").read_text(encoding="utf-8")
    web_package = json.loads(
        (root / "web" / "package.json").read_text(encoding="utf-8")
    )
    web_lock = json.loads(
        (root / "web" / "package-lock.json").read_text(encoding="utf-8")
    )

    assert f"CodeNib `{version}` is the current public beta" in readme
    assert "prerelease tag" not in pages
    assert web_package["name"] == "codenib-web"
    assert web_package["version"] == version
    assert web_lock["name"] == web_package["name"]
    assert web_lock["version"] == version
    assert web_lock["packages"][""]["name"] == web_package["name"]
    assert web_lock["packages"][""]["version"] == version
