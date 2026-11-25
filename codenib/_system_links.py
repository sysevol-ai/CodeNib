# SPDX-FileCopyrightText: 2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Follow the path links that only the system administrator can replace.

CodeNib pins repository roots and publication folders by walking from ``/``
and refuses any link on the way, because a link can be swapped to point
somewhere else between the check and the use. macOS ships ``/var``, ``/tmp``
and ``/etc`` as root-owned links into ``/private``, so every temporary
directory and ``$TMPDIR`` lies behind one. Whoever can replace such a link
already controls the machine, so following it widens nothing a replaceable
link could redirect. Every other link stays in the path and is still refused.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Callable

# A system link that leads to another system link is followed this many times.
_MAX_SYSTEM_LINK_HOPS = 8


def replaceable_only_by_root(link: os.stat_result, parent: os.stat_result) -> bool:
    """True when only root can change the link or swap it for another entry."""

    return (
        link.st_uid == 0
        and parent.st_uid == 0
        and stat.S_ISDIR(parent.st_mode)
        and not parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def follow_system_links(
    path: Path,
    *,
    lstat: Callable[[str], os.stat_result] = os.lstat,
    readlink: Callable[[str], str] = os.readlink,
) -> Path:
    """Follow the ancestor links that only root can replace.

    macOS ships ``/var``, ``/tmp`` and ``/etc`` as root-owned links into
    ``/private``, so every temporary directory and ``$TMPDIR`` lies behind
    one. Whoever can replace such a link already controls the machine, so
    following it widens nothing a replaceable link could redirect. The walk
    stops at the first link anyone else could replace, and at a missing
    component; the rest of the path is kept as written.
    """

    parts = path.parts
    current = Path(parts[0])
    index = 1
    hops = 0
    while index < len(parts):
        candidate = current / parts[index]
        try:
            metadata = lstat(os.fspath(candidate))
        except OSError:
            break
        if not stat.S_ISLNK(metadata.st_mode):
            current = candidate
            index += 1
            continue
        try:
            parent = lstat(os.fspath(current))
        except OSError:
            break
        if hops >= _MAX_SYSTEM_LINK_HOPS or not replaceable_only_by_root(
            metadata, parent
        ):
            break
        try:
            target = readlink(os.fspath(candidate))
        except OSError:
            break
        hops += 1
        # ``current`` holds no links, so ".." in the target is lexical here.
        resolved = Path(os.path.normpath(os.path.join(current, target)))
        parts = resolved.parts + parts[index + 1 :]
        current = Path(parts[0])
        index = 1
    return Path(current, *parts[index:])
