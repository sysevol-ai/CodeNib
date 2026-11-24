# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Keep a local Wiki on the working tree while it changes.

A local Wiki reads source through a binding captured when the server started;
once a file changes, those reads are refused, so the source panel and code
annotations stopped working after the first applied change or editor save.

``WorktreeFollower`` watches the checkout (``git status``, ignoring
``.codenib/``). When it has changed and stayed still for one poll, it runs
the same ``codenib index`` the Wiki was started with, and then the caller
publishes a new bundle generation and carries forward the written pages whose
files did not change. A failed update is not retried until the tree changes
again.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
import threading
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Set, Tuple

from ..log_utils import get_logger

logger = get_logger(__name__)

Tree = Dict[str, object]
IGNORED_PREFIXES = (".codenib/",)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class WorktreeFollower:
    def __init__(
        self,
        repo: Path,
        argv: Sequence[str],
        *,
        on_updated: Callable[[Set[str]], None],
        before_update: Optional[Callable[[], None]] = None,
        interval: float = 2.0,
        log_path: Optional[Path] = None,
        timeout: float = 3600.0,
    ):
        self.repo = Path(repo)
        self.argv = list(argv)
        self.on_updated = on_updated
        self.before_update = before_update
        self.interval = interval
        self.log_path = log_path
        self.timeout = timeout
        #: "current", "updating" or "failed", for the page to show.
        self.state = "current"
        self.error = ""
        self.updated_at = ""
        self.changed: Set[str] = set()
        self._indexed: Tree = self.tree()
        self._pending: Optional[Tree] = None
        self._failed: Optional[Tree] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- reading the checkout ---------------------------------------------

    def _git(self, *args: str) -> Tuple[int, str]:
        completed = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            errors="surrogateescape",
            check=False,
        )
        return completed.returncode, completed.stdout

    def tree(self) -> Tree:
        """HEAD and every changed or new file with its time and size."""

        code, head = self._git("rev-parse", "HEAD")
        tree: Tree = {"HEAD": head.strip() if code == 0 else ""}
        code, status = self._git(
            "status", "--porcelain=v1", "-z", "--untracked-files=all"
        )
        if code != 0:
            return tree
        records = status.split("\0")
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if len(record) < 4:
                continue
            kind, path = record[:2], record[3:]
            if kind[0] in "RC":
                index += 1  # a rename names its old path next
            if path.startswith(IGNORED_PREFIXES):
                continue
            try:
                stat = (self.repo / path).stat()
                tree[path] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                tree[path] = None
        return tree

    def changed_between(self, old: Tree, new: Tree) -> Set[str]:
        paths = {
            path
            for path in set(old) | set(new)
            if path != "HEAD" and old.get(path) != new.get(path)
        }
        if old.get("HEAD") != new.get("HEAD") and old.get("HEAD") and new.get("HEAD"):
            code, names = self._git(
                "diff", "--name-only", str(old["HEAD"]), str(new["HEAD"])
            )
            if code == 0:
                paths |= {name for name in names.splitlines() if name}
        return {p for p in paths if not p.startswith(IGNORED_PREFIXES)}

    # -- updating ----------------------------------------------------------

    def step(self) -> bool:
        """One poll; True when the Wiki was brought up to date."""

        current = self.tree()
        if current == self._indexed:
            self._pending = None
            if self.state == "failed":
                self.state, self.error = "current", ""
            return False
        if current == self._failed:
            return False
        if current != self._pending:
            # Wait for the tree to hold still for one poll before indexing.
            self._pending = current
            return False
        changed = self.changed_between(self._indexed, current)
        self.state, self.changed = "updating", changed
        try:
            if self.before_update is not None:
                self.before_update()
            log = open(self.log_path, "a") if self.log_path else subprocess.DEVNULL
            try:
                completed = subprocess.run(
                    self.argv,
                    cwd=str(self.repo),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=self.timeout,
                    check=False,
                )
            finally:
                if self.log_path:
                    log.close()  # type: ignore[union-attr]
            if completed.returncode != 0:
                raise RuntimeError(
                    f"updating the index failed (exit {completed.returncode})"
                )
            self.on_updated(changed)
        except Exception as exc:  # noqa: BLE001 - the Wiki keeps serving
            logger.warning("Could not follow the working tree: %s", exc)
            self.state, self.error = "failed", str(exc)
            self._failed = current
            return False
        self._indexed, self._pending, self._failed = current, None, None
        self.state, self.error, self.updated_at = "current", "", _now()
        logger.info("Wiki updated for %d changed file(s)", len(changed))
        return True

    def status(self) -> Dict[str, object]:
        return {
            "state": self.state,
            "error": self.error,
            "updated_at": self.updated_at,
            "changed": sorted(self.changed)[:50],
        }

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.step()
            except Exception:  # noqa: BLE001 - keep watching
                logger.exception("worktree follower step failed")

    def start(self) -> "WorktreeFollower":
        self._thread = threading.Thread(
            target=self._loop, name="codenib-worktree", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
