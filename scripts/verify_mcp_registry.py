#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Validate CodeNib's MCP Registry metadata and published identities."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


SERVER_NAME = "ai.codenib/codenib"
PACKAGE_NAME = "codenib"
SCHEMA_URL = (
    "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
)
PYPI_BASE_URL = "https://pypi.org"
REGISTRY_API = "https://registry.modelcontextprotocol.io/v0.1/servers"


class RegistryValidationError(RuntimeError):
    """Registry metadata or a published identity violates the release contract."""


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise RegistryValidationError(f"{path} must contain a JSON object")
    return value


def _project(root: Path) -> dict[str, Any]:
    with (root / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]


def validate_registry_metadata(root: Path) -> tuple[str, dict[str, Any]]:
    """Validate source metadata and return the package version and server object."""
    project = _project(root)
    version = str(project["version"])
    if project["name"] != PACKAGE_NAME:
        raise RegistryValidationError(
            f"project name must be {PACKAGE_NAME!r}, found {project['name']!r}"
        )

    server = _load_json(root / "server.json")
    if server.get("$schema") != SCHEMA_URL:
        raise RegistryValidationError("server.json must use the pinned MCP schema")
    if server.get("name") != SERVER_NAME:
        raise RegistryValidationError(
            f"server name must be {SERVER_NAME!r}, found {server.get('name')!r}"
        )
    if server.get("version") != version:
        raise RegistryValidationError(
            "server version does not match pyproject.toml: "
            f"{server.get('version')!r} != {version!r}"
        )

    packages = server.get("packages")
    if not isinstance(packages, list) or len(packages) != 1:
        raise RegistryValidationError("server.json must declare exactly one package")
    package = packages[0]
    expected_package_fields = {
        "registryType": "pypi",
        "registryBaseUrl": PYPI_BASE_URL,
        "identifier": PACKAGE_NAME,
        "version": version,
        "runtimeHint": "uvx",
        "transport": {"type": "stdio"},
    }
    for field, expected in expected_package_fields.items():
        if package.get(field) != expected:
            raise RegistryValidationError(
                f"server package {field} must be {expected!r}, "
                f"found {package.get(field)!r}"
            )

    expected_extra = f"{PACKAGE_NAME}[mcp]=={version}"
    runtime_arguments = package.get("runtimeArguments")
    if not isinstance(runtime_arguments, list) or not any(
        argument.get("type") == "named"
        and argument.get("name") == "--with"
        and argument.get("value") == expected_extra
        for argument in runtime_arguments
        if isinstance(argument, dict)
    ):
        raise RegistryValidationError(
            f"uvx runtime arguments must install {expected_extra!r}"
        )

    package_arguments = package.get("packageArguments")
    if not isinstance(package_arguments, list) or len(package_arguments) != 2:
        raise RegistryValidationError(
            "package arguments must contain fixed 'mcp' and one repository path"
        )
    if package_arguments[0] != {"type": "positional", "value": "mcp"}:
        raise RegistryValidationError("the first package argument must select 'mcp'")
    repository_argument = package_arguments[1]
    if not (
        repository_argument.get("type") == "positional"
        and repository_argument.get("valueHint") == "repository"
        and repository_argument.get("format") == "filepath"
        and repository_argument.get("isRequired") is True
    ):
        raise RegistryValidationError(
            "the repository package argument must be a required filepath"
        )

    extras = project.get("optional-dependencies", {})
    mcp_dependencies = extras.get("mcp") if isinstance(extras, dict) else None
    if not isinstance(mcp_dependencies, list) or not any(
        str(dependency).startswith("mcp") for dependency in mcp_dependencies
    ):
        raise RegistryValidationError("the project must publish an MCP extra")

    marker = f"mcp-name: {SERVER_NAME}"
    readme = (root / "README.md").read_text(encoding="utf-8")
    if marker not in readme:
        raise RegistryValidationError(f"README.md is missing {marker!r}")
    return version, server


def validate_server_schema(server: dict[str, Any], schema_path: Path) -> None:
    """Validate server metadata against a pinned, locally supplied JSON schema."""
    try:
        import jsonschema
    except ModuleNotFoundError as exc:
        raise RegistryValidationError(
            "JSON schema validation requires the 'jsonschema' package"
        ) from exc

    schema = _load_json(schema_path)
    try:
        validator_class = jsonschema.validators.validator_for(schema)
        validator_class.check_schema(schema)
        validator_class(schema).validate(server)
    except jsonschema.exceptions.SchemaError as exc:
        raise RegistryValidationError(
            f"invalid MCP server schema: {exc.message}"
        ) from exc
    except jsonschema.exceptions.ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "<root>"
        raise RegistryValidationError(
            f"server.json violates MCP schema at {location}: {exc.message}"
        ) from exc


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "codenib-release"})
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RegistryValidationError(f"{url} returned a non-object response")
    return value


def _wait_for(
    label: str,
    probe: Callable[[], None],
    *,
    timeout: int,
    interval: int = 10,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while True:
        try:
            probe()
            return
        except (RegistryValidationError, urllib.error.URLError) as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            raise RegistryValidationError(
                f"timed out waiting for {label}: {last_error}"
            ) from last_error
        time.sleep(interval)


def wait_for_pypi(version: str, *, timeout: int) -> None:
    """Wait until official PyPI exposes the exact package and ownership marker."""
    marker = f"mcp-name: {SERVER_NAME}"
    url = f"{PYPI_BASE_URL}/pypi/{PACKAGE_NAME}/{version}/json"

    def probe() -> None:
        payload = _get_json(url)
        info = payload.get("info", {})
        if info.get("version") != version:
            raise RegistryValidationError("PyPI version metadata has not converged")
        if marker not in str(info.get("description", "")):
            raise RegistryValidationError("PyPI README lacks the MCP ownership marker")

    _wait_for(f"PyPI {PACKAGE_NAME} {version}", probe, timeout=timeout)


def wait_for_registry(version: str, *, timeout: int) -> None:
    """Wait until the official Registry exposes CodeNib's exact server version."""
    query = urllib.parse.urlencode({"search": SERVER_NAME, "version": version})

    def probe() -> None:
        payload = _get_json(f"{REGISTRY_API}?{query}")
        for entry in payload.get("servers", []):
            server = entry.get("server", {}) if isinstance(entry, dict) else {}
            if server.get("name") == SERVER_NAME and server.get("version") == version:
                return
        raise RegistryValidationError(
            "server version is not visible in Registry search"
        )

    _wait_for(f"MCP Registry {SERVER_NAME} {version}", probe, timeout=timeout)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--check-pypi", action="store_true")
    parser.add_argument("--check-registry", action="store_true")
    parser.add_argument("--schema-file", type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        version, server = validate_registry_metadata(args.root.resolve())
        if args.schema_file:
            validate_server_schema(server, args.schema_file.resolve())
        if args.check_pypi:
            wait_for_pypi(version, timeout=args.timeout)
        if args.check_registry:
            wait_for_registry(version, timeout=args.timeout)
    except (OSError, RegistryValidationError, urllib.error.URLError) as exc:
        print(f"MCP Registry validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"MCP Registry metadata verified: {SERVER_NAME} {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
