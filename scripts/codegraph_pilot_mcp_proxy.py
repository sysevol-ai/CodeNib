#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Record actual pilot MCP replies while forwarding their original bytes.

This source-checkout helper owns its child process group. The workflow owns
the container and removes it after SDK disconnect, including outstanding work.
Receipts live outside every model workspace and container mount. Publishing a
completed receipt by atomic replace is its visibility point; one event-loop
writer owns request matching and monotonically numbered receipt files.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import signal
import stat
import sys
import tempfile
from pathlib import Path

MAX_WIRE_BYTES = 16 * 1024 * 1024


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_key(value):
    if type(value) not in (str, int):
        return None
    return type(value).__name__, value


class WireReceipts:
    """One private, single-writer log of completed tools/call exchanges."""

    def __init__(self, root: Path):
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = root.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError("MCP receipts require a private owned directory")
        # Reconnection starts another writer without sharing a mutable counter
        # or reusing earlier request IDs. Each connection owns its directory.
        self.root = Path(tempfile.mkdtemp(prefix="connection-", dir=root))
        self.pending = {}
        self.sequence = 0

    def request(self, line: bytes) -> None:
        message = json.loads(line)
        if message.get("method") != "tools/call":
            return
        key = request_key(message.get("id"))
        if key is None:
            return
        if key in self.pending:
            raise ValueError("duplicate in-flight MCP request ID")
        self.pending[key] = line

    def reply(self, line: bytes) -> None:
        message = json.loads(line)
        key = request_key(message.get("id"))
        original = self.pending.pop(key, None)
        if original is None:
            return
        self.sequence += 1
        record = {
            "schema": "codenib.pilot-mcp-wire.v1",
            "sequence": self.sequence,
            "request_base64": base64.b64encode(original).decode("ascii"),
            "request_sha256": sha256(original),
            "reply_base64": base64.b64encode(line).decode("ascii"),
            "reply_sha256": sha256(line),
        }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.root, delete=False
            ) as handle:
                temporary = Path(handle.name)
                json.dump(record, handle)
                handle.write("\n")
            os.replace(temporary, self.root / f"call-{self.sequence:06d}.json")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


async def proxy(command: list[str], root: Path) -> int:
    receipts = WireReceipts(root)
    reader = asyncio.StreamReader(limit=MAX_WIRE_BYTES + 1)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    child = None
    tasks = []
    try:
        child = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
            start_new_session=True,
            limit=MAX_WIRE_BYTES + 1,
        )

        async def requests():
            while line := await reader.readline():
                if len(line) > MAX_WIRE_BYTES:
                    raise ValueError("pilot MCP request exceeds the wire cap")
                receipts.request(line)
                child.stdin.write(line)
                await child.stdin.drain()
            child.stdin.close()

        async def replies():
            while line := await child.stdout.readline():
                if len(line) > MAX_WIRE_BYTES:
                    raise ValueError("pilot MCP reply exceeds the wire cap")
                receipts.reply(line)
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()

        tasks = [asyncio.create_task(requests()), asyncio.create_task(replies())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if tasks[0] in done:
            await asyncio.wait_for(tasks[1], timeout=5)
        return await asyncio.wait_for(child.wait(), timeout=5)
    finally:
        transport.close()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if child is not None and child.returncode is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(child.wait(), timeout=3)
            except asyncio.TimeoutError:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("an explicit MCP server command is required")
    return asyncio.run(proxy(command, args.receipts))


if __name__ == "__main__":
    raise SystemExit(main())
