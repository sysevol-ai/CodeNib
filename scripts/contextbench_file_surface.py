# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""On-demand file outlines and source handles without repository vector builds.

This source-checkout experiment parses only the file the agent asks for, through
the existing chunker and authenticated source reader. Session handles, body
pagination and dedup share the indexed prototype's contract. No graph reference
counts, persistent index, provider call or source eviction is introduced.
"""

import json
from pathlib import PurePosixPath

from codenib.agent.tools.spec import ToolInputSpec, ToolSpec
from codenib.code_chunking import create_chunker
from codenib.languages import extension_to_language_map
from codenib.mcp.tools.source import _repository_relative_source
from scripts.contextbench_surface import Location, SurfaceContext

GUIDANCE = """Use glob/grep to locate relevant repository files. Ask
explore_context(view='surface', file_path=...) for a file/definition outline
before broad source reads, then request relevant handles with view='body'.
Pass near_line from grep to focus on enclosing/nearby definitions; keep that
same near_line when paging through the outline.
Surface replies contain declarations and locations, not behavior evidence.
Use next_cursor to request more outline entries and next_start_line for large
bodies. Revisit the outline when other definitions in a located file may matter.
The outline covers only the requested file: discover related code, configuration,
documentation and tests with glob/grep when relevant. Non-code files have a file
handle rather than code symbols. Read/grep/glob remain available for precise
follow-up. Fetch only relevant bodies; an outline is not an exhaustive task map.
Graph references are unavailable in this source-only policy."""


class FileSurfaceContext(SurfaceContext):
    def __init__(self, ctx, *, observed_messages, token_budget=2048):
        if ctx.loaded_views or any(
            getattr(ctx, name, None) is not None
            for name in ("vector", "symbol_graph", "bm25", "grep_jev")
        ):
            raise ValueError("File surfaces require an explicitly source-only context")
        self._initialize_source_session(ctx, observed_messages, token_budget)

    def execute(
        self,
        file_path="",
        view="surface",
        handle="",
        start_line=0,
        cursor=0,
        near_line=0,
    ):
        with self.ctx._view_lock:
            self._check_identity()
            if view == "body":
                if file_path or cursor or near_line:
                    raise ValueError(
                        "Body requests use a handle, not an outline cursor"
                    )
                return self._body(handle, start_line)
            if view != "surface" or handle or start_line:
                raise ValueError("Outline requests use file_path and cursor")
            if type(cursor) is not int or cursor < 0:
                raise ValueError("Invalid outline cursor")
            if type(near_line) is not int or near_line < 0:
                raise ValueError("Invalid outline focus line")
            relative = _repository_relative_source(self.ctx, file_path)
            source = self.ctx.read_source_bytes(relative, max_bytes=2 * 1024 * 1024)
            text = source.decode("utf-8", errors="replace")
            lines = text.splitlines()
            language = extension_to_language_map("chunker").get(
                PurePosixPath(relative).suffix
            )
            locations = []
            if language is not None:
                chunker = create_chunker(
                    language, chunk_depth=2, l2_level_exclusive=False
                )
                chunks = chunker.chunk_source(
                    text, file_path=relative, relative_path=relative
                )
                locations.extend(
                    Location(relative, c.start_line + 1, c.end_line + 1, c.name)
                    for c in sorted(
                        chunks, key=lambda c: (c.start_line, c.end_line, c.name)
                    )
                )
            if lines:
                locations.append(Location(relative, 1, len(lines), relative))
            if near_line > len(lines):
                raise ValueError("Outline focus line lies outside authenticated source")
            if near_line:
                locations.sort(
                    key=lambda loc: (
                        max(loc.start - near_line, near_line - loc.end, 0),
                        loc.end - loc.start,
                        loc.start,
                        loc.symbol,
                    )
                )
            locations = list(dict.fromkeys(locations))
            if cursor > len(locations):
                raise ValueError("Outline cursor lies outside the file's definitions")
            payload = {
                "kind": "codenib_surface_v1",
                "commit": self.identity[0],
                "source_fingerprint": self.identity[1],
                "graph_available": False,
                "file": relative,
                "parser": language or "plain_file",
                "near_line": near_line,
                "entries": [],
                "next_cursor": None,
                "diagnostics": [],
            }
            for index in range(cursor, len(locations)):
                location = locations[index]
                if not 1 <= location.start <= location.end <= len(lines):
                    raise ValueError(
                        "Parsed definition lies outside authenticated source"
                    )
                entry = self._location_entry(
                    location, lines[location.start - 1].strip()[:240], ["file_outline"]
                )
                handle = entry["handle"]
                if handle not in self.locations and len(self.locations) >= 256:
                    payload["diagnostics"].append("session_handle_limit_reached")
                    break
                # Reserve the longest possible continuation cursor while
                # checking bytes/tokens; no hidden body is added to the page.
                candidate = {
                    **payload,
                    "entries": [*payload["entries"], entry],
                    "next_cursor": len(locations),
                }
                encoded = json.dumps([candidate], ensure_ascii=True)
                if (
                    len(self.encoder.encode(encoded, disallowed_special=()))
                    > self.token_budget
                ):
                    if not payload["entries"]:
                        if (
                            "oversized_metadata_entry_omitted"
                            not in payload["diagnostics"]
                        ):
                            payload["diagnostics"].append(
                                "oversized_metadata_entry_omitted"
                            )
                        continue
                    payload["next_cursor"] = index
                    break
                if handle in self.locations and self.locations[handle] != location:
                    raise RuntimeError("Surface handle collision")
                self.locations[handle] = location
                payload["entries"].append(entry)
                if len(payload["entries"]) == 8:
                    payload["next_cursor"] = (
                        index + 1 if index + 1 < len(locations) else None
                    )
                    break
            reply = json.dumps([payload], ensure_ascii=True)
            if (
                len(self.encoder.encode(reply, disallowed_special=()))
                > self.token_budget
            ):
                raise ValueError("File outline metadata exceeds its token budget")
            return reply

    def tool(self):
        return ToolSpec(
            tool_id="explore_context",
            executor_fn=self.execute,
            description=(
                "CodeNib on-demand file context: view=surface with file_path lists "
                "definitions and handles; use next_cursor for further entries. "
                "view=body with a handle returns authenticated source, omitting "
                "confirmed prior reads. Non-code files return a file handle."
            ),
            inputs=[
                ToolInputSpec("file_path", "str", required=False, default=""),
                ToolInputSpec(
                    "view",
                    "str",
                    required=False,
                    default="surface",
                    enum=["surface", "body"],
                ),
                ToolInputSpec("handle", "str", required=False, default=""),
                ToolInputSpec("start_line", "int", required=False, default=0),
                ToolInputSpec("cursor", "int", required=False, default=0),
                ToolInputSpec("near_line", "int", required=False, default=0),
            ],
        )
