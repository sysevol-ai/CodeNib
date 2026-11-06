<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Change guard

The guard tells a coding agent, and you, which tests a change can break before
the work is called done. It compares your working tree with a commit, finds the
functions you changed, and lists the tests that reach them through calls in
CodeNib's code graph, each with the call path that connects them. It runs on
your machine and calls no model.

Installed as a hook, it runs each time Claude Code or Codex is about to finish.
If the change reaches tests, the agent gets the list and the command that runs
them, and keeps working.

## How it decides what is at risk

- **Changed code.** Each changed line maps to the innermost function or method
  around it. A line outside any function counts as a change to its class, and a
  line outside any class counts as a change to every definition in the file.
- **Tests at risk.** A test is at risk when it reaches changed code in at most
  three calls. Calls include constructors: `Cart(...)` reaches `Cart.__init__`.
- **Why.** Each test comes with its path, such as
  `test_checkout_total (tests/test_checkout.py:6) -> checkout (shop/checkout.py:5) -> Cart.__init__`.
  The location after each name is the line where it makes the next call.

The graph is built from the commit you compare against, not from your edited
files, and kept for later runs. The first build of a commit takes from under a
second for a small project to a few minutes for one the size of Django.

## Add it to your agent

The guard is on CodeNib's main branch and ships in the next release. Until
then, install CodeNib from source; building it needs Python 3.10 or newer,
Git and Node.js 20 or newer:

```bash
git clone https://github.com/sysevol-ai/CodeNib && cd CodeNib
(cd web && npm ci && npm run build)
python -m pip install ".[graph]"
cd /path/to/repository
codenib guard install --agent claude      # or --agent codex, or --agent all
```

The installer adds two hooks and builds the graph of the current commit:

- **Session start** records the commit the session starts from.
- **Stop** runs the guard on the session's changes when the agent is about to
  finish. It reports each distinct change once, never on a turn that a Stop hook
  already continued, and it never fails the agent's turn.

Claude Code reads the hooks from `.claude/settings.local.json`; `--scope project`
writes the shared `.claude/settings.json` instead. Codex reads
`.codex/hooks.json` and runs a new hook only after you trust it in `/hooks`.
Remove the hooks with `codenib guard uninstall`.

## Check a change yourself

```bash
codenib guard check                 # working tree against HEAD
codenib guard check --base main     # everything since main
codenib guard check --json          # every test and path, for scripts
```

`--exit-code` makes the command exit with status 1 when any test is at risk,
for use in CI.

## Notes that stay attached to code

Some knowledge is not in the code: why a function is shaped the way it is, a fix
that was tried and reverted, an invariant other code relies on. A note records
it and names the functions it is about:

```bash
codenib guard note add "Cart copies its items, so callers can reuse their list." \
  --anchor shop/pricing.py:Cart.__init__ --kind invariant
```

Notes are Markdown files in `.codenib/notes/`, so they are reviewed and
versioned with the code. Each note stores a fingerprint of the code it is
anchored to. When a change touches that code, or a function it calls directly, the
guard shows the note. When the anchored code itself changes, the guard marks
the note as possibly out of date until someone checks it:

```bash
codenib guard note list                                    # every note and its state
codenib guard note confirm .codenib/notes/cart-copies-its-items-so-callers.md
```

Kinds are `decision`, `gotcha`, `invariant`, `contract` and `note`.

## How well it works

On 68 agent patches that pass SWE-bench Verified's tests but break other tests
in the project's full suite, the guard listed a broken test for 35. On patches
that broke nothing, it listed a median of 3 tests. Selecting every test file
that imports a changed module listed a broken test for 22, at a median of 81
tests. The method and data are in
[pull request #976](https://github.com/sysevol-ai/CodeNib/pull/976).

Most misses go through calls that a code graph cannot see: framework code that
calls methods by name, operators such as `==` and `[]`, and tests that start the
program in a separate process. A test the guard does not list can still fail.

??? note "Scope and privacy"
    - Python only, and tests are functions or methods named `test*` in test
      files. The suggested command uses pytest node IDs; other runners can run
      the same tests by name.
    - Files added since the base commit are not in the graph, so code reached
      only from them is not checked.
    - The hook sends nothing over the network. `codenib guard install
      --share-first-run` opts in to one anonymous event, the first time the
      hook reports, with no repository, path or code in it.
    - Graphs are kept under the CodeNib state directory, three commits per
      repository.
