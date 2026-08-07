# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""TCP port validation shared by the local Wiki entry points."""

from __future__ import annotations

import argparse
from typing import Any

MIN_TCP_PORT = 1
MAX_TCP_PORT = 65_535


def validate_tcp_port(value: Any, *, name: str = "port") -> int:
    """Return a usable TCP port or raise a user-facing value error."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} must be between {MIN_TCP_PORT} and {MAX_TCP_PORT}")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must be between {MIN_TCP_PORT} and {MAX_TCP_PORT}"
        ) from exc
    if not MIN_TCP_PORT <= port <= MAX_TCP_PORT:
        raise ValueError(f"{name} must be between {MIN_TCP_PORT} and {MAX_TCP_PORT}")
    return port


def argparse_tcp_port(value: str) -> int:
    """Argparse ``type`` adapter for :func:`validate_tcp_port`."""
    try:
        return validate_tcp_port(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def validate_local_wiki_ports(*, frontend_port: Any, api_port: Any) -> tuple[int, int]:
    """Validate both local services and reject a deterministic bind conflict."""
    frontend = validate_tcp_port(frontend_port, name="frontend port")
    api = validate_tcp_port(api_port, name="API port")
    if frontend == api:
        raise ValueError("frontend port and API port must be different")
    return frontend, api


__all__ = [
    "MAX_TCP_PORT",
    "MIN_TCP_PORT",
    "argparse_tcp_port",
    "validate_local_wiki_ports",
    "validate_tcp_port",
]
