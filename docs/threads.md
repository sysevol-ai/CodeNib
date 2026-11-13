<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Thread context

`codenib threads` gives a coding agent what it needs from a GitHub issue or
pull request in one read: the ask, what people decided, review comments, the
failing checks, the threads it is linked to, and the code those threads point
at. It fits the result into a token budget, and when the agent comes back to
the thread later it returns only what changed. It runs on your machine and
calls no model.

An agent that reads a thread itself spends a tool call on each piece: the
issue, its comments, the linked pull request, the reviews, the checks, a log.
Every call carries the whole conversation so far, so the cost is the number of
calls, not the size of the thread. Putting one prepared context in front of the
agent removes most of those calls.

!!! note "Preview"
    Thread context is a preview. It reads GitHub only, and its code locations
    come from names, paths and diffs in the thread, not from the code graph.

## What the agent receives

One Markdown document, built by code alone:

- **Status.** Open, closed or merged, labels, and the checks of the latest
  commit. These are computed from GitHub's own events.
- **Coverage.** One line that says whether everything people wrote is included
  in full, or how many posts were shortened or left out. An agent can then
  tell whether it still has to read the thread. The thread's own posts are
  kept whole before anything from linked threads.
- **What a pull request changes.** Its files and size, so nobody asks for the
  diff to find out.
- **The ask.** The title and body of the issue or pull request.
- **Code locations.** Paths, stack frames, review positions and symbol names
  from the thread, resolved to files, symbols and line ranges at a commit, with
  the source of the symbols that matter most.
- **Failing CI.** How many checks fail and pass, then the lines around each
  error. Jobs that fail the same way appear once.
- **Also in progress on this code.** Open pull requests, outside this thread,
  that change the same symbols or source files. An agent working one thread
  cannot otherwise see that someone else is changing the same function.
- **Linked threads.** Each linked issue or pull request with its state, what
  it changed and how it was reviewed, followed by what people wrote there.
- **Discussion and review comments**, each review comment with the lines it
  was made on, newest kept first when the budget is tight.

Bot comments, quoted replies and passing checks are left out, and a paragraph
that one thread repeats from another appears once. Every part carries the id of
the event it came from, such as `#812/comment/2990023933`.

## Use it

Thread context needs CodeNib, Git, and a GitHub token in `GH_TOKEN`, or an
installed and signed-in `gh`. Run it inside a clone of the repository:

```bash
codenib threads show 812                  # print the context of issue or PR 812
codenib threads show 812 --budget 1500    # a tighter budget
codenib threads context 812               # write THREAD_CONTEXT.md for an agent
```

The first call fetches the thread and the threads it links to. Pass `--sync`
to fetch again.

### Before an agent starts

Run `codenib threads context` from whatever starts your agent, and point the
agent at the file. This does not depend on the agent choosing to call a tool:

```bash
codenib threads context "$ISSUE_NUMBER" --sync --output THREAD_CONTEXT.md
```

### Work many threads at once

A ticket is one piece of work that starts from an issue or pull request. It
follows that thread and the threads linked to it, and it remembers what it has
already read, so nobody has to keep a cursor.

```bash
codenib threads open 812 --dir ../work/812   # start: writes THREAD_CONTEXT.md there
codenib threads poll                         # fetch what changed, for all tickets
codenib threads inbox 812                    # what ticket 812 has not read yet
codenib threads board                        # all tickets at a glance
codenib threads close 812
```

- **One inbox per ticket.** `inbox` returns what any followed thread gained
  since the ticket last read, and marks it read. A thread that became linked
  since then, such as a new pull request for the issue, joins the ticket.
- **One thread, many tickets.** When two tickets follow the same thread, it is
  fetched and stored once, and each ticket reads it from its own position.
- **A poll costs what changed.** `poll` asks GitHub once which threads changed
  and fetches only those some ticket follows. With 24 tickets following 71
  threads of `astral-sh/ruff`, fetching everything took 278 requests, and a
  poll with nothing new took 13.
- **The board** lists each ticket with its unread count, the other tickets
  whose threads name the same symbol, and open pull requests that change
  those symbols.

A comment edited after a ticket read it comes back in the inbox with its new
text.

### See who else is changing the same code

Index the open pull requests of the repository, and each context then names
the ones that change the same code:

```bash
codenib threads sync --open-pulls        # the 100 most recently updated
```

Run it again to refresh. In `astral-sh/ruff`, 30 of the 59 most recently
updated open pull requests changed a symbol that another of them also changed
(November 2026).

### When an agent comes back

Each context ends with a cursor, the id of the newest event it saw. Pass it
back to get only what happened since: new comments and reviews, a status
change, new check results.

```bash
codenib threads show 812 --sync --since '#812/comment/2990023933'
```

### From an agent, over MCP

`codenib threads mcp` serves the same context to any agent that speaks the
Model Context Protocol. It is a separate server from CodeNib's code search, so
adding it does not change which code tools the agent sees.

```json
{ "command": "codenib", "args": ["threads", "mcp"] }
```

| Tool | Returns |
|---|---|
| `thread_context` | The context of one thread, or what followed a cursor |
| `ticket_open` | The first context of a ticket; remembers what was read |
| `ticket_inbox` | What the ticket's threads gained since it last read |
| `tickets_board` | All tickets, unread counts and shared code |
| `thread_events` | Raw events by id, including a failed check's log |

Writing the context into a file before the agent starts does not depend on the
agent choosing to call a tool, so prefer it for the first context and use the
tools for what comes later.

### From Python

```python
from pathlib import Path
from codenib.threads import ThreadStore, TicketBook, open_ticket, read_inbox

book = TicketBook(ThreadStore("acme/widget"))
ticket, context = open_ticket(book, 812, checkout=Path("."))
news = read_inbox(book, ticket)        # None when nothing is new
```

`show`, `open`, `inbox` and `board` also print JSON with `--json`.

### Check a citation

```bash
codenib threads events '#812/comment/2990023933'
```

## How well it works

With the same Codex agent, the prepared context led to the same answers as
reading threads with `gh`, for about a fifth to a quarter fewer input tokens.
The saving grows with the size of the thread.

The agent was asked which files and symbols have to change, and its answer was
compared with what the merged pull request changed. Three ways of getting
thread context were compared: the agent reads threads itself with `gh`; every
thread event is pasted into the prompt unprocessed; or the prompt carries the
`codenib threads` context.

| Task | Tasks | Files found: `gh` / pasted / CodeNib | Input tokens against `gh` | Against pasted |
|---|---|---|---|---|
| Take over a pull request after review or a failed check | 76 | 0.74 / 0.75 / 0.75 | −21% | −32% |
| The same, threads over 20,000 tokens | 22 | 0.63 / 0.65 / 0.63 | −34% | −52% |
| Take over an issue before any fix exists | 47 | 0.82 / 0.85 / 0.85 | −25% | +2% |

On short threads, pasting everything costs as little as the prepared context.
The prepared context pulls ahead once reviews, linked pull requests and CI
logs make the thread long. It does not make answers better; it makes the same
answers cheaper.

For an agent that returns to a thread, a first context followed by updates
took a median of 2,897 tokens over the life of an issue, against 21,103 for
reading the whole thread each time (151 issues, a median of 6 returns each).

??? note "Scope and method"
    - Repositories: `astral-sh/ruff`, `caddyserver/caddy` and
      `pydantic/pydantic` for pull requests; the first two for issues.
    - Each task shows the thread as it stood at a cut-off, and a snapshot of
      the repository at that point with no history, so no arm can read the fix.
      A read-only replica of `gh` limited to the cut-off is available in every
      arm.
    - The `gh` and pasted arms ran once per task and the CodeNib arm twice;
      its two runs differed by 6% in total input tokens on pull requests and 1%
      on issues. Intervals are 95% paired bootstrap over tasks. Pull requests:
      −27% to −14% against `gh`, −40% to −23% against pasted. Issues: −31% to
      −19% against `gh`, −4% to +9% against pasted.
    - Pasted context keeps the last 200 lines of each failed job log and stops
      at 60,000 tokens.
    - In the pull request tasks no arm could see the pull request's own diff
      as of the cut-off.
    - The answer does not show that an agent then writes a better fix. It shows
      that the agent reaches the same understanding of where to work for less.
    - Reproduce with `scripts/benchmark_threads_context.py`.

## Reference

| Command | Does |
|---|---|
| `codenib threads sync N...` | Fetch threads and the threads they link to |
| `codenib threads sync --open-pulls [N]` | Index what open pull requests change |
| `codenib threads show N` | Print the context |
| `codenib threads context N` | Write the context to a file |
| `codenib threads open N` | Start a ticket and write its first context |
| `codenib threads inbox N` | Print what a ticket has not read; `--peek` leaves it unread |
| `codenib threads poll` | Fetch what changed on the threads tickets follow |
| `codenib threads board` | List tickets, unread counts and shared code |
| `codenib threads close N` | Stop following a ticket's threads |
| `codenib threads mcp` | Serve context and tickets over MCP |
| `codenib threads events ID...` | Print raw events by id |

| Option | Meaning | Default |
|---|---|---|
| `--budget` | Target size in tokens, source excluded. A thread's own posts stay whole up to three times this before they are shortened; linked threads are cut first | 3000 |
| `--code-budget` | Extra tokens for the source of referenced symbols | 1500 |
| `--since ID` | Return only what followed this event | |
| `--rev` | Commit that code locations are resolved against | `HEAD` |
| `--at TIME` | Serve the thread as it was at an ISO time | |
| `--sync` | Fetch from GitHub before serving | |
| `--offline` | Never call GitHub | |
| `--no-anchors` | Skip code locations | |
| `--repo` | `owner/name` | the `origin` remote |
| `--checkout` | Clone used to resolve code locations | `.` |
| `--store` | Where thread logs are kept | CodeNib home |

Thread logs are stored as files under `~/.codenib/threads/<owner>/<repo>/`.
Nothing is written into your checkout. A log is append-only: syncing twice
changes nothing, and an edited comment is kept beside the original.

Thread text is treated as data. CodeNib executes and follows nothing in a
comment, and only reads from GitHub. The text still reaches your agent, as it
would if the agent read the thread itself, so anyone who can comment on a
thread can address your agent. Give the agent no more permissions than you
would when it reads GitHub directly.
