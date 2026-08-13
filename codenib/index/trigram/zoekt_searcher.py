# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Client for the Zoekt trigram code search engine.

Zoekt is written in Go.  CodeNib interacts with it as a subprocess:

* ``zoekt-git-index``  builds shard files from a git repository
  (handled by :class:`codenib.compiler.index_builders.ZoektIndexBuilder`).
* ``zoekt-webserver``  serves a JSON HTTP API over those shards.  This module
  manages the lifecycle of that subprocess and queries its ``/api/search``
  endpoint.

The Zoekt API returns *file*-level matches with line ranges and snippet
content.  CodeNib's other search backends (BM25, regex) return
*symbol*-level :class:`~codenib.types.NodeInfo` results.  The mapper at
the bottom of this file converts ``FileMatch`` records into ``NodeInfo``
objects with ``type="file"`` so callers can treat all search results
uniformly.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import socket
import subprocess
import time
from typing import Any, Dict, List, Optional

import requests

from ..._atomic_directory import _annotate_secondary_error
from ...types import NodeInfo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

ZOEKT_WEBSERVER_BINARY = "zoekt-webserver"
ZOEKT_INDEX_BINARY = "zoekt-git-index"

DEFAULT_STARTUP_TIMEOUT = 10.0  # seconds to wait for webserver to come up
DEFAULT_SEARCH_TIMEOUT = 10.0  # per-request HTTP timeout


class ZoektUnavailableError(RuntimeError):
    """Raised when the Zoekt binary or shard directory is unavailable."""


class _ZoektProcessOwner:
    """Preinstalled owner spanning Popen construction and caller handoff."""

    __slots__ = ("process",)

    def __init__(self) -> None:
        self.process: Optional[subprocess.Popen[bytes]] = None

    def retain(self, process: subprocess.Popen[bytes]) -> None:
        if self.process is not None and self.process is not process:
            raise RuntimeError("zoekt process ownership changed")
        self.process = process

    def release(self, process: subprocess.Popen[bytes]) -> None:
        if self.process is not process:
            raise RuntimeError("zoekt process ownership changed")
        self.process = None


class _OwnedZoektProcess(subprocess.Popen):
    """Register the Python process object before native process creation."""

    def __init__(self, owner: _ZoektProcessOwner, *args, **kwargs) -> None:
        owner.retain(self)
        super().__init__(*args, **kwargs)


# ---------------------------------------------------------------------------
# Searcher
# ---------------------------------------------------------------------------


class ZoektSearcher:
    """Manage a ``zoekt-webserver`` subprocess and query it over HTTP.

    Parameters
    ----------
    index_dir:
        Directory containing the shards produced by ``zoekt-git-index``.
    port:
        TCP port the webserver should listen on.  Pass ``0`` (default)
        to auto-pick a free port.
    listen_host:
        Hostname / address the webserver binds to.  Defaults to
        ``127.0.0.1`` for safety -- shards may contain proprietary code.
    binary:
        Path or name of the ``zoekt-webserver`` executable.  Looked up on
        ``$PATH`` if a bare name is provided.
    startup_timeout:
        Seconds to wait for the webserver to respond on ``/`` before
        declaring the launch failed.
    """

    def __init__(
        self,
        index_dir: str,
        *,
        port: int = 0,
        listen_host: str = "127.0.0.1",
        binary: str = ZOEKT_WEBSERVER_BINARY,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    ) -> None:
        self.index_dir = index_dir
        self.listen_host = listen_host
        self.binary = binary
        self.startup_timeout = startup_timeout

        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._port: int = port
        self._session = requests.Session()
        self._cleanup_registered = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url(self) -> str:
        return f"http://{self.listen_host}:{self._port}"

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        """Spawn the webserver and wait until it accepts connections."""
        if self.is_running:
            return

        if shutil.which(self.binary) is None and not os.path.isfile(self.binary):
            raise ZoektUnavailableError(
                f"Zoekt binary not found: {self.binary!r}. "
                "Run 'make zoekt-tool' and use 'make active-scip-env' for PATH, "
                "or use the official Docker image."
            )
        if not os.path.isdir(self.index_dir):
            raise ZoektUnavailableError(
                f"Zoekt index directory does not exist: {self.index_dir!r}"
            )

        if self._port == 0:
            self._port = _pick_free_port()

        cmd = [
            self.binary,
            "-listen",
            f"{self.listen_host}:{self._port}",
            "-index",
            self.index_dir,
        ]
        logger.info("Starting zoekt-webserver: %s", " ".join(cmd))
        owner = _ZoektProcessOwner()
        try:
            # The owner is installed into the Popen subclass before its native
            # constructor runs. An interruption at the CALL-return/POP_TOP
            # bytecode boundary therefore cannot orphan the spawned child.
            _OwnedZoektProcess(
                owner,
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            if owner.process is None:
                raise RuntimeError("zoekt process constructor lost ownership")
            self._proc = owner.process
            owner.release(self._proc)

            if not self._cleanup_registered:
                atexit.register(self.stop)
                self._cleanup_registered = True

            self._wait_until_ready()
        except BaseException as primary:  # noqa: B036 - retain spawned child
            if owner.process is not None and self._proc is None:
                self._proc = owner.process
            if self._proc is not None:
                try:
                    self.stop()
                except BaseException as cleanup_error:  # noqa: B036
                    _annotate_secondary_error(
                        primary,
                        "zoekt startup cleanup also failed",
                        cleanup_error,
                    )
                    preferred = (
                        cleanup_error
                        if isinstance(primary, Exception)
                        and not isinstance(cleanup_error, Exception)
                        else primary
                    )
                    if self._proc is not None:
                        try:
                            preferred.zoekt_cleanup_owner = self  # type: ignore[attr-defined]
                        except BaseException:  # noqa: B036 - traceback owns self
                            pass
                    if preferred is cleanup_error:
                        raise cleanup_error from primary
                    raise primary from cleanup_error
            raise

    def stop(self) -> None:
        """Terminate the webserver subprocess if it is still running."""
        proc = self._proc
        if proc is None:
            return
        failure: BaseException | None = None

        def retain_failure(exc: BaseException) -> None:
            nonlocal failure
            if failure is None:
                failure = exc
            else:
                _annotate_secondary_error(
                    failure,
                    "additional zoekt-webserver cleanup failed",
                    exc,
                )

        exited = False
        try:
            exited = proc.poll() is not None
        except BaseException as exc:  # noqa: B036 - owner remains pending
            retain_failure(exc)
        if not exited:
            try:
                proc.terminate()
            except BaseException as exc:  # noqa: B036 - still try kill
                retain_failure(exc)
            try:
                proc.wait(timeout=5)
                exited = True
            except subprocess.TimeoutExpired:
                pass
            except BaseException as exc:  # noqa: B036 - still try kill
                retain_failure(exc)
            if not exited:
                try:
                    proc.kill()
                except BaseException as exc:  # noqa: B036 - retain child
                    retain_failure(exc)
                try:
                    proc.wait(timeout=5)
                    exited = True
                except BaseException as exc:  # noqa: B036 - retain child
                    retain_failure(exc)
        if not exited:
            try:
                exited = proc.poll() is not None
            except BaseException as exc:  # noqa: B036 - retain child
                retain_failure(exc)
        if exited:
            self._proc = None
        elif failure is None:
            failure = RuntimeError("zoekt-webserver cleanup remains pending")
        if failure is not None:
            if self._proc is not None:
                try:
                    failure.zoekt_cleanup_owner = self  # type: ignore[attr-defined]
                except BaseException:  # noqa: B036 - traceback owns self
                    pass
            raise failure

    def _wait_until_ready(self) -> None:
        """Poll until the webserver returns any HTTP response or we time out."""
        assert self._proc is not None
        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                stderr = b""
                if self._proc.stderr is not None:
                    try:
                        stderr = self._proc.stderr.read() or b""
                    except Exception:
                        stderr = b""
                raise ZoektUnavailableError(
                    f"zoekt-webserver exited early (rc={self._proc.returncode}): "
                    f"{stderr.decode('utf-8', errors='replace').strip()}"
                )
            try:
                self._session.get(self.base_url + "/", timeout=1.0)
                return
            except requests.RequestException:
                time.sleep(0.2)
        self.stop()
        raise ZoektUnavailableError(
            f"zoekt-webserver did not become ready within "
            f"{self.startup_timeout:.1f}s (port={self._port})."
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        top_k: int = 20,
        file_filter: Optional[str] = None,
        context_lines: int = 1,
        timeout: float = DEFAULT_SEARCH_TIMEOUT,
    ) -> List[NodeInfo]:
        """Run a Zoekt query and return symbol-compatible :class:`NodeInfo`.

        Parameters
        ----------
        query:
            Zoekt query string.  Supports raw substrings (default,
            case-sensitive), regex via the ``regex:<pattern>`` atom, and
            other atoms documented at
            https://github.com/sourcegraph/zoekt/blob/main/doc/query_syntax.md
            (e.g. ``case:no``, ``lang:python``, ``sym:<name>``).
        top_k:
            Maximum number of file matches to return.
        file_filter:
            Optional glob/regex appended to the query as ``file:<expr>``.
        context_lines:
            Number of context lines requested from Zoekt around each match.
        timeout:
            Per-request HTTP timeout in seconds.

        Notes
        -----
        Zoekt's web server exposes a ``GET /search?format=json`` endpoint
        rather than a JSON body POST.  Query parameters and response shape
        follow Zoekt v16+ (the ``result.FileMatches[]`` schema with
        ``Matches[].LineNum`` and ``Matches[].Fragments[]``).
        """
        if not self.is_running:
            self.start()

        full_query = _compose_query(query, file_filter)
        params = {
            "q": full_query,
            "format": "json",
            "num": str(int(top_k)),
            "ctx": str(int(context_lines)),
        }

        try:
            resp = self._session.get(
                f"{self.base_url}/search",
                params=params,
                timeout=timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ZoektUnavailableError(f"Zoekt search request failed: {exc}") from exc

        try:
            data = resp.json()
        except ValueError as exc:
            raise ZoektUnavailableError(
                f"Zoekt returned non-JSON response: {exc}"
            ) from exc

        # Zoekt may return either {"result": {...}} for content searches or
        # {"repos": {...}} for repo-list queries.  We only surface content
        # matches; repo-only responses become an empty result list.
        result = data.get("result") or {}
        files = result.get("FileMatches") or []
        return [_file_match_to_node(fm) for fm in files[:top_k]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    """Bind to an ephemeral port and immediately release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _compose_query(query: str, file_filter: Optional[str]) -> str:
    if not file_filter:
        return query
    expr = file_filter.strip()
    if not expr:
        return query
    if " " in expr or "(" in expr:
        expr = f'"{expr}"'  # noqa: B907 — Zoekt requires literal quotes here
    return f"{query} file:{expr}"


def _file_match_to_node(fm: Dict[str, Any]) -> NodeInfo:
    """Convert a Zoekt ``FileMatch`` JSON record into a :class:`NodeInfo`.

    The Zoekt web-server JSON schema (``/search?format=json``) is::

        {
          "FileName": "src/auth.py",
          "Repo": "...",
          "Language": "Python",
          "Matches": [
            {
              "LineNum": 10,
              "Fragments": [
                {"Pre": "def ", "Match": "login", "Post": "(user):"}
              ]
            }
          ]
        }

    File matches are collapsed into one ``NodeInfo`` with ``type="file"``;
    Zoekt's ``LineNum`` is 1-based. ``NodeInfo`` is an internal model, so
    ``start_line`` / ``end_line`` normalize the union of matched line numbers
    to 0-based values. ``content`` keeps the original 1-based ``L<n>`` label
    and joins ``Pre + Match + Post`` per fragment so the agent sees the actual
    matched line in context.

    Score is not exposed by the JSON endpoint -- callers needing
    ranking information should fall back to result order, which Zoekt
    sorts by relevance.
    """
    file_name = fm.get("FileName") or ""
    language = fm.get("Language") or ""

    start_line: Optional[int] = None
    end_line: Optional[int] = None
    snippets: List[str] = []

    for m in fm.get("Matches") or []:
        display_line = m.get("LineNum")
        has_source_line = (
            isinstance(display_line, int)
            and not isinstance(display_line, bool)
            and display_line >= 1
        )
        if has_source_line:
            internal_line = display_line - 1
            start_line = (
                internal_line if start_line is None else min(start_line, internal_line)
            )
            end_line = (
                internal_line if end_line is None else max(end_line, internal_line)
            )
        for frag in m.get("Fragments") or []:
            text = "{}{}{}".format(
                frag.get("Pre", "") or "",
                frag.get("Match", "") or "",
                frag.get("Post", "") or "",
            )
            stripped = text.rstrip("\n")
            if stripped:
                prefix = f"L{display_line}: " if has_source_line else ""
                snippets.append(prefix + stripped)

    content = "\n".join(snippets) if snippets else None

    return NodeInfo(
        node_name=file_name,
        type="file",
        file=file_name,
        start_line=start_line,
        end_line=end_line,
        content=content,
        node_id=language or None,
    )
