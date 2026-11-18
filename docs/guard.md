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

!!! note "Preview"
    The guard is a preview in CodeNib 0.2.6. It is measured on Python; Go, Rust,
    JavaScript and TypeScript are covered with the limits listed under
    [Languages](#languages). It lists
    tests a change can reach; it has not been shown to reduce the regressions
    agents introduce. See [How well it works](#how-well-it-works).

## How it decides what is at risk

- **Changed code.** Each changed line maps to the innermost function or method
  around it. A line outside any function counts as a change to its class, and a
  line outside any class counts as a change to every definition in the file.
- **Tests at risk.** A test is at risk when it reaches changed code in at most
  three calls. Calls include constructors: `Cart(...)` reaches `Cart.__init__`.
  A call through a name that a module republishes, such as
  `date_input = _main.date_input`, reaches the function it names.
  A call of a method through its base class reaches every override of that
  method in a subclass. Code that uses a class reaches the methods its
  operators run, such as `__eq__` for `==` and `__getitem__` for `[]`. A call
  such as `node.as_sql()` on a value of unknown type reaches each method of
  that name, when the project defines at most three.
- **Why.** Each test comes with its path, such as
  `test_checkout_total (tests/test_checkout.py:6) -> checkout (shop/checkout.py:5) -> Cart.__init__`.
  The location after each name is the line where it makes the next call.
- **Edited tests.** An existing test whose own lines the change rewrote now
  checks the new behaviour, so it passing shows little. The guard lists these
  tests first, with a command that runs them as they were at the base commit
  and then restores your edits. A test added next to an existing one does not
  count as an edit of it.

The graph is built from the commit you compare against, not from your edited
files, and kept for later runs. The first build of a commit takes from under a
second for a small project to a few minutes for one the size of Django.

## Add it to your agent

The guard ships in CodeNib 0.2.6. Until 0.2.6 is available from PyPI, install
CodeNib from source; building it needs Python 3.10 or newer, Git and Node.js 20
or newer:

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

## Comment on pull requests

A reviewer reads the diff and cannot see what the diff leaves out. The impact
action adds one comment to a pull request that says, for each definition the
change edits:

- **Callers outside the change.** Code that calls the edited definition and was
  not edited itself, each linked to the line of the call. These are the places
  a changed signature or return value can break.
- **Tests that reach it.** How many tests reach the definition within three
  calls, grouped by test file, with one call path per file and the command that
  runs them. "None found" marks a definition the graph sees no test for.
- **Rewritten tests.** Existing tests whose own lines the change rewrote.

The comment is read from the code graph of the commit the pull request branched
from. No model writes it and no key is needed. It is updated in place on every
push, and a change that edits no source definition gets no comment.

```yaml
name: CodeNib impact
on: pull_request

permissions:
  contents: read
  pull-requests: write

jobs:
  impact:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
        with:
          fetch-depth: 0
      - uses: sysevol-ai/CodeNib/.github/actions/impact@main
```

The first run on a base commit builds its graph, which takes from seconds to a
few minutes; later runs on the same base reuse it and take seconds. A pull
request from a fork runs with a read-only token, so its report appears in the
job summary instead of a comment.

To see the same comment locally:

```bash
codenib guard check --base main --markdown
```

??? note "Action inputs"
    | Input | Default | Meaning |
    |---|---|---|
    | `repository-path` | `.` | Checkout of the pull request |
    | `base` | the pull request's base | Revision to compare with |
    | `comment` | `true` | `false` writes the job summary only |
    | `token` | the workflow token | Token that writes the comment |
    | `depth` | `3` | Calls between a test and the change |
    | `python-version` | `3.12` | Python that builds the graph |
    | `cache` | `true` | Keep the base commit's graph between runs |

    Edits outside any definition, such as imports and module constants, are
    not traced to callers.

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

## Languages

The walk from a change to the code that calls it is the same in every
language. What differs is what counts as a test and how to run it.

| Language | A test is | Suggested command |
|---|---|---|
| Python | a function or method named `test*` in a test file | `python -m pytest <ids>` |
| Go | `Test*`, `Benchmark*`, `Fuzz*` or `Example*` in a `_test.go` file | `go test ./dir -run '^(A\|B)$'`, with `-bench` for benchmarks |
| Rust | a function marked `#[test]` (also `tokio::test`, `rstest`) | `cargo test --workspace -- <names>` |
| JavaScript, TypeScript | a whole test file | none: the project picks its runner |

The graph must be built for the language, which needs its provider installed
(`codenib toolchain install`). A repository with several languages is guarded
for those whose provider is present.

Only Python has been measured on real regressions. On one commit each of a
real project: Go (gin) listed 39 of 454 tests for a changed function; Rust
(byteorder) saw only the 2 tests written as plain functions, because tests
generated by a macro are not definitions in the graph; JavaScript (preact)
listed none, because its graph lacked the calls between modules. Constructor,
alias, base-class, operator and unknown-receiver reach are Python only.

## How well it works

On 68 agent patches that pass SWE-bench Verified's tests but break other tests
in the project's full suite, the guard listed a broken test for 42. On patches
that broke nothing, it listed a median of 13 tests. Selecting every test file
that imports a changed module listed a broken test for 22, at a median of 81
tests. The guard shows the ten tests nearest the change first; a broken test
was among those ten for 18 of the 68. Without operators and calls of unknown
type the guard listed a broken test for 38, at a median of 11. The method and
data are in pull requests
[#976](https://github.com/sysevol-ai/CodeNib/pull/976) and
[#1118](https://github.com/sysevol-ai/CodeNib/pull/1118).

A list is only useful if the agent acts on it. In an A/B run on 24 Django tasks
chosen because earlier agents broke hidden tests on them, Claude Code with the
guard, with no hook and with a hook naming the full test suite each left one
hidden regression, on the same task. That cohort gave the guard almost nothing
to catch, so it shows no difference either way. In that one case the guard
listed all eight broken tests, but the agent had rewritten them to expect its
new behaviour; such edited tests now come first in the list. The setup and
results are in
[pull request #1010](https://github.com/sysevol-ai/CodeNib/pull/1010).

Most misses go through calls that a code graph cannot see: framework code that
calls methods by name or runs registered callbacks, and tests that start the
program in a separate process. Others reach the change, but in more than three
calls. A test the guard does not list can still fail.

??? note "Scope and privacy"
    - Python tests are functions or methods named `test*` in test files. The
      suggested command uses pytest node IDs; other runners can run
      the same tests by name.
    - Files added since the base commit are not in the graph, so code reached
      only from them is not checked.
    - The hook sends nothing over the network. `codenib guard install
      --share-first-run` opts in to one anonymous event, the first time the
      hook reports, with no repository, path or code in it.
    - Graphs are kept under the CodeNib state directory, three commits per
      repository.
