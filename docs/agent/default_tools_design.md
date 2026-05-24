<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Default Tool Primitives: file_read + regex_search

*Issue #145 — Phase 0/1 Prep for Agent Router (CAR)*

## Overview

`file_read` and `regex_search` are the **two** always-on default tool
primitives. They live in `codeminer/agent/skills/defaults.py` and are
registered into every `AgentRunner` during `__init__` — prior to any
`exclude_skills` or `allow_skills` filtering. Every `Ax` skill subset
(A0–A6) builds on top of them.

`regex_search` is a **multi-mode** tool — one skill, three back-ends,
dispatched by the `mode` argument:

| `mode` | Back-end | Use case |
|--------|----------|----------|
| `"content"` *(default)* | grep-style (`re` over file contents) | Find call sites / strings / patterns. |
| `"files"` | glob-style (`Path.rglob`) | Enumerate files matching a name pattern. |
| `"shell"` | bash-style (`subprocess.run(shell=True)`) | Anything else (`pytest`, `git`, `find`, ...). |

This matches #145's "single tool" option: the issue says
*"`regex_search`: grep / glob / bash-style primitives (single tool or split
— your call)"*. Bundling all three behind one skill keeps the default set
at **exactly two** (matching the issue title `file_read + regex_search`)
while still exposing every back-end #145 lists.

---

## Reference survey: opencode vs openhands vs codex

Three reference implementations were evaluated for the line-number format
of `file_read`.

### opencode

- **Format:** `{lineno} | {content}` (left-padded integer, pipe delimiter)
- **Pagination:** `start_line` / `end_line` + hard `max_lines` cap
- **Error handling:** returns human-readable error strings (not exceptions)

### openhands

- **Format:** similar `{n} | {line}` but with a dash variant in some versions
- **Pagination:** similar range params
- **Error handling:** may raise exceptions; caller must catch

### codex

- **Format:** XML-like `<line n="1">content</line>` tags
- **Pagination:** offset-based
- **Token cost:** higher due to tag overhead

### Decision: follow opencode

opencode's `{lineno:6d} | {content}` format was chosen because:

1. **Explicit reviewer request** — @siriuxyu's comment on #145:
   > "Please make line numbers visible per row (opencode-style
   > `<lineno> | <content>`, or similar)."
2. **LLM usability** — when the LLM picks up a line number to feed back
   into `graph_expand(ranges=[...])` or another `file_read(start_line=N)`,
   it can copy the number directly without counting from the top.
3. **Token efficiency** — `{n} | {line}` is more compact than XML tags
   and adds ~8 chars per line.
4. **Familiarity** — VS Code's peek-definition uses similar numbering.

---

## Line-number convention: 1-based throughout

Both `file_read` inputs (`start_line`, `end_line`) and output line labels
are **1-based**, aligned with #147/#153.

- `start_line=1` reads from the very first line (default).
- Truncation notices emit `start_line=N` for the next call, also 1-based.
- `regex_search` (content mode) output uses 1-based
  `{file}:{lineno}: {content}` (POSIX grep style).

The single 0↔1 conversion site for `file_read` is
`all_lines[start - 1 : end]`; matches the "one conversion at the
boundary" pattern from #153.

---

## Token safety

| Tool | Parameter | Default | Notes |
|------|-----------|---------|-------|
| `file_read` | `max_lines` | 200 | Caller-settable; truncation notice with `start_line=N`. |
| `regex_search` (content / files) | `max_results` | 50 | Caller-settable. |
| `regex_search` (shell) | `timeout` | 30 s | Caller-settable. |
| `regex_search` (shell) | output cap | 16 000 chars | Non-configurable. Mirrors `runner._MAX_RESULT_CHARS`. |

Every cap appends a truncation / cap notice so the LLM knows to narrow
the query rather than silently consume a partial result.

---

## Shell-mode safety policy

**Loose, by design.** Shell mode calls
`subprocess.run(command, shell=True, ...)` with no command filtering, no
allow/deny list, and no path jail.

Rationale:

- The agent operator already chooses what binary the runner has access
  to — containers / VMs / process sandboxes are the right enforcement
  layer.
- An in-process allow/deny list quickly becomes either too restrictive
  (blocking legitimate `pytest` / `git` use) or trivially bypassable
  (`bash -c '...'`, `eval`, env-var games). Either failure mode is
  worse than honesty about the trust boundary.
- `timeout` (default 30 s) protects against hangs; the 16k output cap
  protects against runaway producers (`yes`, `seq`, etc.).

**Callers running the agent in untrusted contexts must sandbox at the
environment level.**

---

## AgentRunner wiring

```python
# runner.py __init__
ensure_defaults_registered(self.registry)  # idempotent

# Resource guard / caller-supplied filters run first, then we strip
# DEFAULT_SKILL_IDS from exclude and union it into allow so the always-on
# primitives survive both filter paths.
exclude -= DEFAULT_SKILL_IDS
if allow is not None:
    allow |= DEFAULT_SKILL_IDS

self.tools = registry_to_tools(self.registry, allow=allow, exclude=exclude)
```

`DEFAULT_SKILL_IDS = frozenset({"file_read", "regex_search"})` is the
canonical set — import it from `codeminer.agent.skills.defaults`.

---

## Why one multi-mode `regex_search` instead of three split skills

We considered splitting `regex_search` into three skills (`regex_search`,
`glob`, `bash`) and rejected it:

- The #145 title is literally `file_read + regex_search (grep / glob /
  bash)`. The parenthetical lists *implementation flavors*, not extra
  primitives — three skills would expand the default set to four and
  break the title's contract.
- The #133 RFC's motivation is *minimising* agent tool-choice fan-out;
  shipping three skills where the issue asks for one moves in the wrong
  direction.
- LLM routing remains explicit via the `mode` argument; in practice
  agents already pick the right mode just from skill_doc + the
  `mode`-tagged parameter descriptions.

The trade-off is a slightly busier `inputs` schema (mode-irrelevant
parameters are silently ignored), which we accept in exchange for
matching the #145 title literally.

---

## What is NOT in scope (per #145)

- Changes to sweep-variable skills: `bm25_search`, `embedding_search`,
  `graph_expand`, `regex_search` (A_g index-backed version — distinct
  from the always-on default added here).
- New LLM-powered skills.
- Changes to the compiler or resource guard.
