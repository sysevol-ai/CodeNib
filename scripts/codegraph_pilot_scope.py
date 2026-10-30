# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Scope native pilot tools and authenticate SDK output before mirroring it.

The pinned SDK hook envelope binds a call to its session/transcript. MCP spill
text alone grants no access: file bytes must equal a completed tools/call reply
captured by the private wire proxy. The registry is call-scoped, synchronous
inside SDK callbacks and never shared across sessions. Registration linearizes
after a verified container copy; failed copies cannot authorize host reads.
Copying establishes availability, not model delivery or source-read credit.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

NATIVE_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write", "Bash", "ToolSearch"]
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_SPILL = re.compile(
    r"\AError: result \(([\d,]+) characters\) exceeds maximum allowed tokens\. "
    r"Output has been saved to (/[^\n]+)\.\n"
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]+\Z")
_WIRE_FILE = re.compile(r"call-[0-9]{6}\.json\Z")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _identity(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _read_owned_file(path: Path, *, max_bytes: int) -> bytes:
    """Open each component without links, then read one bounded regular file."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("SDK output path must be absolute without traversal")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    parent = os.open(path.anchor, flags | os.O_DIRECTORY)
    descriptor = None
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, flags | os.O_DIRECTORY, dir_fd=parent)
            os.close(parent)
            parent = child
        before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(
            path.name, flags | getattr(os, "O_NONBLOCK", 0), dir_fd=parent
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or opened.st_size > max_bytes
            or _identity(before) != _identity(opened)
        ):
            raise ValueError("SDK output is not a bounded owned regular file")
        chunks = []
        remaining = opened.st_size
        while remaining:
            block = os.read(descriptor, min(remaining, 1024 * 1024))
            if not block:
                raise ValueError("SDK output was truncated while reading")
            chunks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise ValueError("SDK output grew while reading")
        after = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if _identity(os.fstat(descriptor)) != _identity(opened) or (
            _identity(after) != _identity(opened)
        ):
            raise ValueError("SDK output changed while reading")
        return b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


_MIRROR = r"""import hashlib,os,stat,sys
from pathlib import PurePosixPath
path=PurePosixPath(sys.argv[1]); expected=sys.argv[2]
assert path.is_absolute() and '..' not in path.parts
data=sys.stdin.buffer.read()
assert hashlib.sha256(data).hexdigest()==expected
flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
parent=os.open('/',flags); output=None; created=False
try:
 for part in path.parts[1:-1]:
  try:os.mkdir(part,0o755,dir_fd=parent)
  except FileExistsError:pass
  child=os.open(part,flags,dir_fd=parent);os.close(parent);parent=child
 output=os.open(path.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400,dir_fd=parent);created=True
 with os.fdopen(output,'wb',closefd=False) as handle:handle.write(data)
 os.fchmod(output,0o444);os.close(output);output=None
 check=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
 with os.fdopen(check,'rb') as handle:
  assert stat.S_ISREG(os.fstat(handle.fileno()).st_mode)
  assert hashlib.sha256(handle.read()).hexdigest()==expected
 print(expected)
except BaseException:
 if output is not None:os.close(output)
 if created:os.unlink(path.name,dir_fd=parent)
 raise
finally:os.close(parent)
"""

_VERIFY = r"""import hashlib,json,os,stat,sys
from pathlib import PurePosixPath
for row in json.load(sys.stdin):
 path=PurePosixPath(row['path']); assert path.is_absolute() and '..' not in path.parts
 flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW; parent=os.open('/',flags)
 try:
  for part in path.parts[1:-1]:
   child=os.open(part,flags,dir_fd=parent);os.close(parent);parent=child
  descriptor=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
  with os.fdopen(descriptor,'rb') as handle:
   assert stat.S_ISREG(os.fstat(handle.fileno()).st_mode)
   assert os.fstat(handle.fileno()).st_size==row['bytes']
   assert hashlib.sha256(handle.read()).hexdigest()==row['sha256']
 finally:os.close(parent)
print('verified')
"""


class SessionOutputs:
    """Private output receipts for one actual SDK session and container."""

    def __init__(self, workspace, container_id, wire_root, emit):
        self.workspace = Path(workspace).resolve()
        self.container_id = container_id
        self.wire_root = Path(wire_root).absolute()
        if self.wire_root.is_relative_to(self.workspace):
            raise ValueError("wire receipts must stay outside the model workspace")
        self.emit = emit
        self.session = None
        self.pending = {}
        self.outputs = {}
        self.claimed_wire = set()
        self.deferred = {}

    def before(self, data, tool_id):
        if (
            not isinstance(tool_id, str)
            or not _IDENTIFIER.fullmatch(tool_id)
            or data.get("tool_use_id") != tool_id
            or Path(str(data.get("cwd", ""))).resolve() != self.workspace
        ):
            raise ValueError("SDK callback has no exact tool/workspace binding")
        session = data.get("session_id")
        transcript = Path(str(data.get("transcript_path", "")))
        if (
            not isinstance(session, str)
            or not _IDENTIFIER.fullmatch(session)
            or not transcript.is_absolute()
            or transcript.name != session + ".jsonl"
            or ".." in transcript.parts
        ):
            raise ValueError("SDK call has no exact session/transcript binding")
        binding = (session, transcript)
        if self.session is None:
            self.session = binding
        if binding != self.session:
            raise ValueError("SDK callback belongs to a different session")
        self.pending[tool_id] = {
            "tool": data["tool_name"],
            "input": data["tool_input"],
            "session": binding,
            "wire_seen": {self._wire_key(p) for p in self._wire_files()},
        }

    def _wire_files(self):
        return sorted(self.wire_root.glob("connection-*/call-*.json"))

    def _wire_key(self, path):
        return path.relative_to(self.wire_root).as_posix()

    def _wire_match(self, tool, arguments, payload, excluded, *, blocks=None):
        for path in self._wire_files():
            key = self._wire_key(path)
            if (
                not _WIRE_FILE.fullmatch(path.name)
                or key in self.claimed_wire
                or key in excluded
            ):
                continue
            record = json.loads(_read_owned_file(path, max_bytes=3 * MAX_OUTPUT_BYTES))
            request = base64.b64decode(record["request_base64"], validate=True)
            reply = base64.b64decode(record["reply_base64"], validate=True)
            if _sha(request) != record["request_sha256"] or (
                _sha(reply) != record["reply_sha256"]
            ):
                raise ValueError("private MCP wire receipt changed")
            sent, received = json.loads(request), json.loads(reply)
            params = sent.get("params") or {}
            result = received.get("result") or {}
            content = result.get("content") or []
            same_content = (
                json.dumps(content, sort_keys=True)
                == json.dumps(blocks, sort_keys=True)
                if blocks is not None
                else (
                    len(content) == 1
                    and content[0].get("type") == "text"
                    and isinstance(content[0].get("text"), str)
                    and content[0]["text"].encode("utf-8") == payload
                )
            )
            if (
                sent.get("method") != "tools/call"
                or type(sent.get("id")) is not type(received.get("id"))
                or sent.get("id") != received.get("id")
                or params.get("name") != tool
                or json.dumps(params.get("arguments", {}), sort_keys=True)
                != json.dumps(arguments, sort_keys=True)
                or result.get("isError")
                or not same_content
            ):
                continue
            return key, {
                "wire_receipt": key,
                "request_id": sent["id"],
                "request_sha256": record["request_sha256"],
                "reply_sha256": record["reply_sha256"],
            }
        raise ValueError("SDK spill does not match a completed original MCP reply")

    def register(self, data, tool_id, mcp_prefix):
        call = self.pending.pop(tool_id, None)
        if (
            call is None
            or call["tool"] != data["tool_name"]
            or (
                call["input"] != data["tool_input"]
                or call["session"]
                != (data.get("session_id"), Path(str(data.get("transcript_path", ""))))
            )
        ):
            raise ValueError("SDK output has no matching original call")
        response = data.get("tool_response")
        if data["tool_name"].startswith(mcp_prefix) and isinstance(response, list):
            # The CLI can persist its full block array only after PostToolUse.
            # Its call-ID filename is checked at the next pre-tool boundary;
            # no model command text or arbitrary directory scan grants access.
            self.deferred[tool_id] = (call, response, mcp_prefix)
            return None
        wire_name = None
        receipt = {}
        declared_chars = None
        if data["tool_name"] == "Bash" and isinstance(response, dict):
            supplied = response.get("persistedOutputPath")
            if not supplied:
                return None
            declared_size = response.get("persistedOutputSize")
            if type(declared_size) is not int:
                raise ValueError("native output has no exact byte count")
            receipt["producer"] = "native_bash_metadata"
        elif data["tool_name"].startswith(mcp_prefix) and isinstance(response, str):
            match = _SPILL.match(response)
            if match is None:
                return None
            declared_chars = int(match[1].replace(",", ""))
            supplied = match[2]
            declared_size = None
            receipt["producer"] = "authenticated_mcp_wire"
        else:
            return None
        path = Path(supplied)
        session, transcript = self.session
        expected_root = transcript.parent / session / "tool-results"
        if path.parent != expected_root or ".." in path.parts:
            raise ValueError("SDK output is outside this session's result directory")
        if path in self.outputs:
            raise ValueError("SDK output path is already bound to another result")
        payload = _read_owned_file(path, max_bytes=MAX_OUTPUT_BYTES)
        if declared_size is not None and len(payload) != declared_size:
            raise ValueError("native output byte count differs")
        if declared_chars is not None:
            if len(payload.decode("utf-8").encode("utf-16-le")) // 2 != declared_chars:
                raise ValueError("SDK output character count differs")
            wire_name, wire = self._wire_match(
                data["tool_name"][len(mcp_prefix) :],
                call["input"],
                payload,
                call["wire_seen"],
            )
            receipt.update(wire)
        return self._publish(path, payload, tool_id, receipt, wire_name)

    def _publish(self, path, payload, tool_id, receipt, wire_name):
        session, _ = self.session
        digest = _sha(payload)
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                self.container_id,
                "python3",
                "-c",
                _MIRROR,
                str(path),
                digest,
            ],
            input=payload,
            capture_output=True,
            timeout=30,
            check=True,
        )
        if result.stdout.decode().strip() != digest:
            raise ValueError("container output copy differs from original bytes")
        receipt.update(
            {
                "path": str(path),
                "sha256": digest,
                "bytes": len(payload),
                "tool_use_id": tool_id,
                "session_id": session,
                "container_id": self.container_id,
                "delivery": "available to native tools; no model-read credit",
            }
        )
        self.outputs[path] = receipt
        if wire_name is not None:
            self.claimed_wire.add(wire_name)
        return receipt

    def flush_completed(self):
        """Authenticate call-ID spill files written after the producer hook."""
        session, transcript = self.session
        root = transcript.parent / session / "tool-results"
        for producer, (call, blocks, prefix) in list(self.deferred.items()):
            path = root / (producer + ".json")
            if not path.exists() and not path.is_symlink():
                continue
            del self.deferred[producer]
            try:
                payload = _read_owned_file(path, max_bytes=MAX_OUTPUT_BYTES)
                stored = json.loads(payload)
                if json.dumps(stored, sort_keys=True) != json.dumps(
                    blocks, sort_keys=True
                ):
                    raise ValueError(
                        "SDK block file differs from the original hook result"
                    )
                name, wire = self._wire_match(
                    call["tool"][len(prefix) :],
                    call["input"],
                    payload,
                    call["wire_seen"],
                    blocks=blocks,
                )
                receipt = {
                    "producer": "authenticated_mcp_wire",
                    "representation": "mcp_content_blocks_json",
                    **wire,
                }
                if len(blocks) == 1 and blocks[0].get("type") == "text":
                    receipt["original_text_sha256"] = _sha(
                        blocks[0]["text"].encode("utf-8")
                    )
                receipt = self._publish(path, payload, producer, receipt, name)
                self.emit({"event": "sdk_output_registered", **receipt})
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                self.emit(
                    {
                        "event": "sdk_output_registration_rejected",
                        "tool_use_id": producer,
                        "reason": str(exc),
                    }
                )

    def readable(self, path):
        record = self.outputs.get(path)
        return (
            record is not None
            and _sha(_read_owned_file(path, max_bytes=MAX_OUTPUT_BYTES))
            == record["sha256"]
        )

    def verify_container_copies(self):
        if not self.outputs:
            return
        result = subprocess.run(
            ["docker", "exec", "-i", self.container_id, "python3", "-c", _VERIFY],
            input=json.dumps(list(self.outputs.values())).encode("utf-8"),
            capture_output=True,
            timeout=15,
            check=True,
        )
        if result.stdout.strip() != b"verified":
            raise ValueError("registered container copies are unavailable or changed")


def scoped_hooks(workspace, container_id, emit, wire_root, mcp_prefix="mcp__study__"):
    workspace = Path(workspace).resolve()
    outputs = SessionOutputs(workspace, container_id, wire_root, emit)

    def target_path(raw):
        path = Path(str(raw)).expanduser()
        return (path if path.is_absolute() else workspace / path).resolve()

    def prepare(data, tool_id):
        name, original = data["tool_name"], data["tool_input"]
        outputs.before(data, tool_id)
        outputs.flush_completed()
        event = {
            "event": "pre",
            "tool": name,
            "tool_use_id": tool_id,
            "original_input": original,
            "session_id": data.get("session_id"),
            "effective_effort": data.get("effort"),
        }
        updated = dict(original)
        allowed = False
        if name == "Bash":
            outputs.verify_container_copies()
            updated["command"] = shlex.join(
                [
                    "docker",
                    "exec",
                    "-i",
                    "-w",
                    str(workspace),
                    container_id,
                    "/bin/bash",
                    "-lc",
                    "umask 002\n" + original["command"],
                ]
            )
            allowed = True
        elif name in {"Read", "Edit", "Write"}:
            path = target_path(original.get("file_path", ""))
            allowed = bool(original.get("file_path")) and path.is_relative_to(workspace)
            if name == "Read" and path in outputs.outputs:
                allowed = outputs.readable(path)
            if allowed:
                updated["file_path"] = str(path)
        elif name in {"Grep", "Glob"}:
            supplied = original.get("path") or str(workspace)
            path = target_path(supplied)
            pattern = (
                original.get("pattern", "")
                if name == "Glob"
                else original.get("glob", "")
            )
            own_output = (
                name == "Grep" and path in outputs.outputs and outputs.readable(path)
            )
            allowed = (
                (path.is_relative_to(workspace) or own_output)
                and ".." not in pattern
                and not Path(pattern).is_absolute()
            )
            if allowed and not own_output:
                allowed = not any(
                    p.is_symlink() and not p.resolve().is_relative_to(workspace)
                    for p in workspace.rglob("*")
                )
            if allowed:
                updated["path"] = str(path)
        elif name == "ToolSearch" or name.startswith(mcp_prefix):
            allowed = True
        decision = "allow" if allowed else "deny"
        event.update(
            {"decision": decision, "effective_input": updated if allowed else None}
        )
        emit(event)
        result = {"hookEventName": "PreToolUse", "permissionDecision": decision}
        if allowed:
            outputs.pending[tool_id]["input"] = updated
            result["updatedInput"] = updated
        else:
            result["permissionDecisionReason"] = (
                "Native access is limited to this checkout and authenticated session outputs."
            )
        return {"hookSpecificOutput": result}

    async def before(data, tool_id, context):
        try:
            return prepare(data, tool_id)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            # A failed hook must return a denial; SDK hook exceptions alone
            # do not establish that the pre-approved native tool was blocked.
            emit({"event": "pre_rejected", "tool_use_id": tool_id, "reason": str(exc)})
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "Pilot tool binding or authenticated output "
                        "verification failed."
                    ),
                }
            }

    async def after(data, tool_id, context):
        emit(
            {
                "event": "post",
                "tool": data["tool_name"],
                "tool_use_id": tool_id,
                "tool_input": data["tool_input"],
                "response": data.get("tool_response"),
                "session_id": data.get("session_id"),
                "transcript_path": data.get("transcript_path"),
                "duration_ms": data.get("duration_ms"),
            }
        )
        try:
            receipt = outputs.register(data, tool_id, mcp_prefix)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            emit(
                {
                    "event": "sdk_output_registration_rejected",
                    "tool_use_id": tool_id,
                    "reason": str(exc),
                }
            )
            return {}
        if receipt is not None:
            emit({"event": "sdk_output_registered", **receipt})
        return {}

    return before, after
