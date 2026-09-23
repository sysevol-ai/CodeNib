# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The one line a landing-page card says about a repository.

Candidates, in order: the cached Overview's opening thesis, the package
manifest's own description (root, then the workspace member named after the
project), the first README sentence whose subject is the project, and the
README-derived description. Each must read
as a statement of purpose; support notes ("We have a community chat at
Gitter", "For questions and support please use the forum") and behaviour
notes ("By default, bat pipes its own output to a pager") are skipped. When
nothing qualifies the card says nothing rather than something wrong.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable, List, Optional, Sequence, Tuple

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


def _read(bundle: Any, relative: str, max_bytes: int) -> Optional[str]:
    """Read one repository file through the bound reader when there is one."""

    reader = getattr(bundle, "source_reader", None)
    if reader is not None:
        captured = reader.captured_relative_path(relative)
        if captured is None:
            return None
        return reader.read_prefix(captured, max_bytes=max_bytes).decode(
            "utf-8", errors="replace"
        )
    repo_dir = str(getattr(getattr(bundle, "entry", None), "repo_dir", "") or "")
    path = os.path.join(repo_dir, relative) if repo_dir else ""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(max_bytes)
    except OSError:
        return None


def project_names(repo: str) -> List[str]:
    """Names a project goes by: ``vuejs/core`` is Vue, ``valkey-io/valkey``
    is Valkey. The repository name comes first."""

    owner, _, name = (repo or "").lower().partition("/")
    if not name:
        owner, name = "", owner
    names = [name, owner.split("-")[0], owner[:-2] if owner.endswith("js") else ""]
    return [n for n in dict.fromkeys(names) if len(n) >= 2]


def _manifests(bundle: Any, names: Sequence[str]) -> Iterable[Tuple[str, str]]:
    """Root manifests, then the workspace member named after the project."""

    members = [
        (manifest, f"{folder}/{name}/{manifest}")
        for name in names
        for folder, manifest in (("packages", "package.json"), ("crates", "Cargo.toml"))
    ]
    for name, relative in [(n, n) for n in MANIFEST_NAMES] + members:
        text = _read(bundle, relative, _MAX_MANIFEST_BYTES)
        if text is not None:
            yield name, text


def readme_subject_sentence(text: str, names: Sequence[str]) -> str:
    """The first README sentence whose subject is the project itself.

    "Valkey is a high-performance data structure server ..." qualifies;
    "Please make sure to respect issue requirements" does not.
    """

    if not names:
        return ""
    subject = re.compile(
        r"^(?:the\s+)?(?:" + "|".join(re.escape(n) for n in names) + r")(?:\.js)?\b",
        re.IGNORECASE,
    )
    in_fence = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if (
            in_fence
            or not line
            or line.startswith(("#", ">", "<", "|", "!", "[", "-", "*"))
        ):
            continue
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        sentence = re.split(r"(?<=[.!?])\s+", line)[0]
        if (
            subject.match(sentence)
            and len(sentence.split()) >= 6
            and is_purpose_sentence(sentence)
        ):
            return sentence
    return ""


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
    names = project_names(
        str(getattr(getattr(bundle, "entry", None), "repo", "") or "")
    )
    for name, text in _manifests(bundle, names):
        value = manifest_summary(name, text)
        if is_purpose_sentence(value):
            return _trim(value)
    for readme in ("README.md", "README.rst", "README"):
        text = _read(bundle, readme, 256 * 1024)
        if text is not None:
            sentence = readme_subject_sentence(text, names)
            if sentence:
                return _trim(sentence)
            break
    if is_purpose_sentence(description):
        return _trim(description)
    return ""


__all__ = [
    "card_summary",
    "is_purpose_sentence",
    "manifest_summary",
    "project_names",
    "readme_subject_sentence",
]
