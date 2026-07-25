# test/ — rules

Mirrors the `codenib/` package layout. Loads on top of the project-wide
[`.claude/CLAUDE.md`](../.claude/CLAUDE.md).

## Marker tiers

| Marker | Scope | Duration |
|--------|-------|----------|
| _(none)_ | Unit — pure logic, mocks only | ~1 min |
| `integration` | External repos, **read-only / parallel-safe**: chunkers, fixture-based SCIP | ~2 min |
| `integration_serial` | **Mutates shared repos** (SCIP indexing, `process_instance`, `git checkout`/`apply`) — must run sequentially | ~25 min |
| `integration_serial_consumer` | Consumes the `graph.pkl` written by `integration_serial` (runs in a separate downstream job) | ~5 min |
| `slow` | LLM API calls, GPU embeddings | ~15 min |

```bash
pytest -m "not slow and not integration and not integration_serial and not integration_serial_consumer" -x  # unit only
pytest -m "integration and not slow"           # parallel-safe integration
pytest -m integration_serial                   # serial (repo-mutating)
pytest -m integration_serial_consumer          # graph.pkl consumers
pytest -m slow                                 # slow only
```

A new test defaults to the unit tier — only add a marker if it actually needs
the heavier deps, and pick the right tier: anything that *mutates* a shared
repo must be `integration_serial`, never plain `integration` — the
`integration` job runs parallel under pytest-xdist and a mutating test breaks
it. The CI jobs key off exactly these markers (`unit`, `integration`,
`integration-serial`, `graph-consumer`, `slow`; `scip-core` runs
`test/scip/test_scip_core.py` directly) — see
[`docs/ci_cd.md`](../docs/ci_cd.md) for the job chain.

## Fixtures & caches

- Repo fixtures cache clones under `${CODENIB_TEMP_DIR}/gt-test/`.
- HuggingFace dataset cache lives at `~/.codenib/`.

## Gotchas

- **Package shadowing**: a `test/<name>/__init__.py` whose `<name>` matches a
  top-level package (e.g. `scripts/`) shadows that real package under pytest's
  rootdir import mode. Two independently-green PRs can break once merged — when a
  test dir mirrors a top-level package name, revalidate the *merged* tree, not
  just per-PR CI.
- The `slow` job needs LLM API keys + a GPU; a red `slow` run is usually
  pre-existing infra flake, not a regression in your change — don't block merges
  on it without checking it failed for the same reason before your PR.
