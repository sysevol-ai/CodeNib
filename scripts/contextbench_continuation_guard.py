# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""One bounded Stop intervention for an authenticated, partially read callable.

Source-checkout experiment: no index, model call, transcript-file read or source
injection. Only confirmed native read replies with a real continuation footer
are eligible. A reason contains locations; subsequent tool delivery alone earns
source credit. The caller owns the source context and its cleanup.
"""

import json
import re
from pathlib import Path, PurePosixPath

from codenib.code_chunking import create_chunker
from codenib.eval.benchmarks.contextbench import contextbench_prediction
from codenib.languages import extension_to_language_map
from codenib.types import NODE_TYPE_FUNCTION, NODE_TYPE_METHOD

_CONTINUATION = re.compile(r"\.\.\. \(more lines; use offset=(\d+) to continue\)")
_MAX_SOURCE_BYTES = 2 * 1024 * 1024


class ContinuationGuard:
    """Session-local guard; block at most once, with no retained background work.

    The SDK event loop owns callback state. The callback has no await between
    checking and setting ``intervened``; that assignment is the intervention's
    linearization point. Source inspection uses the existing view -> reader
    lock order. No guard lock, cross-session state or recovery owner is added.
    """

    def __init__(self, ctx, *, observed_messages, max_tail_lines=80, max_files=16):
        if (
            ctx.source_verified is not True
            or ctx.loaded_views
            or any(
                getattr(ctx, name, None) is not None
                for name in ("vector", "symbol_graph", "bm25", "grep_jev")
            )
        ):
            raise ValueError("Continuation guard requires authenticated source only")
        if type(max_tail_lines) is not int or not 1 <= max_tail_lines <= 200:
            raise ValueError("Invalid continuation tail limit")
        if type(max_files) is not int or not 1 <= max_files <= 32:
            raise ValueError("Invalid continuation file limit")
        self.ctx = ctx
        self.observed_messages = observed_messages
        self.max_tail_lines = max_tail_lines
        self.max_files = max_files
        self.identity = (ctx.manifest.commit, ctx.manifest.source_fingerprint)
        self.intervened = False
        self.ledger = {
            "policy": "bounded_callable_tail_v1",
            "max_tail_lines": max_tail_lines,
            "max_files": max_files,
            "stop_events": [],
            "diagnostics": [],
            "failed": False,
        }

    def candidates(self):
        """Inspect actual credited read endpoints, never requested line ranges."""
        with self.ctx._view_lock:
            if self.identity != (
                self.ctx.manifest.commit,
                self.ctx.manifest.source_fingerprint,
            ):
                raise ValueError("Continuation context belongs to another snapshot")
            history = tuple(self.observed_messages())
            confirmed = [
                m
                for i, m in enumerate(history)
                if m.get("role") != "tool"
                or any(x.get("role") == "assistant" for x in history[i + 1 :])
            ]
            prediction = contextbench_prediction(
                instance_id="continuation_guard",
                messages=confirmed,
                repo_path=self.ctx.manifest.repo_path,
            )["traj_data"]
            calls = {
                call["id"]: call["function"]
                for message in confirmed
                for call in message.get("tool_calls", [])
            }
            tools = [m for m in confirmed if m.get("role") == "tool"]
            seen = {
                file: {n for r in spans for n in range(r["start"], r["end"] + 1)}
                for file, spans in prediction["pred_spans"].items()
            }
            parsed = {}
            candidates = []
            languages = extension_to_language_map("chunker")
            for index in reversed(range(len(tools))):
                message, step = tools[index], prediction["pred_steps"][index]
                if calls[message["tool_call_id"]]["name"] != "read":
                    continue
                reply = message.get("content", "")
                footer = _CONTINUATION.fullmatch(reply.rsplit("\n", 1)[-1])
                if not footer or len(step["spans"]) != 1:
                    continue
                file, spans = next(iter(step["spans"].items()))
                end = int(footer[1]) - 1
                if not spans or max(r["end"] for r in spans) != end:
                    # Elided/partial lines must not become an observed endpoint.
                    continue
                last_source_line = reply.rsplit("\n", 2)[-2]
                if last_source_line.endswith(" ... [line truncated]"):
                    continue
                language = languages.get(PurePosixPath(file).suffix)
                if language is None:
                    continue
                if file not in parsed:
                    if len(parsed) >= self.max_files:
                        self.ledger["diagnostics"].append("file_inspection_limit")
                        break
                    # The reader authenticates complete bytes even when only a
                    # bounded prefix is retained. Oversized files are explicit
                    # coverage gaps, not stale-source failures or partial ASTs.
                    source = self.ctx.borrow_source_reader().read_prefix(
                        file, max_bytes=_MAX_SOURCE_BYTES + 1
                    )
                    if len(source) > _MAX_SOURCE_BYTES:
                        parsed[file] = ([], 0)
                        self.ledger["diagnostics"].append(
                            {"file": file, "coverage": "source_byte_limit"}
                        )
                        continue
                    content = source.decode("utf-8", errors="replace")
                    chunker = create_chunker(
                        language, chunk_depth=2, l2_level_exclusive=False
                    )
                    parsed[file] = (
                        chunker.chunk_source(
                            content, file_path=file, relative_path=file
                        ),
                        len(content.splitlines()),
                    )
                chunks, line_count = parsed[file]
                scopes = [
                    c
                    for c in chunks
                    if c.chunk_type in {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}
                    and c.start_line + 1 in seen[file]
                    and c.start_line + 1 <= end < c.end_line + 1 <= line_count
                ]
                if not scopes:
                    continue
                scope = min(scopes, key=lambda c: c.end_line - c.start_line)
                last = scope.end_line + 1
                unread = set(range(end + 1, last + 1)) - seen[file]
                if not unread or last - end > self.max_tail_lines:
                    continue
                candidates.append(
                    {
                        "tool_call_id": message["tool_call_id"],
                        "file": file,
                        "symbol": scope.name,
                        "function_lines": [scope.start_line + 1, last],
                        "last_read_line": end,
                        "unseen_tail_lines": len(unread),
                        "read_next": {
                            "file_path": file,
                            "offset": end + 1,
                            "limit": last - end,
                        },
                    }
                )
            return candidates

    async def stop(self, data, tool_use_id, context):
        event = {"decision": "allow", "stop_hook_active": data.get("stop_hook_active")}
        self.ledger["stop_events"].append(event)
        if (
            data.get("hook_event_name") != "Stop"
            or data.get("stop_hook_active") is not False
            or self.intervened
        ):
            return {}
        if (
            Path(data.get("cwd", "")).resolve()
            != Path(self.ctx.manifest.repo_path).resolve()
        ):
            event["diagnostic"] = "hook_repository_mismatch"
            return {}
        try:
            candidates = self.candidates()
        except Exception as exc:
            # Drain the SDK terminal before the caller rejects this attempt,
            # retaining usage even when the retained source authority is stale.
            self.ledger["failed"] = True
            event["error"] = f"{type(exc).__name__}: {exc}"
            return {}
        if not candidates:
            return {}
        candidate = candidates[0]
        reason = (
            "CodeNib found an incomplete native read inside a callable you already "
            "entered. Inspect its bounded remaining tail before finishing your "
            "investigation, then update the handoff. This is location metadata, "
            "not additional source or a requirement to read the rest of the file: "
            + json.dumps(candidate, ensure_ascii=True)
        )
        self.intervened = True
        event.update(decision="block", candidate=candidate, reason=reason)
        return {"decision": "block", "reason": reason}
