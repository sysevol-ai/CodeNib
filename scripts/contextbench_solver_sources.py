# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Source receipts at the existing mini SWE-agent environment/model boundary.

This is a checkout-only benchmark adapter, not a shell parser. Arbitrary shell
output remains available to the solver and explicitly unattributed. Only the
controlled reader binds output to one source version; changed files never gain
original-commit line credit through guessed offsets or text matching.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path


def receipt_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def initial_source_receipt(context, repo, commit):
    """Validate delivered retrieval text against the already-audited base tree."""
    sources, lines = {}, {}
    root = repo.resolve()
    for node in json.loads(context):
        path = (root / node["file"]).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Delivered source is outside the pinned checkout")
        file = path.relative_to(root).as_posix()
        if file not in sources:
            data = path.read_bytes()
            sources[file] = {
                "file": file,
                "source_sha256": hashlib.sha256(data).hexdigest(),
            }
            lines[file] = data.decode("utf-8", errors="replace").splitlines()
        for text in node["content"].splitlines():
            match = re.fullmatch(r"(\d+) \| (.*)", text)
            if not match:
                raise ValueError("Delivered context lacks complete numbered source")
            number, content = int(match[1]), match[2]
            if not 1 <= number <= len(lines[file]) or (
                content.rstrip() != lines[file][number - 1].rstrip()
            ):
                raise ValueError("Delivered source differs from the pinned checkout")
            sources[file].setdefault("lines", []).append(number)
    receipt = {
        "base_commit": commit,
        "context": context,
        "sources": list(sources.values()),
    }
    return {"receipt": receipt, "sha256": receipt_digest(receipt)}


def source_output(source):
    return f"File: {source['file']}\n" + "".join(
        f"{number} | {line}\n"
        for number, line in enumerate(source["lines"], source["start_line"])
    )


class SourceEnvironment:
    """Record executions before the next model request can confirm delivery.

    Only the owning synchronous solver loop calls this wrapper. The underlying
    environment retains container ownership. A checkpoint after execution is the
    receipt boundary; it proves no model delivery until a subsequent confirmed
    request contains the exact rendered observation at the recorded index.
    """

    def __init__(self, environment, *, root, commit, receipts, checkpoint, observation):
        self.environment = environment
        self.root, self.commit = root, commit
        self.receipts, self.checkpoint = receipts, checkpoint
        self.observation = observation

    def get_template_vars(self):
        return self.environment.get_template_vars()

    def execute(self, command):
        source = None
        kind = "shell_unattributed"
        try:
            if command.split(maxsplit=1)[:1] == ["codenib-read"]:
                kind = "source_read"
                try:
                    args = shlex.split(command)
                    if len(args) != 4:
                        raise ValueError("Use codenib-read PATH START END alone")
                    request = {
                        "root": self.root,
                        "commit": self.commit,
                        "file": args[1],
                        "start": int(args[2]),
                        "end": int(args[3]),
                    }
                    script = (
                        Path(__file__)
                        .with_name("_contextbench_solver_read.py")
                        .read_text()
                    )
                    raw = self.environment.execute(
                        shlex.join(["python3", "-c", script, json.dumps(request)])
                    )
                    if raw["returncode"]:
                        raise ValueError("Source reader failed in the container")
                    data = json.loads(raw["output"])
                    if "error" in data:
                        raise ValueError(data["error"])
                    source = data["source"]
                    output = {"returncode": 0, "output": source_output(source)}
                except (ValueError, KeyError, TypeError) as exc:
                    output = {"returncode": 1, "output": f"Source read failed: {exc}\n"}
            else:
                output = self.environment.execute(command)
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            raw = getattr(exc, "output", None) or b""
            text = (
                raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            )
            self._record(command, kind, {"output": text}, None, timeout=True)
            raise
        self._record(command, kind, output, source)
        return output

    def _record(self, command, kind, output, source, *, timeout=False):
        index, rendered = self.observation(command, output, timeout)
        receipt = {
            "command": command,
            "kind": kind,
            "output": output,
            "timeout": timeout,
            "source": source,
            "message_index": index,
            "rendered_observation": rendered,
        }
        self.receipts.append({"receipt": receipt, "sha256": receipt_digest(receipt)})
        self.checkpoint()


def _verified(envelope):
    receipt = envelope["receipt"]
    if receipt_digest(receipt) != envelope["sha256"]:
        raise ValueError("Solver source receipt changed")
    return receipt


def _delivered(inputs, index, content):
    return [
        item["turn"]
        for item in inputs
        if len(item["messages"]) > index
        and item["messages"][index] == {"role": "user", "content": content}
    ]


def source_attribution(row, confirmed):
    """Return auditable base-coordinate coverage, explicitly partial for shells."""
    seen, events = {}, []
    observation_indexes = {
        index
        for item in confirmed
        for index, message in enumerate(item["messages"])
        if index > 1 and message["role"] == "user"
    }
    recorded_indexes = set()
    initial = row.get("initial_source_receipt")
    if initial:
        receipt = _verified(initial)
        block = "<retrieved_source>\n" + receipt["context"] + "\n</retrieved_source>"
        turns = [
            item["turn"]
            for item in confirmed
            if len(item["messages"]) > 1
            and item["messages"][1]["role"] == "user"
            and block in item["messages"][1]["content"]
        ]
        if turns:
            for source in receipt["sources"]:
                seen.setdefault(source["file"], set()).update(source["lines"])
        events.append(
            {
                "kind": "initial_context",
                "confirmed_turns": turns,
                "sha256": initial["sha256"],
            }
        )
    for envelope in row.get("execution_receipts", []):
        receipt = _verified(envelope)
        recorded_indexes.add(receipt["message_index"])
        turns = _delivered(
            confirmed, receipt["message_index"], receipt["rendered_observation"]
        )
        source = receipt["source"]
        visible = []
        if turns and source:
            if source_output(source) != receipt["output"]["output"]:
                raise ValueError("Solver source receipt output differs")
            for number, text in enumerate(source["lines"], source["start_line"]):
                if f"\n{number} | {text}\n" in receipt["rendered_observation"]:
                    visible.append(number)
            if source["source_sha256"] == source["base_sha256"]:
                seen.setdefault(source["file"], set()).update(visible)
        events.append(
            {
                "kind": receipt["kind"],
                "message_index": receipt["message_index"],
                "confirmed_turns": turns,
                "sha256": envelope["sha256"],
                "file": source["file"] if source else None,
                "source_sha256": source["source_sha256"] if source else None,
                "base_version": bool(
                    source and source["source_sha256"] == source["base_sha256"]
                ),
                "visible_source_lines": visible,
            }
        )
    return {
        "status": (
            "partial" if initial or "execution_receipts" in row else "missing_receipts"
        ),
        "base_source_lines": {
            file: sorted(lines) for file, lines in sorted(seen.items())
        },
        "base_source_line_count": sum(len(lines) for lines in seen.values()),
        "initial_source_receipt_available": bool(initial),
        "missing_execution_receipt_indexes": sorted(
            observation_indexes - recorded_indexes
        ),
        "events": events,
        "scope": (
            "Confirmed initial retrieval and complete controlled-reader lines only. "
            "Arbitrary shell output, partial/elided lines and modified-file coordinates "
            "receive no original-source credit. This is not a full ContextBench score."
        ),
    }
