# AGENTS.md

CodeNib is a Python 3.10+ code analysis agent with tree-sitter chunking,
semantic graph construction, hybrid retrieval, and MCP-facing search tools.
Use this file as the repo-level contract for Codex and other code agents.

## Layered Objectives

Work in this order unless the user gives a narrower task:

1. Protect `main`: do not merge draft PRs, PRs with unresolved code failures, or
   stacked PRs whose dependency chain is not clear.
2. Make changes reviewable: keep commits focused, preserve existing ownership
   boundaries, and avoid unrelated refactors.
3. Verify the changed surface: run the smallest relevant test first, then widen
   to the job or marker tier that matches the risk.
4. Keep stacked work coherent: merge or rebase dependencies before dependents,
   and re-check diffs after a base branch changes.
5. Codify repeated feedback here or in the nearest directory-specific rules
   file so future agents do not repeat the same mistake.

For broad multi-step work, keep an explicit layered goal active until the
requested surface is implemented, locally verified, documented, and reconciled
with relevant PRs/issues. Do not mark a broad goal complete just because a
single subgoal landed. Prefer local targeted verification and continued
implementation over waiting on slow remote CI; run the remote/slow tier after
the merge queue or feature batch is otherwise coherent.

For the current multi-language SCIP cold-start and acceleration program, use
`docs/scip_multilanguage_roadmap.md` as the durable objective record. Update it
whenever a backend is promoted, a C++ acceleration gate changes, or a related
issue/PR is closed.

## Dev Commands

```bash
make dev          # pip install -e ".[dev,test]"
make test         # run the local unit tier; never makes billed API calls
make test-all     # run every marker tier, including external/slow tests
make test-slow    # run credential- and model-dependent slow tests explicitly
make bootstrap-ubuntu # install Ubuntu system deps, Python dev deps, and all local toolchains
make bootstrap    # install Python dev deps and local toolchains without sudo/apt
make multilang-tools # install active SCIP/LSP plus cold-start SCIP/LSP smoke tools
make toolchain-doctor # verify locally installed toolchain commands resolve on PATH
make scip         # install active SCIP toolchain under CODENIB_SCIP_TOOLS_DIR
make zoekt-tool   # install Zoekt binaries under CODENIB_SCIP_TOOLS_DIR
make web-deps     # install Next.js frontend dependencies
make scip-cold-start-system-deps-ubuntu # install Ubuntu base deps for cold-start tools
make scip-cold-start-tools # install Java/C#/JVM/Ruby/PHP SCIP smoke toolchains
make lsp-smoke-tools # install Java/C#/Ruby/PHP/Kotlin LSP smoke tools
make ruby-project-bundle PROJECT_ROOT=/path/to/ruby/repo # prepare Ruby overlay Gemfile/bundle
make php-project-scip-tool PROJECT_ROOT=/path/to/php/repo # optionally prewarm project-local scip-php
make core-build   # build the C++ core/ pybind acceleration module
make ask-quality  # run the deterministic Ask retrieval gate for this checkout
make scip-cold-start-smoke # run generated SCIP cold-start smoke
make scip-project-smoke # run SCIP smoke on PROJECT_ROOT/PROJECT_LANGUAGE
make lsp-smoke    # run generated Java/C#/Ruby/PHP/Kotlin LSP graph smoke
make lsp-project-smoke # run LSP smoke on PROJECT_ROOT/PROJECT_LANGUAGE
make graph-route-alignment # compare graph routes for PROJECT_ROOT/PROJECT_LANGUAGE
make multilang-registry-check # check registry and capability matrix consistency
make install      # pip install -e .
```

For Ruby toolchain installs, prefer the Makefile targets over ad hoc `gem`
commands. If the default system gem lacks Ruby headers, pass
`RUBY_GEM=/path/to/gem` to the same target instead of installing tools outside
`CODENIB_SCIP_TOOLS_DIR`.

Pre-commit uses black with line length 88, isort, flake8+bugbear, and
clang-format for C/C++.

## Git, Commit, And PR Rules

- Do not add AI attribution footers or generated-by text to commits, PR bodies,
  or review comments.
- Use Conventional Commits: `type(scope): summary`, with `type` in `feat`,
  `fix`, `docs`, `refactor`, `perf`, `test`, `chore`, or `ci`.
- Keep commit subjects imperative and at most 72 characters. Use the body for
  why the change was made and how it was verified.
- PR bodies must follow `.github/PULL_REQUEST_TEMPLATE.md` with the existing
  headings in order: `Summary`, `Changes`, `Type of Change`, `Testing`,
  `Checklist`.
- This repository allows squash and rebase merges, not merge commits. For
  stacked PRs, do not blindly squash each PR into `main`; first confirm the
  dependency chain and restack dependents so their diffs stay clean.
- Before merging, confirm the PR is not draft, is mergeable, has no failing
  required checks, and has all dependent changes merged or explicitly accounted
  for. Treat cancelled checks as incomplete, not green.
- Pin every external GitHub Action in workflows and composite actions to a
  full 40-character commit SHA, with the exact release tag retained in a
  trailing comment. Local actions under `./` do not need a commit pin.

## Testing Guidance

- Unit tier: `pytest -m "not slow and not integration and not integration_serial and not integration_serial_consumer" -x --tb=short`
- Integration tier: `pytest -m integration --tb=short`
- Serial integration tier: `pytest -m integration_serial -v --tb=short`
- Generic LSP graph smoke:
  `make lsp-smoke`
- SCIP cold-start smoke:
  `make scip-cold-start-smoke`
- Route-level backend alignment:
  `make graph-route-alignment PROJECT_LANGUAGE=java PROJECT_ROOT=/path/to/repo`
- Slow tier needs LLM credentials and GPU resources; inspect whether a red slow
  job is an infra/config failure before treating it as a code regression.
- New tests default to unit scope. Add heavier markers only when the test really
  needs external repos, SCIP indexing, LLM calls, GPU embeddings, or serial
  mutation.

## Critical Code Conventions

- `CodeChunk.start_line` and `CodeChunk.end_line` are 0-based tree-sitter line
  numbers.
- `CodeLocation.start_line` and `CodeLocation.end_line` are 1-based output
  locations. The conversion happens downstream in `dataset/gt_locate.py`.
- `.c` files use the `cpp` chunker; there is no separate C chunker.
- Graph node and edge types are centralized in `codenib/types.py`. Use the
  constants and helpers there instead of hard-coded string literals.
- Persisted graph schema changes require updating `_SCHEMA_VERSION` in
  `codenib/graph/code_graph.py` and checking C++ decoder parity under `core/`.
- New languages should flow through the language registry instead of adding
  isolated dispatch maps.

## Directory-Specific Rules

Codex does not automatically load `.claude/CLAUDE.md`, but those files remain
the authoritative local notes for several subtrees. Read the matching file
before editing the listed area:

| Path | Required extra context |
| --- | --- |
| `codenib/code_chunking/` | `codenib/code_chunking/CLAUDE.md` |
| `codenib/graph/` | `codenib/graph/CLAUDE.md` |
| `codenib/dataset/` | `codenib/dataset/CLAUDE.md` |
| `scripts/agent_compile/` | `scripts/agent_compile/CLAUDE.md` |
| `test/` | `test/CLAUDE.md` |

The project-wide Claude notes in `.claude/CLAUDE.md` are useful background, but
do not rely on Codex discovering them automatically.
