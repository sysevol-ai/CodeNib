# test/ — rules

Mirrors the `codenib/` package layout. Loads on top of the project-wide
[`.claude/CLAUDE.md`](../.claude/CLAUDE.md).

## Marker tiers

| Marker | Scope | Duration |
|--------|-------|----------|
| _(none)_ | Unit — pure logic, mocks only | ~1 min |
| `integration` | Repo cloning, SCIP indexing, chunkers | ~15 min |
| `slow` | LLM API calls, GPU embeddings | ~15 min |

```bash
pytest -m "not slow and not integration" -x   # unit only
pytest -m integration                          # integration only
pytest -m slow                                 # slow only
```

A new test defaults to the unit tier — only add `@pytest.mark.integration`
or `@pytest.mark.slow` if it actually needs the heavier deps. The three CI jobs
key off exactly these markers.

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
