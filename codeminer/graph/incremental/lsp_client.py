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
from ...scip_interface.rust_analyzer import rust_toolchain

logger = get_logger(__name__)


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
    # Bypass a repository-pinned Rust toolchain so rust-analyzer uses the
    # CodeMiner-selected toolchain instead.
    if language == "rust":
        env["RUSTUP_TOOLCHAIN"] = rust_toolchain()

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
    ):
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
        self._max_pending: int = 10

        # Buffer for out-of-order responses: when _request reads a response
        # for a different request ID, it stores it here instead of discarding.
        self._response_buffer: dict[int, dict] = {}

        # Track the last error from _request so callers can decide whether
        # to retry (transient errors like -32801) or not (permanent errors).
        self._last_error: Optional[dict] = None

    # ── Lifecycle ─────────────────────────────────────────────

    def start(self, skip_probe: bool = False) -> dict:
        """Start the LSP server and send initialize/initialized.

        Args:
            skip_probe: If True, skip the blocking readiness probe.
                Use this when starting early for warm-up — the server
                will analyze the project in the background.
        """
        logger.info(f"Starting LSP server: {' '.join(self.command)}")
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
    ):
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
            # If hover responds in < 1s, server is likely done analyzing
            if elapsed < 1.0:
                return
            time.sleep(poll_interval)

    # ── LSP queries ───────────────────────────────────────────

    def document_symbol(
        self, file_path: str, retries: int = 0, retry_delay: float = 0.5
    ) -> list[dict]:
        """Get hierarchical document symbols for a file.

        Default ``retries=0`` — null is a legitimate "no symbols" answer
        per LSP spec. Previous default of 5 retries × 5s sleep wasted up
        to 25s/call when servers returned null. If the server is still
        loading at warmup time, callers should wait at the call-site,
        not inside this primitive.
        """
        t0 = time.monotonic()
        self.open_document(file_path)
        abs_path = self._abs_path(file_path)
        uri = Path(abs_path).as_uri()

        for attempt in range(retries + 1):
            result = self._request(
                "textDocument/documentSymbol",
                {
                    "textDocument": {"uri": uri},
                },
                timeout=10,  # longest non-empty observed: 138ms
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
            if attempt < retries:
                logger.debug(
                    f"documentSymbol returned null for {file_path}, "
                    f"retrying in {retry_delay}s ({attempt + 1}/{retries})"
                )
                time.sleep(retry_delay)

        _profile_log("documentSymbol", file_path, None, None, time.monotonic() - t0, 0)
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

    def _references_inner(
        self,
        file_path: str,
        line: int,
        character: int,
        include_declaration: bool = False,
        timeout: float = 10,
        retries: int = 0,
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
            # Record this attempt's id for buffer check on next retry
            prior_ids.append(self._next_id - 1)
            if not self._is_retryable_error():
                logger.warning(
                    f"references failed for {file_path}:{line} (permanent error)"
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
        timeout: float = 10,
        retries: int = 0,
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
            prior_ids.append(self._next_id - 1)
            if not self._is_retryable_error():
                logger.warning(
                    f"definition failed for {file_path}:{line}:{character} "
                    f"(permanent error)"
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
        return []

    def semantic_tokens_full(
        self, file_path: str, timeout: float = 15
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
        self.open_document(file_path)
        abs_path = self._abs_path(file_path)
        uri = Path(abs_path).as_uri()
        result = self._request(
            "textDocument/semanticTokens/full",
            {
                "textDocument": {"uri": uri},
            },
            timeout=timeout,
        )
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
        return result

    def semantic_tokens_range(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
        timeout: float = 30,
    ) -> Optional[dict]:
        """Get semantic tokens for a specific line range.

        Faster than full for large files when only a small range is needed.
        Returns None if not supported or on error.
        """
        t0 = time.monotonic()
        self.open_document(file_path)
        abs_path = self._abs_path(file_path)
        uri = Path(abs_path).as_uri()
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

        Strategy: send a probe documentSymbol on a known file. If it returns,
        the server is ready. Falls back to a short sleep if no file is available.
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
        probe_file = None
        probe_exts = _LANG_PROBE_EXTS.get(self.language, [])
        for ext in probe_exts:
            candidates = list(self.project_root.glob(f"**/*{ext}"))
            if candidates:
                probe_file = candidates[0]
                break

        if probe_file:
            logger.debug(f"Probing server readiness with {probe_file}")
            self.open_document(str(probe_file))
            # documentSymbol verifies basic server readiness (syntax analysis)
            self._request(
                "textDocument/documentSymbol",
                {
                    "textDocument": {"uri": probe_file.as_uri()},
                },
                timeout=60,
            )
            # Poll references to wait for cross-file semantic analysis.
            # Servers like rust-analyzer need extra time for project loading;
            # references returns [] until the project is fully indexed.

            symbols = (
                self._request(
                    "textDocument/documentSymbol",
                    {
                        "textDocument": {"uri": probe_file.as_uri()},
                    },
                    timeout=10,
                )
                or []
            )
            if symbols:
                # Use the first symbol's position to probe references
                first_sym = symbols[0]
                sel = first_sym.get("selectionRange", first_sym.get("range", {}))
                probe_pos = sel.get("start", {"line": 0, "character": 0})
                for _ in range(15):
                    result = self._request(
                        "textDocument/references",
                        {
                            "textDocument": {"uri": probe_file.as_uri()},
                            "position": probe_pos,
                            "context": {"includeDeclaration": True},
                        },
                        timeout=10,
                    )
                    if result:
                        break
                    time.sleep(1)
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
        buf = b""

        while time.monotonic() < deadline:
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
                return None
            buf += chunk

            # Try to parse a complete message from buffer
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

                return message

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
            ready, _, _ = select.select(
                [self.process.stdout], [], [], min(remaining, 0.05)
            )
            if not ready:
                break
            msg = self._read_message(timeout=remaining)
            if msg is None:
                break
            n += 1
            # We don't dispatch unmatched responses here; just count and
            # let _handle_progress (called inside _read_message) update state.
        return n

    def wait_until_idle(
        self, max_wait_s: float = 60.0, idle_grace_s: float = 1.0
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
        logger.warning(f"LSP request {method} timed out after {timeout}s")
        return None

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
