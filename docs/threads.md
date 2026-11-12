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
  tell whether it still has to read the thread.
- **The ask.** The title and body of the issue or pull request.
- **Code locations.** Paths, stack frames, review positions and symbol names
  from the thread, resolved to files, symbols and line ranges at a commit, with
  the source of the symbols that matter most.
- **Failing CI.** The lines around each error. Jobs that fail the same way
  appear once.
- **Also in progress on this code.** Open pull requests, outside this thread,
  that change the same symbols or source files. An agent working one thread
  cannot otherwise see that someone else is changing the same function.
- **Linked threads.** Each linked issue or pull request with its state, what
  it changed and how it was reviewed, followed by what people wrote there.
- **Discussion and review comments**, newest kept first when the budget is
  tight.

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

### See who else is changing the same code

Index the open pull requests of the repository, and each context then names
the ones that change the same code:

```bash
codenib threads sync --open-pulls        # the 100 most recently updated
```

Run it again to refresh. In `astral-sh/ruff`, 30 of the 59 most recently
updated open pull requests changed a symbol that another of them also changed
(November 2026).

### With dsh-superagent

[dsh-superagent](https://github.com/deepseek-ai/dsh-superagent) runs a prepare
command once before a worker starts on a Ticket. End that command with the
script from this repository, after the Ticket directory holds a checkout:

```bash
sh /path/to/CodeNib/plugins/dsh/superagent-thread-context.sh
```

It reads the Ticket's issue or pull request from the variables dsh-superagent
provides, writes `THREAD_CONTEXT.md` into the Ticket directory, and keeps the
file out of the worker's commits. It never fails the Ticket: without a GitHub
thread, or when GitHub cannot be reached, it explains and exits cleanly. Tell
workers to read the file first, for example in the repository's `AGENTS.md`.

dsh-superagent removes token variables from the prepare command's
environment. The script then uses the `gh` that dsh-superagent already signs
in.

This step has been run the way dsh-superagent's source runs a prepare command
(its variables, a clean environment, the parent directory as working
directory, its check of the Ticket directory afterwards), not inside a running
dsh-superagent.

### When an agent comes back

Each context ends with a cursor, the id of the newest event it saw. Pass it
back to get only what happened since: new comments and reviews, a status
change, new check results.

```bash
codenib threads show 812 --sync --since '#812/comment/2990023933'
```

### Check a citation

```bash
codenib threads events '#812/comment/2990023933'
```

## How well it works

With the same Codex agent, the prepared context led to the same answers as
reading threads with `gh`, for about a quarter fewer input tokens. The saving
grows with the size of the thread.

The agent was asked which files and symbols have to change, and its answer was
compared with what the merged pull request changed. Three ways of getting
thread context were compared: the agent reads threads itself with `gh`; every
thread event is pasted into the prompt unprocessed; or the prompt carries the
`codenib threads` context.

| Task | Tasks | Files found: `gh` / pasted / CodeNib | Input tokens against `gh` | Against pasted |
|---|---|---|---|---|
| Take over a pull request after review or a failed check | 76 | 0.74 / 0.75 / 0.74 | −23% | −34% |
| The same, threads over 20,000 tokens | 22 | 0.63 / 0.65 / 0.65 | −36% | −54% |
| Take over an issue before any fix exists | 47 | 0.82 / 0.85 / 0.86 | −24% | +4% |

On short threads, pasting everything costs as little as the prepared context.
The prepared context pulls ahead once reviews, linked pull requests and CI
logs make the thread long.

For an agent that returns to a thread, a first context followed by updates
took a median of 3,010 tokens over the life of an issue, against 27,799 for
reading the whole thread each time (103 issues, a median of 6 returns each).

??? note "Scope and method"
    - Repositories: `astral-sh/ruff`, `caddyserver/caddy` and
      `pydantic/pydantic` for pull requests; the first two for issues.
    - Each task shows the thread as it stood at a cut-off, and a snapshot of
      the repository at that point with no history, so no arm can read the fix.
      A read-only replica of `gh` limited to the cut-off is available in every
      arm.
    - Each task ran once per arm. Intervals are 95% paired bootstrap over
      tasks. Pull requests: −29% to −16% against `gh`, −42% to −25% against
      pasted. Issues: −32% to −15% against `gh`, −5% to +14% against pasted.
    - A blind grader compared each plan for an issue with the merged fix and
      found the same approach in 0.64, 0.68 and 0.64 of the 47 tasks.
    - Pasted context keeps the last 200 lines of each failed job log and stops
      at 60,000 tokens.
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
| `codenib threads events ID...` | Print raw events by id |

| Option | Meaning | Default |
|---|---|---|
| `--budget` | Token limit for everything except source | 3000 |
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

Thread text is treated as data. Nothing in a comment is executed or followed,
and CodeNib only reads from GitHub.
