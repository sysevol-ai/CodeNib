# Guardian + Coding Agent — Concurrent Architecture

## Overview

Two agents run concurrently inside a single Pier task container: the **coding agent**
(claude-code, codex, etc.) solves the DeepSWE task; the **Guardian MCP server** runs
alongside it, watching the repo and answering tool calls from the coding agent on demand.

From Pier's perspective there is one agent (a custom `GuardianCodingAgent`). Internally
it orchestrates both processes.

---

## Container layout

```
┌─────────────────────────────────────────────────────────┐
│  Pier task container                                     │
│                                                          │
│  ┌─────────────────────┐    MCP tool call (stdio)       │
│  │  Coding agent       │ ──► guardian.query_guardian()  │
│  │  (claude-code/codex)│                           │    │
│  └─────────────────────┘ ◄── findings JSON         │    │
│         reads/edits /app                            │    │
│         commits changes                      ┌──────┘   │
│                                              │           │
│                                   ┌──────────▼────────┐ │
│                                   │  Guardian MCP     │ │
│                                   │  server (stdio)   │ │
│                                   │                   │ │
│                                   │  codeminer/       │ │
│                                   │  guardian/        │ │
│                                   │  mcp_server.py    │ │
│                                   │                   │ │
│                                   │  watches /app,    │ │
│                                   │  runs cycles,     │ │
│                                   │  returns findings │ │
│                                   └───────────────────┘ │
│                                                          │
│  shared filesystem: /app  (the task repo)                │
└─────────────────────────────────────────────────────────┘
```

---

## Transport: MCP stdio (no port needed)

Guardian is exposed as an MCP server over **stdin/stdout**. The coding agent spawns it as
a child process via its MCP config — no HTTP server, no port to manage. Claude Code
supports this natively:

```json
{
  "mcpServers": {
    "guardian": {
      "command": "python",
      "args": ["-m", "codeminer.guardian.mcp_server", "--repo", "/app"]
    }
  }
}
```

The Guardian process shares the container filesystem (`/app`), so it sees the same repo
state the coding agent is editing in real time.

## Codex transport: MCP plus filesystem bridge

Pier's Codex harness writes MCP server entries into Codex's `config.toml`, so the
same stdio MCP server remains registered for Codex.  To make the integration robust
and visible through Codex's normal shell workflow, `GuardianCodingAgent` also starts a
Codex-only filesystem bridge:

```text
python -m codeminer.guardian.codex_bridge --repo /app --out-dir ~/.guardian ...
```

The bridge runs Guardian with `use_llm=True`, analyzes the initial checkout and every
new commit, and writes the latest report to:

```text
~/.guardian/findings.md
~/.guardian/findings.json
~/.guardian/status.json
```

The Codex task preamble tells Codex to read `findings.md` at concrete checkpoints:
early in the task, after substantial refactors or commits, when tests fail
unexpectedly, and before finalizing risky changes.  This makes Guardian's findings
reach Codex even if a specific Codex/Pier MCP path is unavailable, while preserving
the MCP path for Claude Code and any Codex build that supports it.

For Pier environments where Vertex AI is unavailable, `guardian_model=codex:<model>`
uses the Codex Python SDK as Guardian's model transport. The SDK talks to the local
Codex app-server with the same subscription-backed auth used by the Codex CLI. The
adapter keeps Guardian in control of retrieval, probes, memory, and stopping; Codex
only supplies completions. If the SDK/app-server path is unavailable, the adapter
falls back to `codex exec`.

---

## Guardian MCP server (`codeminer/guardian/mcp_server.py`)

Implements the MCP protocol over stdio and exposes one tool:

```
query_guardian(
    hypothesis: str,          # what the agent suspects / wants checked
    region: list[str] | None  # optional focus: ["path/to/file.py:function_name"]
) -> {
    "findings": [...],        # Guardian's current findings for this region
    "memory_unique": [...],   # findings only visible with cross-cycle memory
    "confidence": float
}
```

Internally calls Guardian's existing `hypothesize` + `investigate` logic from
`codeminer/guardian/`. Watches `/app` for new commits (git hook or polling) and fires
a cycle per commit, caching findings between calls so repeated queries are cheap.

---

## Custom Pier agent (`codeminer/guardian/pier_agent.py`)

Registered via Pier's `--agent-import-path`. Orchestrates both processes:

1. Start Guardian MCP server subprocess (`python -m codeminer.guardian.mcp_server --repo /app`)
2. Build the coding agent's MCP config pointing at the Guardian subprocess
3. Launch the coding agent (claude-code / codex) with that MCP config injected
4. Wait for the coding agent to finish
5. Kill Guardian server; container exits normally

```bash
pier run \
  -p deep-swe/tasks/<task> \
  --agent-import-path codeminer.guardian.guardian_coding_agent:GuardianCodingAgent \
  --model gpt-5.6-luna \
  --ae "CODEX_FORCE_AUTH_JSON=1" \
  --ak solver=codex \
  --ak reasoning_effort=high \
  --ak guardian_arm=memory \
  --mounts-json '[{"type":"bind","source":"/home/xiangye/CodeMiner","target":"/codeminer"},{"type":"bind","source":"/tmp/pier-agent-logs","target":"/logs/agent"}]' \
  --jobs-dir deep-swe/jobs \
  -y
```

---

## A/B/C evaluation arms

Same Pier invocation for all three arms; only the Guardian config varies:

| Arm | Coding agent | Guardian server | Memory |
|-----|-------------|-----------------|--------|
| **A** | solo (no MCP tool) | not started | — |
| **B** | + `query_guardian` tool | running, `--arm memoryless` | disabled |
| **C** | + `query_guardian` tool | running, `--arm memory` | enabled (pre-mined) |

- **C − A**: does Guardian help the coding agent at all? *(headline product claim)*
- **C − B**: does cross-cycle memory add value over a memoryless Guardian? *(research question)*
- **B − A**: how much is just a second agent, no memory? *(decomposes the gain)*

---

## When the coding agent calls `query_guardian`

The agent decides when to invoke the tool — Guardian only responds when asked (pull, not
push). Natural call sites in the agent's workflow:

- After a significant refactor: "did I break any contracts?"
- When tests start failing unexpectedly: "what changed in this region?"
- Before committing a risky change: "what does Guardian know about this module?"
- When the task instruction references a complex existing behaviour to preserve

The system prompt for the coding agent instructs it to use `query_guardian` at these
moments and to treat findings as advisory context, not instructions to follow blindly.

---

## What still needs to be built

| Component | File | Status |
|-----------|------|--------|
| Guardian MCP server | `codeminer/guardian/mcp_server.py` | **built** |
| Guardian per-commit watcher | background thread in `mcp_server.py` | **built** |
| GuardianCodingAgent | `codeminer/guardian/guardian_coding_agent.py` | **built** |
| MCP config injection logic | via Pier's `MCPServerConfig` in `guardian_coding_agent.py` | **built** |
| System prompt for coding agent | `codeminer/guardian/prompts/coding_agent.md` | **built** |
| Codex findings file bridge | `codeminer/guardian/codex_bridge.py` + `prompts/codex_file_bridge.md` | **built** |
| Codex SDK model transport | `codeminer/llm/codex_cli_chat.py` | **built** |
