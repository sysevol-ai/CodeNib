# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The one line a landing-page card says about a repository.

Candidates, in order: the cached Overview's opening thesis, the package
manifest's own description, the README-derived description. Each must read
as a statement of purpose; support notes ("We have a community chat at
Gitter", "For questions and support please use the forum") and behaviour
notes ("By default, bat pipes its own output to a pager") are skipped. When
nothing qualifies the card says nothing rather than something wrong.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable, Optional, Tuple

MANIFEST_NAMES = (
    "package.json",
    "Cargo.toml",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
)
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_CHARS = 200

_NOT_PURPOSE = re.compile(
    r"\b(?:community chat|gitter|discord|slack|forum|mailing list|issue list"
    r"|issues? (?:tracker|requirements|helper)|questions and support"
    r"|pull requests?|contribut\w*|code of conduct|sponsor\w*|donat\w*)\b"
    r"|^(?:by default|when you run|please|note|for developers|to use|for details)\b"
    r"|\bminimum[- ]viable[- ]product\b"
    r"|^this project (?:was|has|is (?:still|currently))\b"
    r"|\b(?:install(?:ation|ing)?|download)\b",
    re.IGNORECASE,
)


def is_purpose_sentence(text: Optional[str]) -> bool:
    """True when *text* reads as what the project is, not how to engage."""

    value = (text or "").strip()
    return len(value.split()) >= 3 and not _NOT_PURPOSE.search(value)


def manifest_summary(name: str, text: str) -> str:
    """The package's own one-line description from a manifest, if any.

    ``setup.py`` is read from its ``setup(`` call on, so a command class's
    ``description`` attribute is not taken for the package's.
    """

    value = ""
    if name == "package.json":
        try:
            data = json.loads(text)
        except ValueError:
            return ""
        value = str(data.get("description") or "") if isinstance(data, dict) else ""
    elif name in {"Cargo.toml", "pyproject.toml"}:
        try:
            import tomllib
        except ImportError:  # pragma: no cover - Python 3.10
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                return ""
        try:
            data = tomllib.loads(text)
        except Exception:  # noqa: BLE001 - a malformed manifest has no summary
            return ""
        for table in ("package", "project"):
            section = data.get(table)
            if isinstance(section, dict) and isinstance(
                section.get("description"), str
            ):
                value = section["description"]
                break
    elif name == "setup.cfg":
        match = re.search(r"^description\s*=\s*(.+)$", text, re.MULTILINE)
        value = match.group(1) if match else ""
    elif name == "setup.py":
        start = text.find("setup(")
        match = re.search(
            r"\bdescription\s*=\s*(['\"])(.{8,200}?)\1",
            text[start:] if start >= 0 else "",
        )
        value = match.group(2) if match else ""
    return re.sub(r"\s+", " ", value).strip()


def _manifests(bundle: Any) -> Iterable[Tuple[str, str]]:
    reader = getattr(bundle, "source_reader", None)
    repo_dir = str(getattr(getattr(bundle, "entry", None), "repo_dir", "") or "")
    for name in MANIFEST_NAMES:
        if reader is not None:
            relative = reader.captured_relative_path(name)
            if relative is None:
                continue
            payload = reader.read_prefix(relative, max_bytes=_MAX_MANIFEST_BYTES)
            yield name, payload.decode("utf-8", errors="replace")
            continue
        path = os.path.join(repo_dir, name) if repo_dir else ""
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                yield name, handle.read(_MAX_MANIFEST_BYTES)
        except OSError:
            continue


def _trim(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= _MAX_CHARS:
        return text
    cut = text.rfind(". ", 0, _MAX_CHARS)
    return text[: cut + 1] if cut > 40 else text[: _MAX_CHARS - 1].rstrip() + "…"


def card_summary(bundle: Any, overview_lead: Optional[str], description: str) -> str:
    """Pick the landing-card line for one repository (see module docstring)."""

    if is_purpose_sentence(overview_lead):
        return _trim(str(overview_lead))
    for name, text in _manifests(bundle):
        value = manifest_summary(name, text)
        if is_purpose_sentence(value):
            return _trim(value)
    if is_purpose_sentence(description):
        return _trim(description)
    return ""


__all__ = ["card_summary", "is_purpose_sentence", "manifest_summary"]
