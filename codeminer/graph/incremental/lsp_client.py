# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Production LSP JSON-RPC client for graph-patching.

Supports the subset of LSP needed for incremental graph updates:
  - textDocument/documentSymbol
  - textDocument/references
  - textDocument/definition
  - textDocument/semanticTokens/full
"""

from __future__ import annotations

import atexit
import json
import os
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from ...languages import lsp_command_for_language, normalize_graph_language
from ...log_utils import get_logger

logger = get_logger(__name__)

_DOCUMENT_UNAVAILABLE_MESSAGES = ("no package metadata for file",)


def is_lsp_document_unavailable_error(error: Optional[dict]) -> bool:
    """Return whether an LSP error declares a document outside its workspace.

    These are deterministic provider exclusions (for example, a Go file whose
    build constraints exclude it), not transport or analysis failures.
    """

    if not error:
        return False
    message = str(error.get("message") or "").lower()
    return any(pattern in message for pattern in _DOCUMENT_UNAVAILABLE_MESSAGES)


# ---------------------------------------------------------------------------
# Per-call profiling (env-controlled, zero overhead when off)
#
# Enable by setting ``LSP_PROFILE_PATH=/path/to/calls.jsonl``. Each call to
# definition / references / semantic_tokens_* writes one line:
#   {"m": method, "f": abs_file, "l": line, "c": char, "ms": elapsed_ms, "n": result_count}
# Aggregate offline to find slow positions.
# ---------------------------------------------------------------------------
_LSP_PROFILE_FH = None
_LSP_PROFILE_PATH: str | None = None


@atexit.register
def _close_profile_fh() -> None:
    if _LSP_PROFILE_FH is not None:
        try:
            _LSP_PROFILE_FH.close()
        except Exception:
            pass


def _profile_log(
    method: str, abs_file: str, line, character, elapsed_s: float, n_results
) -> None:
    """Append one call to the file at ``$LSP_PROFILE_PATH``.

    Re-reads the env var each call and reopens the file when the path
    changes — so a single Python process can cleanly switch profile
    files between strategies (chain runner sets a fresh path per
    (step, strategy) and expects the next calls to land in the new file).
    """
    global _LSP_PROFILE_FH, _LSP_PROFILE_PATH
    path = os.environ.get("LSP_PROFILE_PATH")
    if path != _LSP_PROFILE_PATH:
        if _LSP_PROFILE_FH is not None:
            try:
                _LSP_PROFILE_FH.close()
            except Exception:
                pass  # stale FH; close failures are harmless here
        _LSP_PROFILE_FH = None
        _LSP_PROFILE_PATH = path
        if path:
            try:
                _LSP_PROFILE_FH = open(path, "a", buffering=1)  # line-buffered
            except Exception:
                _LSP_PROFILE_FH = None
    if _LSP_PROFILE_FH is None:
        return
    try:
        _LSP_PROFILE_FH.write(
            json.dumps(
                {
                    "t": round(time.time(), 3),
                    "m": method,
                    "f": abs_file,
                    "l": line,
                    "c": character,
                    "ms": round(elapsed_s * 1000, 2),
                    "n": n_results,
                }
            )
            + "\n"
        )
    except Exception:
        pass  # best-effort profile write — never break real work


# LSP SymbolKind integer → human-readable name
SYMBOL_KIND_NAMES = {
    1: "File",
    2: "Module",
    3: "Namespace",
    4: "Package",
    5: "Class",
    6: "Method",
    7: "Property",
    8: "Field",
    9: "Constructor",
    10: "Enum",
    11: "Interface",
    12: "Function",
    13: "Variable",
    14: "Constant",
    22: "Enum",
    23: "Struct",
    24: "Event",
    25: "Operator",
    26: "TypeParameter",
}

# Common locations to search for LSP binaries not on PATH
_EXTRA_BIN_DIRS = [
    lambda: Path(sys.executable).parent,  # conda/venv bin
    lambda: Path.home() / "go" / "bin",  # Go binaries
    lambda: Path.home() / ".cargo" / "bin",  # Rust binaries
    lambda: Path.home() / ".npm-global" / "bin",  # npm global
    lambda: Path.home() / ".dotnet" / "tools",  # dotnet global tools
    lambda: Path.home() / ".local" / "bin",  # pip --user
]


def resolve_lsp_binary(binary: str) -> Optional[str]:
    """Find the full path to an LSP binary.

    Searches: PATH (via shutil.which) → common install locations.
    Returns resolved path or None.
    """
    resolved = shutil.which(binary)
    if resolved:
        return resolved
    for dir_fn in _EXTRA_BIN_DIRS:
        try:
            candidate = dir_fn() / binary
            if candidate.exists():
                return str(candidate)
        except Exception as exc:
            logger.debug(f"Failed to check {binary} at {candidate}: {exc}")
            continue
    return None


def _lsp_process_env(
    language: str,
    project_root: str | Path | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    # The analyzer binary is selected independently. Let Cargo and proc macros
    # honor the repository's rust-toolchain.toml unless explicitly overridden.
    if language == "rust":
        project_toolchain = env.get("CODEMINER_RUST_PROJECT_TOOLCHAIN")
        if project_toolchain:
            env["RUSTUP_TOOLCHAIN"] = project_toolchain
        else:
            env.pop("RUSTUP_TOOLCHAIN", None)

    if language == "ruby":
        env.pop("GEM_PATH", None)
        gemfile = env.get("CODEMINER_RUBY_BUNDLE_GEMFILE") or env.get("BUNDLE_GEMFILE")
        if gemfile:
            gemfile_path = Path(gemfile).expanduser()
            if not gemfile_path.is_absolute() and project_root is not None:
                gemfile_path = Path(project_root) / gemfile_path
            env["BUNDLE_GEMFILE"] = str(gemfile_path)

    dotnet_root = Path.home() / ".dotnet"
    if "DOTNET_ROOT" not in env and (dotnet_root / "dotnet").exists():
        env["DOTNET_ROOT"] = str(dotnet_root)
        env["DOTNET_ROOT_X64"] = str(dotnet_root)
        env["PATH"] = (
            f"{dotnet_root}{os.pathsep}{dotnet_root / 'tools'}"
            f"{os.pathsep}{env.get('PATH', '')}"
        )
    return env


# File extension → LSP languageId
_EXT_TO_LANG_ID = {
    ".py": "python",
    ".rs": "rust",
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".java": "java",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".phtml": "php",
    ".kt": "kotlin",
    ".kts": "kotlin",
}


class LSPClient:
    """Synchronous LSP client that communicates with a language server via stdio.

    Usage::

        with LSPClient(["rust-analyzer"], "/path/to/project", "rust") as client:
            symbols = client.document_symbol("src/main.rs")
            refs = client.references("src/main.rs", 10, 4)
    """

    def __init__(
        self,
        command: list[str],
        project_root: str,
        language: str,
        init_options: Optional[dict] = None,
        max_in_flight_requests: int = 10,
    ):
        if max_in_flight_requests < 1:
            raise ValueError("max_in_flight_requests must be positive")
        self.command = command
        self.project_root = Path(project_root).resolve()
        self.root_uri = self.project_root.as_uri()
        self.language = language
        self.init_options = init_options
        self.process: Optional[subprocess.Popen] = None
        self._next_id = 1
        self._opened_files: set[str] = set()
        self._document_versions: dict[str, int] = {}

        # Populated during start() from server capabilities
        self.semantic_tokens_legend: Optional[dict] = None
        self.supports_semantic_tokens_range: bool = False

        # Active $/progress tokens (set by _handle_progress, cleared by
        # 'end' notifications). wait_until_idle() polls this for emptiness
        # to know when LSP background work has finished.
        self._active_progress: dict = {}

        # Pending request tracking: limit in-flight requests to avoid
        # overwhelming the LSP server (clangd hangs after ~570 queued requests)
        self._pending_ids: set[int] = set()
        self._max_pending = max_in_flight_requests

        # Buffer for out-of-order responses: when _request reads a response
        # for a different request ID, it stores it here instead of discarding.
        self._response_buffer: dict[int, dict] = {}

        # Raw bytes can contain more than one JSON-RPC frame. Keep unread
        # frames across calls so a response cannot discard a trailing
        # notification or another response from the same pipe read.
        self._read_buffer = b""

        # Track the last error from _request so callers can decide whether
        # to retry (transient errors like -32801) or not (permanent errors).
        self._last_error: Optional[dict] = None
        self.strict_request_failures: bool = False
        self.operation_retries: int = 0
        self.document_symbol_timeout_s: float = 10.0
        self.definition_timeout_s: float = 10.0
        self.semantic_tokens_timeout_s: float = 15.0
        self.reference_timeout_s: float = 10.0
        self.reference_retries: int = 0

    @property
    def last_error(self) -> Optional[dict]:
        """Return a copy of the most recent request error, if any."""
        return dict(self._last_error) if self._last_error is not None else None

    def _raise_if_strict_failure(self, method: str, context: str) -> None:
        error = self.last_error
        if (
            self.strict_request_failures
            and error is not None
            and error.get("code") != -2
            and not is_lsp_document_unavailable_error(error)
        ):
            raise RuntimeError(f"LSP {method} failed for {context}: {error}")

    # ── Lifecycle ─────────────────────────────────────────────

    def start(self, skip_probe: bool = False) -> dict:
        """Start the LSP server and send initialize/initialized.

        Args:
            skip_probe: If True, skip the blocking readiness probe.
                Use this when starting early for warm-up — the server
                will analyze the project in the background.
        """
        logger.info(f"Starting LSP server: {' '.join(self.command)}")
        # A client object can be restarted after a failed strategy attempt. No
        # transport state from the old process is valid for the new JSON-RPC
        # session.
        self._next_id = 1
        self._pending_ids.clear()
        self._response_buffer.clear()
        self._read_buffer = b""
        self._active_progress.clear()
        self._last_error = None
        env = _lsp_process_env(self.language, self.project_root)
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.project_root),
            env=env,
        )

        # Build capabilities we request
        capabilities = {
            "textDocument": {
                "documentSymbol": {
                    "hierarchicalDocumentSymbolSupport": True,
                },
                "references": {},
                "definition": {
                    "linkSupport": True,
                },
                "semanticTokens": {
                    "requests": {"full": True},
                    "tokenTypes": [],
                    "tokenModifiers": [],
                    "formats": ["relative"],
                },
            },
            "window": {
                "workDoneProgress": True,
            },
        }

        # Language-specific initialization options
        init_opts = dict(self.init_options) if self.init_options else {}
        if self.language == "go":
            # gopls disables semanticTokens by default — enable it
            init_opts.setdefault("semanticTokens", True)

        params = {
            "processId": os.getpid(),
            "rootUri": self.root_uri,
            "rootPath": str(self.project_root),
            "capabilities": capabilities,
            "workspaceFolders": [
                {"uri": self.root_uri, "name": self.project_root.name}
            ],
        }
        if init_opts:
            params["initializationOptions"] = init_opts

        result = self._request("initialize", params, timeout=120)
        self._notify("initialized", {})

        # Extract semantic tokens legend from server capabilities
        if result:
            sem_provider = result.get("capabilities", {}).get(
                "semanticTokensProvider", {}
            )
            if isinstance(sem_provider, dict):
                self.semantic_tokens_legend = sem_provider.get("legend")
                self.supports_semantic_tokens_range = bool(sem_provider.get("range"))

        # Wait for server readiness (unless skipped for warm-up)
        if not skip_probe:
            self._wait_for_ready()

        logger.info("LSP server initialized successfully")
        return result or {}

    def shutdown(self):
        """Gracefully shut down the LSP server."""
        if self.process is None:
            return
        try:
            self._request("shutdown", None, timeout=10)
            self._notify("exit", None)
            self.process.wait(timeout=5)
        except Exception:
            if self.process:
                self.process.kill()
                self.process.wait()
        finally:
            self.process = None
            self._opened_files.clear()
            self._document_versions.clear()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.shutdown()

    # ── Document management ───────────────────────────────────

    def open_document(self, file_path: str):
        """Send textDocument/didOpen for a file (idempotent)."""
        abs_path = self._abs_path(file_path)
        uri = Path(abs_path).as_uri()
        if uri in self._opened_files:
            return
        try:
            text = Path(abs_path).read_text(errors="replace")
        except FileNotFoundError:
            logger.warning(f"Cannot open {abs_path}: file not found")
            return

        self._notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": self._detect_language_id(abs_path),
                    "version": 1,
                    "text": text,
                },
            },
        )
        self._opened_files.add(uri)
        self._document_versions[uri] = 1

    def sync_document(self, file_path: str):
        """Publish the current on-disk text after an external file change.

        Incremental graph updates commonly move the worktree with Git rather
        than through an editor. Once a document is open, language servers use
        their in-memory buffer and do not observe that change reliably. Send
        a full-content ``didChange`` with a monotonically increasing version;
        unopened files use the normal ``didOpen`` path.
        """
        abs_path = self._abs_path(file_path)
        uri = Path(abs_path).as_uri()
        if uri not in self._opened_files:
            self.open_document(file_path)
            return

        try:
            text = Path(abs_path).read_text(errors="replace")
        except FileNotFoundError:
            logger.warning(f"Cannot sync {abs_path}: file not found")
            return

        version = self._document_versions.get(uri, 1) + 1
        self._notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            },
        )
        self._document_versions[uri] = version

    def close_document(self, file_path: str):
        """Send textDocument/didClose for a file."""
        abs_path = self._abs_path(file_path)
        uri = Path(abs_path).as_uri()
        if uri not in self._opened_files:
            return
        self._notify(
            "textDocument/didClose",
            {
                "textDocument": {"uri": uri},
            },
        )
        self._opened_files.discard(uri)
        self._document_versions.pop(uri, None)

    def wait_for_analysis(
        self,
        file_path: str,
        max_wait: float = 15.0,
        poll_interval: float = 0.5,
    ) -> bool:
        """Wait for the LSP server to finish analyzing a file.

        After didOpen/didChange, servers like pyright need time to
        re-analyze before references() returns correct results.
        We poll with a lightweight hover request until it responds
        quickly, indicating analysis is complete.
        """
        abs_path = self._abs_path(file_path)
        uri = Path(abs_path).as_uri()
        start = time.time()
        while time.time() - start < max_wait:
            t0 = time.time()
            try:
                self._request(
                    "textDocument/hover",
                    {
                        "textDocument": {"uri": uri},
                        "position": {"line": 0, "character": 0},
                    },
                    timeout=5,
                )
            except Exception as exc:
                logger.debug(f"Analysis probe failed: {exc}")
            elapsed = time.time() - t0
            error = self.last_error
            responsive = (
                error is None
                or error.get("code") == -2
                or is_lsp_document_unavailable_error(error)
            )
            # A fast result (including a valid null hover) is a readiness
            # signal; a fast transport/server error is not.
            if elapsed < 1.0 and responsive:
                return True
            time.sleep(poll_interval)
        return False

    # ── LSP queries ───────────────────────────────────────────

    def document_symbol(
        self,
        file_path: str,
        retries: Optional[int] = None,
        retry_delay: float = 0.5,
        timeout: Optional[float] = None,
    ) -> list[dict]:
        """Get hierarchical document symbols for a file.

        Default ``retries=0`` — null is a legitimate "no symbols" answer
        per LSP spec. Previous default of 5 retries × 5s sleep wasted up
        to 25s/call when servers returned null. If the server is still
        loading at warmup time, callers should wait at the call-site,
        not inside this primitive.
        """
        t0 = time.monotonic()
        retries = self.operation_retries if retries is None else retries
        timeout = self.document_symbol_timeout_s if timeout is None else timeout
        self.open_document(file_path)
        abs_path = self._abs_path(file_path)
        uri = Path(abs_path).as_uri()

        for attempt in range(retries + 1):
            result = self._request(
                "textDocument/documentSymbol",
                {
                    "textDocument": {"uri": uri},
                },
                timeout=timeout,
            )
            if result is not None:
                _profile_log(
                    "documentSymbol",
                    file_path,
                    None,
                    None,
                    time.monotonic() - t0,
                    len(result) if isinstance(result, list) else 0,
                )
                return result
            if self._last_error and self._last_error.get("code") == -2:
                break
            if not self._is_retryable_error():
                break
            if attempt < retries:
                logger.debug(
                    f"documentSymbol returned null for {file_path}, "
                    f"retrying in {retry_delay}s ({attempt + 1}/{retries})"
                )
                time.sleep(retry_delay)

        _profile_log("documentSymbol", file_path, None, None, time.monotonic() - t0, 0)
        self._raise_if_strict_failure("textDocument/documentSymbol", file_path)
        return []

    def references(self, *args, **kwargs):
        t0 = time.monotonic()
        out = self._references_inner(*args, **kwargs)
        _profile_log(
            "references",
            args[0] if args else kwargs.get("file_path"),
            args[1] if len(args) > 1 else kwargs.get("line"),
            args[2] if len(args) > 2 else kwargs.get("character"),
            time.monotonic() - t0,
            len(out) if out else 0,
        )
        return out

    def references_batch(
        self,
        queries: list[tuple[str, int, int]],
        *,
        include_declaration: bool = False,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
        max_in_flight: Optional[int] = None,
    ) -> list[tuple[list[dict], Optional[dict]]]:
        """Resolve reference queries through one bounded JSON-RPC window.

        The server may answer requests out of order. Results retain input order,
        and each request keeps the same timeout/retry semantics as
        :meth:`references`. JSON-RPC ``null`` remains a valid empty result.
        """

        if not queries:
            return []
        timeout = self.reference_timeout_s if timeout is None else timeout
        retries = self.reference_retries if retries is None else retries
        params = []
        for file_path, line, character in queries:
            self.open_document(file_path)
            uri = Path(self._abs_path(file_path)).as_uri()
            params.append(
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": line, "character": character},
                    "context": {"includeDeclaration": include_declaration},
                }
            )

        outcomes: list[Optional[tuple[list[dict], Optional[dict]]]] = [None] * len(
            queries
        )
        elapsed_s = [0.0] * len(queries)
        remaining = list(range(len(queries)))
        for _attempt in range(retries + 1):
            batch = self._request_batch(
                "textDocument/references",
                [params[index] for index in remaining],
                timeout=timeout,
                max_in_flight=max_in_flight,
            )
            retry_indices = []
            for index, (result, error, elapsed) in zip(remaining, batch, strict=True):
                elapsed_s[index] += elapsed
                if result is not None:
                    outcomes[index] = (result, None)
                    continue
                if error and error.get("code") == -2:
                    outcomes[index] = ([], error)
                    continue
                if (
                    error
                    and error.get("code") in {-1, -32801, -32802}
                    and _attempt < retries
                ):
                    retry_indices.append(index)
                    continue
                outcomes[index] = ([], error)
            remaining = retry_indices
            if not remaining:
                break

        resolved = []
        for index, outcome in enumerate(outcomes):
            result, error = outcome or (
                [],
                {"code": -1, "message": "batch request did not complete"},
            )
            file_path, line, character = queries[index]
            _profile_log(
                "references",
                file_path,
                line,
                character,
                elapsed_s[index],
                len(result),
            )
            resolved.append((result, error))
        self._last_error = next(
            (
                error
                for _result, error in resolved
                if error is not None and error.get("code") != -2
            ),
            None,
        )
        return resolved

    def _references_inner(
        self,
        file_path: str,
        line: int,
        character: int,
        include_declaration: bool = False,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
    ) -> list[dict]:
        """Find all references to the symbol at the given position.

        Default ``timeout=10``. Across 25 v8 trials there were 0 references
        calls that took >10s and returned a non-empty result (longest
        non-empty was 9.6s). Calls hitting 30s were always rust-analyzer /
        basedpyright pathological hangs returning n=0. Cap at 10s to fail
        fast on hangs while keeping all legitimate slow calls.

        Default ``retries=0``. Per LSP 3.17 spec, null is a legitimate
        response meaning "no references at this position". Retrying on
        null wastes 9s (3×3s sleep) per call; with N callers many calls
        return null and the cumulative cost was the dominant patcher
        bottleneck. Caller-side warmup handles "server still loading".
        Checks response buffer for late-arriving responses from prior
        attempts.
        """
        timeout = self.reference_timeout_s if timeout is None else timeout
        retries = self.reference_retries if retries is None else retries
        self.open_document(file_path)
        abs_path = self._abs_path(file_path)
        uri = Path(abs_path).as_uri()
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": include_declaration},
        }
        prior_ids = []
        for attempt in range(retries + 1):
            # Check if a prior attempt's response arrived late
            for pid in prior_ids:
                if pid in self._response_buffer:
                    message = self._response_buffer.pop(pid)
                    if "error" not in message and message.get("result") is not None:
                        logger.debug(f"references: using buffered response id={pid}")
                        return message["result"]

            result = self._request("textDocument/references", params, timeout=timeout)
            if result is not None:
                if attempt > 0:
                    logger.debug(f"references retry succeeded on attempt {attempt + 1}")
                return result
            if self._last_error and self._last_error.get("code") == -2:
                # JSON-RPC null is the LSP's valid "no references" result,
                # not evidence that the request should be retried.
                return []
            # Record this attempt's id for buffer check on next retry
            prior_ids.append(self._next_id - 1)
            if not self._is_retryable_error():
                logger.warning(
                    f"references failed for {file_path}:{line} (permanent error)"
                )
                self._raise_if_strict_failure(
                    "textDocument/references", f"{file_path}:{line}:{character}"
                )
                return []
            if attempt < retries:
                reason = (
                    self._last_error.get("message", "unknown")
                    if self._last_error
                    else "null result"
                )
                logger.debug(
                    f"references failed for {file_path}:{line} ({reason}), "
                    f"retrying ({attempt + 1}/{retries})"
                )
                time.sleep(0.5)
        if retries > 0:
            logger.warning(
                f"references failed for {file_path}:{line} after {retries} retries"
            )
        self._raise_if_strict_failure(
            "textDocument/references", f"{file_path}:{line}:{character}"
        )
        return []

    def definition(self, *args, **kwargs):
        t0 = time.monotonic()
        out = self._definition_inner(*args, **kwargs)
        _profile_log(
            "definition",
            args[0] if args else kwargs.get("file_path"),
            args[1] if len(args) > 1 else kwargs.get("line"),
            args[2] if len(args) > 2 else kwargs.get("character"),
            time.monotonic() - t0,
            len(out) if out else 0,
        )
        return out

    def _definition_inner(
        self,
        file_path: str,
        line: int,
        character: int,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
    ) -> list[dict]:
        """Go to definition of the symbol at the given position.

        Returns a list of Location or LocationLink objects.

        Default ``timeout=10``: longest non-empty definition across 25 v8
        trials was 1.9s. Hits at 30s were all rust-analyzer / basedpyright
        timeouts on pathological positions returning n=0.

        Default ``retries=0`` because the most common cause of a null
        response is "no definition at this position" (a legitimate answer
        for whitespace, comments, or already-resolved tokens) — NOT a
        transient error. The previous default of 3 retries × 3s sleep
        wasted ~9s per null token; on xarray bin=30 that pushed
        ``reconnect_outgoing`` from a few seconds to 39s. Callers that
        want to wait for server warmup should retry at the call-site,
        not inside this primitive. Checks response buffer for
        late-arriving responses from prior attempts.
        """
        timeout = self.definition_timeout_s if timeout is None else timeout
        retries = self.operation_retries if retries is None else retries
        self.open_document(file_path)
        abs_path = self._abs_path(file_path)
        uri = Path(abs_path).as_uri()
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        }
        prior_ids = []
        for attempt in range(retries + 1):
            # Check if a prior attempt's response arrived late
            for pid in prior_ids:
                if pid in self._response_buffer:
                    message = self._response_buffer.pop(pid)
                    if "error" not in message and message.get("result") is not None:
                        logger.debug(f"definition: using buffered response id={pid}")
                        result = message["result"]
                        if isinstance(result, dict):
                            return [result]
                        return result

            result = self._request("textDocument/definition", params, timeout=timeout)
            if result is not None:
                if attempt > 0:
                    logger.debug(f"definition retry succeeded on attempt {attempt + 1}")
                if isinstance(result, dict):
                    return [result]
                return result
            if self._last_error and self._last_error.get("code") == -2:
                return []
            prior_ids.append(self._next_id - 1)
            if not self._is_retryable_error():
                logger.warning(
                    f"definition failed for {file_path}:{line}:{character} "
                    f"(permanent error)"
                )
                self._raise_if_strict_failure(
                    "textDocument/definition", f"{file_path}:{line}:{character}"
                )
                return []
            if attempt < retries:
                reason = (
                    self._last_error.get("message", "unknown")
                    if self._last_error
                    else "null result"
                )
                logger.debug(
                    f"definition failed for {file_path}:{line}:{character} ({reason}), "
                    f"retrying ({attempt + 1}/{retries})"
                )
                time.sleep(0.5)
        if retries > 0:
            logger.warning(
                f"definition failed for {file_path}:{line}:{character} "
                f"after {retries} retries"
            )
        self._raise_if_strict_failure(
            "textDocument/definition", f"{file_path}:{line}:{character}"
        )
        return []

    def semantic_tokens_full(
        self,
        file_path: str,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
    ) -> Optional[dict]:
        """Get semantic tokens for the entire file.

        Returns raw response with ``data`` field (delta-encoded integers).
        Use ``decode_semantic_tokens()`` to decode.
        Returns None on timeout or error (details logged by _request).

        Default timeout 15s: a server that needs longer is pathological
        (basedpyright on sklearn's test_pls.py used to take 60s). The
        patcher tolerates None — that file simply skips outgoing-ref
        discovery for this run. 60s × N pathological files dominated
        runtime before this cap.
        """
        t0 = time.monotonic()
        timeout = self.semantic_tokens_timeout_s if timeout is None else timeout
        retries = self.operation_retries if retries is None else retries
        self.open_document(file_path)
        abs_path = self._abs_path(file_path)
        uri = Path(abs_path).as_uri()
        result = None
        for attempt in range(retries + 1):
            result = self._request(
                "textDocument/semanticTokens/full",
                {
                    "textDocument": {"uri": uri},
                },
                timeout=timeout,
            )
            if result is not None:
                break
            if self._last_error and self._last_error.get("code") == -2:
                break
            if not self._is_retryable_error() or attempt >= retries:
                break
            time.sleep(0.5)
        n_tokens = 0
        if result is not None:
            n_tokens = len(result.get("data", [])) // 5
            logger.debug(f"semanticTokens for {file_path}: {n_tokens} tokens")
        _profile_log(
            "semanticTokens/full",
            file_path,
            None,
            None,
            time.monotonic() - t0,
            n_tokens,
        )
        if result is None:
            self._raise_if_strict_failure("textDocument/semanticTokens/full", file_path)
        return result

    def semantic_tokens_range(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
    ) -> Optional[dict]:
        """Get semantic tokens for a specific line range.

        Faster than full for large files when only a small range is needed.
        Returns None if not supported or on error.
        """
        t0 = time.monotonic()
        timeout = self.semantic_tokens_timeout_s if timeout is None else timeout
        retries = self.operation_retries if retries is None else retries
        self.open_document(file_path)
        abs_path = self._abs_path(file_path)
        uri = Path(abs_path).as_uri()
        result = None
        for attempt in range(retries + 1):
            result = self._request(
                "textDocument/semanticTokens/range",
                {
                    "textDocument": {"uri": uri},
                    "range": {
                        "start": {"line": start_line, "character": 0},
                        "end": {"line": end_line + 1, "character": 0},
                    },
                },
                timeout=timeout,
            )
            if result is not None:
                break
            if self._last_error and self._last_error.get("code") == -2:
                break
            if not self._is_retryable_error() or attempt >= retries:
                break
            time.sleep(0.5)
        n_tokens = 0
        if result is not None:
            n_tokens = len(result.get("data", [])) // 5
            logger.debug(
                f"semanticTokens/range for {file_path} "
                f"L{start_line}-{end_line}: {n_tokens} tokens"
            )
        _profile_log(
            "semanticTokens/range",
            file_path,
            start_line,
            end_line,
            time.monotonic() - t0,
            n_tokens,
        )
        if result is None:
            self._raise_if_strict_failure(
                "textDocument/semanticTokens/range", file_path
            )
        return result

    def decode_semantic_tokens(
        self, tokens_response: dict, file_path: str
    ) -> list[dict]:
        """Decode semantic tokens response into a list of token dicts.

        Each token dict has: line, character, length, token_type, modifiers, text.
        """
        if not tokens_response or "data" not in tokens_response:
            return []
        if not self.semantic_tokens_legend:
            logger.warning("No semantic tokens legend available")
            return []

        data = tokens_response["data"]
        token_types = self.semantic_tokens_legend.get("tokenTypes", [])
        token_modifiers = self.semantic_tokens_legend.get("tokenModifiers", [])

        # Read file content for extracting token text
        abs_path = self._abs_path(file_path)
        try:
            lines = Path(abs_path).read_text(errors="replace").splitlines()
        except FileNotFoundError:
            lines = []

        tokens = []
        current_line = 0
        current_char = 0

        for i in range(0, len(data), 5):
            if i + 4 >= len(data):
                break
            delta_line = data[i]
            delta_char = data[i + 1]
            length = data[i + 2]
            type_idx = data[i + 3]
            mod_bits = data[i + 4]

            if delta_line > 0:
                current_line += delta_line
                current_char = delta_char
            else:
                current_char += delta_char

            # Decode type
            type_name = (
                token_types[type_idx]
                if type_idx < len(token_types)
                else f"type_{type_idx}"
            )

            # Decode modifiers (bitmask)
            mods = []
            for bit_idx, mod_name in enumerate(token_modifiers):
                if mod_bits & (1 << bit_idx):
                    mods.append(mod_name)

            # Extract text from source
            text = ""
            if current_line < len(lines):
                line_text = lines[current_line]
                text = line_text[current_char : current_char + length]

            tokens.append(
                {
                    "line": current_line,
                    "character": current_char,
                    "length": length,
                    "token_type": type_name,
                    "modifiers": mods,
                    "text": text,
                }
            )

        return tokens

    # ── Internal ──────────────────────────────────────────────

    def _abs_path(self, file_path: str) -> str:
        """Convert relative file path to absolute."""
        p = Path(file_path)
        if p.is_absolute():
            return str(p)
        return str(self.project_root / file_path)

    def _detect_language_id(self, file_path: str) -> str:
        ext = Path(file_path).suffix
        return _EXT_TO_LANG_ID.get(ext, "text")

    def _wait_for_ready(self):
        """Wait for the LSP server to become ready.

        Send ``documentSymbol`` on a known file to verify the initialized
        transport can serve document requests. Cross-file semantic readiness
        is a separate concern handled by workspace warmup and analysis barriers;
        an empty ``references`` result is valid and must not delay startup.
        """
        # Find a file to probe — use extensions matching the configured language
        _LANG_PROBE_EXTS = {
            "python": [".py"],
            "rust": [".rs"],
            "typescript": [".ts", ".tsx", ".js"],
            "ts": [".ts", ".tsx", ".js"],
            "go": [".go"],
            "cpp": [".cpp", ".cc", ".c", ".h"],
            "c": [".c", ".h"],
            "c++": [".cpp", ".cc", ".h"],
            "java": [".java"],
            "csharp": [".cs"],
            "ruby": [".rb"],
            "php": [".php", ".phtml"],
            "kotlin": [".kt", ".kts"],
        }
        probe_exts = _LANG_PROBE_EXTS.get(self.language, [])
        candidates = []
        for ext in probe_exts:
            candidates.extend(self.project_root.glob(f"**/*{ext}"))

        if candidates:
            for probe_file in sorted(set(candidates)):
                logger.debug(f"Probing server readiness with {probe_file}")
                self.open_document(str(probe_file))
                result = self._request(
                    "textDocument/documentSymbol",
                    {
                        "textDocument": {"uri": probe_file.as_uri()},
                    },
                    timeout=60,
                )
                if result is not None:
                    return
                if is_lsp_document_unavailable_error(self.last_error):
                    continue
                if self.last_error and self.last_error.get("code") == -2:
                    continue
                raise RuntimeError(
                    f"LSP readiness probe failed for {probe_file}: {self.last_error}"
                )
            raise RuntimeError("LSP readiness probe found no queryable source file")
        else:
            # No source files found, just wait briefly
            time.sleep(2)

    def _send(self, message: dict):
        """Send a JSON-RPC message to the server."""
        content = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(content)}\r\n\r\n".encode("utf-8")
        try:
            self.process.stdin.write(header + content)
            self.process.stdin.flush()
        except OSError as e:
            logger.error(f"LSP server pipe broken: {e}")
            raise

    def _read_message(self, timeout: float = 30) -> Optional[dict]:
        """Read one JSON-RPC message from the server's stdout."""
        deadline = time.monotonic() + timeout
        buf = self._read_buffer
        self._read_buffer = b""

        while time.monotonic() < deadline:
            # Consume complete buffered frames before waiting for more bytes.
            while True:
                # Find header/body boundary
                header_end = buf.find(b"\r\n\r\n")
                if header_end == -1:
                    break

                # Parse Content-Length
                header_text = buf[:header_end].decode("utf-8", errors="replace")
                content_length = None
                for line in header_text.split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        content_length = int(line.split(":", 1)[1].strip())
                        break

                if content_length is None:
                    # Malformed header, skip
                    buf = buf[header_end + 4 :]
                    continue

                body_start = header_end + 4
                body_end = body_start + content_length
                if len(buf) < body_end:
                    # Need more data
                    break

                body = buf[body_start:body_end]
                buf = buf[body_end:]

                try:
                    message = json.loads(body.decode("utf-8"))
                except json.JSONDecodeError:
                    continue

                # Auto-respond to server-initiated requests
                if "id" in message and "method" in message:
                    server_method = message["method"]
                    if server_method == "window/workDoneProgress/create":
                        # Acknowledge progress token creation
                        self._send(
                            {
                                "jsonrpc": "2.0",
                                "id": message["id"],
                                "result": None,
                            }
                        )
                        continue
                    elif server_method == "client/registerCapability":
                        # Acknowledge dynamic capability registration
                        self._send(
                            {
                                "jsonrpc": "2.0",
                                "id": message["id"],
                                "result": None,
                            }
                        )
                        continue

                # Server notifications we don't return upstream.
                if "method" in message and "id" not in message:
                    if message["method"] == "$/progress":
                        # Track LSP background work (rust-analyzer indexing,
                        # gopls workspace setup, basedpyright analysis...)
                        # so callers can wait_until_idle() before timed
                        # regions instead of guessing with sleeps.
                        self._handle_progress(message.get("params", {}))
                        continue
                    skip_methods = {
                        "window/logMessage",
                        "window/showMessage",
                        "textDocument/publishDiagnostics",
                    }
                    if message["method"] in skip_methods:
                        continue

                self._read_buffer = buf
                return message

            remaining = max(0.1, deadline - time.monotonic())
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                continue

            chunk = (
                self.process.stdout.read1(65536)
                if hasattr(self.process.stdout, "read1")
                else self.process.stdout.read(1)
            )
            if not chunk:
                self._read_buffer = buf
                return None
            buf += chunk

        self._read_buffer = buf
        return None

    # ── progress / idleness tracking ─────────────────────────────────
    # Servers signal background work via ``$/progress`` notifications:
    #   begin → report* → end
    # We track active tokens; ``wait_until_idle`` polls for empty.

    def _handle_progress(self, params: dict) -> None:
        token = params.get("token")
        if token is None:
            return
        kind = (params.get("value") or {}).get("kind")
        if kind == "begin":
            self._active_progress[token] = time.monotonic()
        elif kind == "end":
            self._active_progress.pop(token, None)
        # report: keep the token active

    def drain_notifications(self, timeout_s: float = 0.5) -> int:
        """Drain any pending notifications without waiting for a response.

        Returns the number of messages drained. Used by ``wait_until_idle``
        to keep the progress map fresh while polling.
        """
        n = 0
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            message = self._read_message(timeout=min(remaining, 0.05))
            if message is None:
                break
            n += 1
            response_id = message.get("id")
            if response_id is not None:
                self._pending_ids.discard(response_id)
                self._response_buffer[response_id] = message
        return n

    def wait_until_idle(
        self, max_wait_s: float = 300.0, idle_grace_s: float = 1.0
    ) -> bool:
        """Wait until no ``$/progress`` tokens are active for ``idle_grace_s``.

        Returns True if the server became idle within ``max_wait_s``,
        False on timeout. Useful before timed regions so background
        indexing (rust-analyzer cache priming, gopls workspace load,
        basedpyright type analysis) doesn't bleed into measurements.
        """
        deadline = time.monotonic() + max_wait_s
        idle_since: Optional[float] = None
        while time.monotonic() < deadline:
            self.drain_notifications(timeout_s=0.2)
            if not self._active_progress:
                if idle_since is None:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since >= idle_grace_s:
                    return True
            else:
                idle_since = None
            time.sleep(0.1)
        return False

    def _request(self, method: str, params: Any, timeout: float = 30) -> Any:
        """Send a request and wait for the matching response.

        Enforces a maximum number of in-flight (pending) requests to prevent
        overwhelming the LSP server.  When the limit is reached, drains
        pending responses before sending a new request.

        Out-of-order responses are buffered (not discarded) so that
        concurrent request patterns do not lose data.
        """
        # Drain pending responses if we're at the limit
        self._drain_pending()

        msg_id = self._next_id
        self._next_id += 1
        self._pending_ids.add(msg_id)
        self._send(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": method,
                "params": params,
            }
        )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Check buffer first — another _request may have read our response
            if msg_id in self._response_buffer:
                message = self._response_buffer.pop(msg_id)
                self._pending_ids.discard(msg_id)
                if "error" in message:
                    self._last_error = message["error"]
                    logger.warning(f"LSP error for {method}: {message['error']}")
                    return None
                result = message.get("result")
                if result is None:
                    self._last_error = {"code": -2, "message": "null result"}
                else:
                    self._last_error = None
                return result

            remaining = max(0.1, deadline - time.monotonic())
            message = self._read_message(timeout=remaining)
            if message is None:
                self._pending_ids.discard(msg_id)
                self._notify("$/cancelRequest", {"id": msg_id})
                self._last_error = {"code": -1, "message": "timeout"}
                logger.warning(f"LSP request {method} timed out (id={msg_id})")
                return None
            resp_id = message.get("id")
            if resp_id is not None:
                self._pending_ids.discard(resp_id)
            if resp_id == msg_id:
                if "error" in message:
                    self._last_error = message["error"]
                    logger.warning(f"LSP error for {method}: {message['error']}")
                    return None
                result = message.get("result")
                if result is None:
                    # Server returned null — may still be analyzing
                    self._last_error = {"code": -2, "message": "null result"}
                else:
                    self._last_error = None
                return result
            # Not our response — buffer it for later retrieval
            if resp_id is not None:
                self._response_buffer[resp_id] = message
                # Evict oldest entries if buffer grows too large
                _MAX_RESPONSE_BUFFER = 100
                if len(self._response_buffer) > _MAX_RESPONSE_BUFFER:
                    oldest = min(self._response_buffer.keys())
                    del self._response_buffer[oldest]

        self._last_error = {"code": -1, "message": "timeout"}
        self._pending_ids.discard(msg_id)
        self._notify("$/cancelRequest", {"id": msg_id})
        logger.warning(f"LSP request {method} timed out after {timeout}s")
        return None

    def _request_batch(
        self,
        method: str,
        params_batch: list[Any],
        *,
        timeout: float = 30,
        max_in_flight: Optional[int] = None,
    ) -> list[tuple[Any, Optional[dict], float]]:
        """Issue a bounded group of requests with one stdout reader.

        This is transport-level pipelining, not a JSON-RPC batch payload: LSP
        servers receive ordinary requests and can schedule them concurrently.
        """

        if not params_batch:
            return []
        window = self._max_pending if max_in_flight is None else max_in_flight
        if window < 1:
            raise ValueError("max_in_flight must be positive")

        results: list[Optional[tuple[Any, Optional[dict], float]]] = [None] * len(
            params_batch
        )
        pending: dict[int, tuple[int, float, float]] = {}
        next_index = 0

        def fill_window() -> None:
            nonlocal next_index
            while next_index < len(params_batch) and len(pending) < window:
                msg_id = self._next_id
                self._next_id += 1
                sent_at = time.monotonic()
                pending[msg_id] = (next_index, sent_at, sent_at + timeout)
                self._pending_ids.add(msg_id)
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "method": method,
                        "params": params_batch[next_index],
                    }
                )
                next_index += 1

        fill_window()
        while pending:
            now = time.monotonic()
            expired = [
                msg_id
                for msg_id, (_index, _sent_at, deadline) in pending.items()
                if deadline <= now
            ]
            for msg_id in expired:
                index, sent_at, _deadline = pending.pop(msg_id)
                self._pending_ids.discard(msg_id)
                results[index] = (
                    None,
                    {"code": -1, "message": "timeout"},
                    now - sent_at,
                )
                self._notify("$/cancelRequest", {"id": msg_id})
            fill_window()
            if not pending:
                break

            next_deadline = min(deadline for _i, _sent, deadline in pending.values())
            message = self._read_message(timeout=max(0.01, next_deadline - now))
            if message is None:
                continue
            response_id = message.get("id")
            if response_id not in pending:
                if response_id is not None:
                    self._pending_ids.discard(response_id)
                    self._response_buffer[response_id] = message
                continue

            index, sent_at, _deadline = pending.pop(response_id)
            self._pending_ids.discard(response_id)
            error = message.get("error")
            result = message.get("result")
            if error is None and result is None:
                error = {"code": -2, "message": "null result"}
            results[index] = (result, error, time.monotonic() - sent_at)
            fill_window()

        fallback = (None, {"code": -1, "message": "missing response"}, 0.0)
        return [result or fallback for result in results]

    # LSP error codes that are transient (server busy/updating, worth retrying).
    # Based on LSP 3.17 spec and Neovim's handling (PR #14622, #30999):
    #   -32801 ContentModified: server VFS changed, will resolve after apply
    #   -32802 ServerCancelled: server cancelled (spec says retrigger)
    # All other codes are permanent (bad request, not supported, etc.).
    _RETRYABLE_ERROR_CODES = frozenset(
        {
            -1,  # Timeout (internal)
            -2,  # Null result (server may still be analyzing)
            -32801,  # ContentModified
            -32802,  # ServerCancelled
        }
    )

    def _is_retryable_error(self) -> bool:
        """Check if the last _request error is transient (worth retrying).

        Transient: timeout (code=-1), ContentModified (-32801),
                   ServerCancelled (-32802), null result (code=-2,
                   server may still be analyzing).
        Permanent: everything else.
        """
        if self._last_error is None:
            return False  # no error info at all
        code = self._last_error.get("code", 0)
        return code in self._RETRYABLE_ERROR_CODES

    def _drain_pending(self):
        """If too many requests are pending, block until responses arrive."""
        max_iterations = 100
        iterations = 0
        while len(self._pending_ids) >= self._max_pending:
            iterations += 1
            if iterations > max_iterations:
                logger.warning(
                    f"_drain_pending exceeded {max_iterations} iterations, "
                    f"clearing {len(self._pending_ids)} pending requests"
                )
                self._pending_ids.clear()
                break

            if self.process and self.process.poll() is not None:
                logger.warning("LSP server process died, clearing pending requests")
                self._pending_ids.clear()
                break

            message = self._read_message(timeout=30.0)
            if message is None:
                # No response — server may be busy processing.
                # Do NOT discard pending IDs or send new requests.
                # Just keep waiting.
                continue
            resp_id = message.get("id")
            if resp_id is not None:
                self._pending_ids.discard(resp_id)

    def _notify(self, method: str, params: Any):
        """Send a notification (no response expected)."""
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    # ── Class methods ─────────────────────────────────────────

    @staticmethod
    def get_lsp_command(language: str) -> Optional[list[str]]:
        """Get the default LSP server command for a language."""
        cmd = lsp_command_for_language(language)
        if not cmd:
            return None
        binary = cmd[0]
        resolved = resolve_lsp_binary(binary)
        if resolved:
            return [resolved] + cmd[1:]
        return cmd

    @staticmethod
    def check_lsp_available(language: str) -> bool:
        """Check if the LSP server binary is available."""
        cmd = LSPClient.get_lsp_command(language)
        if not cmd:
            return False
        if normalize_graph_language(language) == "rust":
            try:
                subprocess.run(
                    cmd + ["--version"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return True
            except (
                subprocess.CalledProcessError,
                FileNotFoundError,
                subprocess.TimeoutExpired,
            ):
                return False
        return Path(cmd[0]).exists() or resolve_lsp_binary(cmd[0]) is not None


def uri_to_relpath(uri: str, project_root: str) -> Optional[str]:
    """Convert a file:// URI to a path relative to the project root."""
    if not uri.startswith("file://"):
        return None
    abs_path = uri[7:]  # strip file://
    try:
        return str(Path(abs_path).relative_to(project_root))
    except ValueError:
        return abs_path
