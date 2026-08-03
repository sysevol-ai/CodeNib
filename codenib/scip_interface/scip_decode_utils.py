#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared normalization helpers for SCIP TextFormat decoders."""

from __future__ import annotations

from collections.abc import Container


def scip_symbol_descriptor(
    symbol: str | None,
    accepted_prefixes: Container[str],
) -> str:
    """Return the raw SCIP descriptor when the symbol belongs to a known tool."""
    if not symbol:
        return ""
    parts = symbol.split(" ")
    if len(parts) < 5 or parts[0] not in accepted_prefixes:
        return ""
    return parts[-1].replace("`", "")


def normalize_scip_descriptor(
    descriptor: str,
    *,
    strip_trailing_dot: bool = True,
    strip_empty_call: bool = True,
    strip_trailing_hash: bool = True,
) -> str:
    """Normalize descriptor suffixes shared by SCIP text decoders."""
    if strip_trailing_dot:
        descriptor = descriptor.rstrip(".")
    if strip_empty_call and descriptor.endswith("()"):
        descriptor = descriptor[:-2]
    if strip_trailing_hash and descriptor.endswith("#"):
        descriptor = descriptor[:-1]
    return descriptor


def format_unified_name(
    file_path: str,
    display_name: str,
    fallback: str,
) -> str:
    """Format the CodeNib unified_name attribute consistently."""
    if file_path and display_name:
        return f"{file_path}:{display_name}"
    return display_name or fallback
