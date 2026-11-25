# SPDX-FileCopyrightText: 2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Follow the top-level links that only the system administrator can replace.

CodeNib pins repository roots and publication folders by walking from ``/``
and refuses any link on the way, because a link can be swapped to point
somewhere else between the check and the use. macOS ships ``/var``, ``/tmp``
and ``/etc`` as root-owned links into ``/private``, so every temporary
directory and ``$TMPDIR`` lies behind one. Whoever can replace such a link
already controls the machine, so following it widens nothing a replaceable
link could redirect. Only links directly under ``/`` are followed; every
other link stays in the path and is still refused.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Callable

# A top-level link that leads to another is followed this many times.
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
    """Follow a link directly under ``/`` when only root can replace it.

    macOS ships ``/var``, ``/tmp`` and ``/etc`` as such links into
    ``/private``, so every temporary directory and ``$TMPDIR`` lies behind
    one. Whoever can replace them already controls the machine, so following
    them widens nothing a replaceable link could redirect. Deeper links are
    never followed, including root-owned ones such as Linux's ``/proc/self``
    whose target depends on who reads it; neither is a missing component.
    """

    for _ in range(_MAX_SYSTEM_LINK_HOPS):
        parts = path.parts
        if len(parts) < 2:
            return path
        top = Path(parts[0], parts[1])
        try:
            metadata = lstat(os.fspath(top))
            if not stat.S_ISLNK(metadata.st_mode):
                return path
            if not replaceable_only_by_root(metadata, lstat(parts[0])):
                return path
            target = readlink(os.fspath(top))
        except OSError:
            return path
        # The parent is "/", so ".." in the target is lexical here.
        path = Path(os.path.normpath(os.path.join(parts[0], target)), *parts[2:])
    return path
