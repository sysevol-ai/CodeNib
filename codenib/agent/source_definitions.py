# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Source-bound Python definition candidates with explicit graph fallback.

The context owns both this worker and its source binding. A worker generation
receives every captured file, never the host checkout, and never serves
references. A standard LSP reply qualifies only when it selects one exact
function/class declaration token in captured source. This is navigation, not
proof of a unique runtime call target.

Calls are serialized by the context query lock. Original-source checks bracket
the immutable generation's requests; publication linearizes at the enclosing
context's final source check. Export alone holds a batched source read lease,
so a worker's startup/analysis cannot block status verification on that lease.
Closing retains the process, pipes and temporary payload until exit, pipe close
and payload cleanup succeed. The context closes this owner before compiling a
graph or intentionally releasing usable source. Authentication failure revokes
the source immediately; the enclosing indexed query then reaps the immutable
worker before returning failure, or retains an explicit cleanup-pending owner.
No new lock, detached job, automatic recapture or provider restart is introduced.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import os
import select
import subprocess
import tempfile
import time
import tokenize
import unicodedata
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

from ..paths import toolchain_dir
from ..source_fingerprint import RepositorySourceBinding
from ..toolchains import BASEDPYRIGHT_VERSION, resolve_command
from ..types import NODE_TYPE_CLASS, NODE_TYPE_FUNCTION, QueriedNode
from .lsp_provider import (
    CAPABILITY_DEFINITION,
    JSON_RPC_LSP_PROVIDER,
    LSPProviderMetadata,
    LSPProviderNodes,
)

_BUNDLE_SHA256 = "703d47989c5132c29a507f1789a2b583b986e9689f4632a1ef1cbcd4d54fb1bd"
_PREFIX = b"CODENIB_SOURCE_DEFINITION_V1 "
_MAX_SOURCE_BYTES = 128 * 1024**2
_MAX_PAYLOAD_BYTES = 256 * 1024**2
_MAX_REPLY_BYTES = 4 * 1024**2
_MAX_DECLARATION_BYTES = 2 * 1024**2
_START_SECONDS = 120.0
_QUERY_SECONDS = 25.0
_CLOSE_SECONDS = 2.0


class SourceDefinitionUnavailable(RuntimeError):
    """A source candidate cannot be delivered; use the existing graph route."""


def managed_definition_runtime() -> tuple[str, Path] | None:
    """Resolve locally installed pinned software without downloading anything."""

    if os.name == "nt" or os.environ.get("CODENIB_PYTHON_LSP_CMD"):
        # Pipe readiness below is POSIX-specific. Windows keeps the graph route.
        # An explicitly selected Python server also keeps its existing route.
        return None
    node = resolve_command("node")
    package = toolchain_dir() / "node-tools/node_modules/browser-basedpyright"
    bundle = package / "dist/pyright.worker.js"
    if node is None:
        return None
    try:
        metadata = (package / "package.json").read_bytes()
        if len(metadata) > 64 * 1024:
            return None
        if json.loads(metadata).get("version") != BASEDPYRIGHT_VERSION:
            return None
        if hashlib.sha256(bundle.read_bytes()).hexdigest() != _BUNDLE_SHA256:
            return None
    except (OSError, ValueError, AttributeError):
        return None
    return node, bundle


class SourceDefinitionProvider:
    """One lazy immutable virtual-source generation, owned by ServerContext."""

    def __init__(
        self,
        source: RepositorySourceBinding,
        fingerprint: str,
        runtime: tuple[str, Path],
    ) -> None:
        self.source = source
        self.fingerprint = fingerprint
        self.runtime = runtime
        self.process: subprocess.Popen | None = None
        self._temporary: tempfile.TemporaryDirectory | None = None
        self._stderr = None
        self._pending = b""
        self._next_id = 1
        self._sources: dict[str, str] = {}
        self._declarations: dict[
            str, dict[tuple[int, int, int, int], tuple[str, str]]
        ] = {}
        self.ready: dict | None = None
        self.status = "available"

    def selection(self) -> dict:
        return {
            "provider": JSON_RPC_LSP_PROVIDER,
            "backend": "captured-python-definition-v1",
            "status": self.status,
            "index_snapshot": self.fingerprint,
            "capabilities": {
                "definition": self.status in ("available", "ok"),
                "references": False,
                "route": False,
            },
        }

    def can_serve(self, capability: str) -> LSPProviderMetadata:
        supported = capability in (CAPABILITY_DEFINITION, "textDocument/definition")
        return LSPProviderMetadata(
            provider=JSON_RPC_LSP_PROVIDER,
            capability=CAPABILITY_DEFINITION if supported else capability,
            status=(
                "ok"
                if supported and self.status in ("available", "ok")
                else "unsupported"
            ),
            lsp_method="textDocument/definition" if supported else capability,
            backend="captured-python-definition-v1",
            index_snapshot=self.fingerprint,
            behavior_contract="captured_python_definition_candidate_v1",
            position_granularity="character",
            position_encoding="UTF8",
        )

    def _start(self) -> None:
        if self.process is not None:
            raise SourceDefinitionUnavailable("source_worker_already_owned")
        if self.status == "closed":
            raise SourceDefinitionUnavailable("source_worker_closed")
        with self.source.read_session():
            identity = self.source.authenticated_identity_snapshot()
            if identity.fingerprint != self.fingerprint:
                raise SourceDefinitionUnavailable("source_generation_mismatch")
            if sum(record.size for record in identity.file_records) > _MAX_SOURCE_BYTES:
                raise SourceDefinitionUnavailable("source_generation_too_large")
            files = {}
            for record in identity.file_records:
                if record.link_target is not None:
                    raise SourceDefinitionUnavailable("selected_links_need_graph")
                raw = self.source.read_bytes(record.path, max_bytes=max(1, record.size))
                if hashlib.sha256(raw).hexdigest() != record.sha256:
                    raise SourceDefinitionUnavailable("source_export_mismatch")
                item = {"bytes": len(raw), "sha256": record.sha256}
                try:
                    item["text"] = raw.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    if record.path.endswith((".py", ".pyi")):
                        raise SourceDefinitionUnavailable(
                            "non_utf8_python_needs_graph"
                        ) from exc
                    item["base64"] = base64.b64encode(raw).decode()
                if record.path.endswith((".py", ".pyi")):
                    self._sources[record.path] = item["text"]
                files["/workspace/" + record.path] = item
            payload = json.dumps(
                {"source_fingerprint": self.fingerprint, "files": files},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            if len(payload) > _MAX_PAYLOAD_BYTES:
                raise SourceDefinitionUnavailable("source_payload_too_large")
        self._temporary = tempfile.TemporaryDirectory(prefix="codenib-definitions-")
        payload_path = Path(self._temporary.name) / "source.json"
        payload_path.write_bytes(payload)
        self._stderr = tempfile.TemporaryFile(mode="w+b")
        node, bundle = self.runtime
        self.process = subprocess.Popen(
            [
                node,
                "--max-old-space-size=4096",
                str(Path(__file__).with_name("_python_source_worker.cjs")),
                str(payload_path),
                str(bundle),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            bufsize=0,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC"},
        )
        os.set_blocking(self.process.stdin.fileno(), False)
        ready = self._receive(time.monotonic() + _START_SECONDS)
        if (
            ready.get("id") != 0
            or ready.get("ready") is not True
            or ready.get("pid") != self.process.pid
            or ready.get("source_fingerprint") != self.fingerprint
            or len(ready.get("preparation", ())) != len(self._sources)
            or len(ready.get("before", ())) != 2
            or not all(
                item.get("all_bytes_match") is True and item.get("files") == len(files)
                for item in ready["before"]
            )
        ):
            raise SourceDefinitionUnavailable("source_worker_not_ready")
        self.ready = ready
        self.status = "ok"

    def _receive(self, deadline: float) -> dict:
        while True:
            if len(self._pending) > _MAX_REPLY_BYTES:
                raise SourceDefinitionUnavailable("source_worker_reply_too_large")
            if b"\n" in self._pending:
                line, self._pending = self._pending.split(b"\n", 1)
                if not line.startswith(_PREFIX):
                    raise SourceDefinitionUnavailable("source_worker_invalid_frame")
                try:
                    result = json.loads(line[len(_PREFIX) :])
                except ValueError as exc:
                    raise SourceDefinitionUnavailable(
                        "source_worker_invalid_json"
                    ) from exc
                if not isinstance(result, dict):
                    raise SourceDefinitionUnavailable("source_worker_invalid_reply")
                return result
            remaining = deadline - time.monotonic()
            if (
                remaining <= 0
                or not select.select([self.process.stdout], [], [], max(0, remaining))[
                    0
                ]
            ):
                raise SourceDefinitionUnavailable("source_worker_deadline")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise SourceDefinitionUnavailable("source_worker_closed_stdout")
            self._pending += chunk

    def _exchange(
        self, action: str, *, timeout: float = _QUERY_SECONDS, **params
    ) -> dict:
        request = {"id": self._next_id, "action": action, **params}
        self._next_id += 1
        data = (json.dumps(request) + "\n").encode()
        deadline = time.monotonic() + timeout
        while data:
            remaining = deadline - time.monotonic()
            if (
                remaining <= 0
                or not select.select([], [self.process.stdin], [], max(0, remaining))[1]
            ):
                raise SourceDefinitionUnavailable("source_worker_write_deadline")
            try:
                written = os.write(self.process.stdin.fileno(), data)
            except BlockingIOError:
                continue
            if written <= 0:
                raise SourceDefinitionUnavailable("source_worker_closed_stdin")
            data = data[written:]
        result = self._receive(deadline)
        if result.get("id") != request["id"]:
            raise SourceDefinitionUnavailable("source_worker_reply_id_mismatch")
        return result

    @staticmethod
    def _column(text: str, column: int, source_codec: str, target_codec: str) -> int:
        width = 2 if source_codec == "utf-16-le" else 1
        raw = text.encode(source_codec)
        if not 0 <= column * width <= len(raw):
            raise ValueError("character exceeds source line")
        prefix = raw[: column * width].decode(source_codec, errors="strict")
        return len(prefix.encode(target_codec)) // (
            2 if target_codec == "utf-16-le" else 1
        )

    def _target(self, location: dict) -> QueriedNode:
        parsed = urlparse(location.get("targetUri") or location.get("uri") or "")
        path = unquote(parsed.path)
        if (
            parsed.scheme != "file"
            or parsed.netloc
            or not path.startswith("/workspace/")
        ):
            raise SourceDefinitionUnavailable("definition_outside_captured_source")
        file = path[len("/workspace/") :]
        if file not in self._sources:
            raise SourceDefinitionUnavailable("definition_without_captured_python")
        if len(self._sources[file].encode()) > _MAX_DECLARATION_BYTES:
            raise SourceDefinitionUnavailable("definition_source_file_too_large")
        extent = location.get("targetSelectionRange") or location.get("range")
        start, end = extent["start"], extent["end"]
        lines = self._sources[file].split("\n")
        begin = (
            start["line"],
            self._column(
                lines[start["line"]], start["character"], "utf-16-le", "utf-8"
            ),
        )
        finish = (
            end["line"],
            self._column(lines[end["line"]], end["character"], "utf-16-le", "utf-8"),
        )
        if file not in self._declarations:
            names = {}
            tokens = list(
                tokenize.generate_tokens(io.StringIO(self._sources[file]).readline)
            )
            keyword_names = {}
            for index, token in enumerate(tokens[:-1]):
                if token.type == tokenize.NAME and token.string in ("def", "class"):
                    name = tokens[index + 1]
                    if name.type == tokenize.NAME:
                        keyword_names[(token.start[0], token.string)] = name
            tree = ast.parse(self._sources[file], filename=file)
            for node in ast.walk(tree):
                if not isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    continue
                keyword = "class" if isinstance(node, ast.ClassDef) else "def"
                kind = NODE_TYPE_CLASS if keyword == "class" else NODE_TYPE_FUNCTION
                name = keyword_names.get((node.lineno, keyword))
                if (
                    name is not None
                    and unicodedata.normalize("NFKC", name.string) == node.name
                ):
                    line = name.start[0] - 1
                    left = len(lines[line][: name.start[1]].encode())
                    right = len(lines[line][: name.end[1]].encode())
                    names[(line, left, line, right)] = (node.name, kind)
            self._declarations[file] = names
        declaration = self._declarations[file].get((*begin, *finish))
        if declaration is None:
            raise SourceDefinitionUnavailable("definition_is_not_exact_declaration")
        name, kind = declaration
        return QueriedNode(
            node_name=f"{file}:{name}",
            node_id=f"{file}:{begin[0]}:{begin[1]}:{kind}",
            file=file,
            type=kind,
            start_line=begin[0],
            end_line=finish[0],
            score=1.0,
            content="definition candidate",
        )

    def definition(
        self, *, file_path=None, line=None, character=None, symbol=None, top_k=8
    ):
        if self.status not in ("available", "ok"):
            raise SourceDefinitionUnavailable("source_worker_needs_cleanup")
        if symbol or file_path is None or line is None or character is None:
            raise SourceDefinitionUnavailable("definition_requires_exact_position")
        relative = PurePosixPath(file_path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or str(relative) != file_path
        ):
            raise SourceDefinitionUnavailable("definition_requires_selected_path")
        if not file_path.endswith((".py", ".pyi")):
            raise SourceDefinitionUnavailable("definition_requires_python_source")
        if (
            type(line) is not int
            or line < 0
            or type(character) is not int
            or character < 0
        ):
            raise ValueError("line and character must be non-negative integers")
        self.source.verify_snapshot()
        try:
            if self.ready is None:
                self._start()
            if file_path not in self._sources:
                raise SourceDefinitionUnavailable("definition_requires_selected_path")
            text = self._sources[file_path].split("\n")[line]
            column16 = self._column(text, character, "utf-8", "utf-16-le")
            observed = self._exchange(
                "definition", file=file_path, line=line, character=column16
            )
            if observed.get("source_fingerprint") != self.fingerprint:
                raise SourceDefinitionUnavailable("definition_generation_mismatch")
            response = observed["response"]
            if "error" in response:
                raise SourceDefinitionUnavailable("definition_provider_error")
            locations = response.get("result") or []
            if isinstance(locations, dict):
                locations = [locations]
            if len(locations) != 1:
                raise SourceDefinitionUnavailable("definition_not_one_candidate")
            try:
                node = self._target(locations[0])
            except (TypeError, ValueError) as exc:
                raise SourceDefinitionUnavailable(
                    "definition_target_range_unsupported"
                ) from exc
            result = LSPProviderNodes(
                [node][:top_k], metadata=self.can_serve("definition")
            )
        except (
            OSError,
            EOFError,
            IndexError,
            KeyError,
            SyntaxError,
            tokenize.TokenError,
        ) as exc:
            raise SourceDefinitionUnavailable(
                "definition_worker_or_source_unsupported"
            ) from exc
        finally:
            self.source.verify_snapshot()
        return result

    def references(self, **_arguments):
        raise SourceDefinitionUnavailable("source_references_not_promoted")

    def route(self, **_arguments):
        raise SourceDefinitionUnavailable("source_routes_require_graph")

    def close(self) -> None:
        self.status = "closing"
        process = self.process
        if process is not None:
            if process.poll() is None:
                try:
                    self._exchange("stop", timeout=_CLOSE_SECONDS)
                    process.wait(timeout=_CLOSE_SECONDS)
                except (
                    OSError,
                    ValueError,
                    subprocess.TimeoutExpired,
                    SourceDefinitionUnavailable,
                ):
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=_CLOSE_SECONDS)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=_CLOSE_SECONDS)
            if process.poll() is None:
                raise RuntimeError("source definition worker still needs cleanup")
            for stream in (process.stdin, process.stdout):
                stream.close()
        if self._stderr is not None:
            self._stderr.close()
        if self._temporary is not None:
            self._temporary.cleanup()
        # Release only after observed exit, closed pipes and payload cleanup.
        self.process = None
        self._temporary = None
        self._stderr = None
        self._pending = b""
        self.ready = None
        self._sources.clear()
        self._declarations.clear()
        self.status = "closed"
