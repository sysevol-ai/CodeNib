# Default Tool Primitives: file_read + grep

*Issue #145 — Phase 0/1 Prep for Agent Router (CAR)*

## Overview

`file_read` and `grep` are always-on tool primitives available to every agent
regardless of which `Ax` skill subset is loaded.  They live in
`codeminer/agent/skills/defaults.py` and are registered by `AgentRunner`
during `__init__` — prior to any `exclude_skills` filtering.

---

## Reference survey: opencode vs openhands vs codex

Three reference implementations were evaluated for line-number format and
API surface.

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

opencode's `{lineno:6d} | {content}` format was chosen for the following
reasons:

1. **Explicit reviewer request** — @siriuxyu's comment on #145 says:
   > "Please make line numbers visible per row (opencode-style
   > `<lineno> | <content>`, or similar)."

2. **LLM usability** — when an LLM reads a snippet and wants to follow up
   with `graph_expand(ranges=[...])` or another `file_read(start_line=N)`,
   it can copy the number directly instead of counting from the top of the
   snippet.

3. **Token efficiency** — plain `{n} | {line}` is more compact than XML tags
   and adds only ~8 chars per line.

4. **Consistency** — opencode-style output is already familiar to engineers
   on the team (VS Code's peek-definition uses similar numbering).

---

## Line-number convention: 1-based throughout

Both `file_read` inputs (`start_line`, `end_line`) and output line labels are
**1-based**, aligned with the convention settled in issue #147/#153.

- `start_line=1` reads from the very first line (default).
- Truncation notices emit `start_line=N` for the next call, also 1-based.
- `grep` output uses 1-based `{file}:{lineno}: {content}` (POSIX grep style).

---

## Token safety

| Tool | Parameter | Default | Hard max |
|------|-----------|---------|----------|
| `file_read` | `max_lines` | 200 | caller-settable |
| `grep` | `max_results` | 50 | caller-settable |

When either cap is reached a truncation notice is appended so the LLM knows
to narrow the query rather than silently receiving a partial result.

---

## AgentRunner wiring

```python
# runner.py __init__
ensure_defaults_registered(self.registry)  # idempotent
# ...
exclude -= DEFAULT_SKILL_IDS              # never filter out defaults
self.tools = registry_to_tools(self.registry, exclude=exclude)
```

`DEFAULT_SKILL_IDS = frozenset({"file_read", "grep"})` is the canonical set
used everywhere — import it from `codeminer.agent.skills.defaults`.

---

## What is NOT in scope (per #145)

- Changes to sweep-variable skills: `bm25_search`, `embedding_search`,
  `graph_expand`, `regex_search` (A_g index-backed version)
- New LLM-powered skills
- Changes to the compiler or resource guard
