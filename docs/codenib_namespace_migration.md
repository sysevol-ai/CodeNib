# CodeNib Namespace Migration

## Decision

`main` adopts **CodeNib** as its only maintained product and programmatic
namespace. The frozen artifact branch remains the reproducibility surface for
the former CodeMiner package, commands, environment variables, paths, and
experiment layouts. `main` does not carry dual-read aliases or deprecation
shims for those identifiers.

This supersedes the compatibility policy in issue #335. The migration is a
breaking release, not a display-only rename.

## Boundary

The following identifiers must move together:

| Surface | Old | New |
| --- | --- | --- |
| Python distribution | `codeminer` | `codenib` |
| Python package | `codeminer.*` | `codenib.*` |
| Public Python names | `CodeMiner*` | `CodeNib*` |
| Commands | `codeminer-*` | `codenib-*` |
| Environment variables | `CODEMINER_*` | `CODENIB_*` |
| Repository-local state | `.codeminer*` | `.codenib*` |
| User and temporary state | `~/.codeminer`, `/tmp/codeminer-*` | `~/.codenib`, `${TMPDIR}/codenib/*` |
| MCP and skill identifiers | `codeminer*` | `codenib*` |
| Native module and C++ namespace | `codeminer_core`, `codeminer::core` | `codenib_core`, `codenib::core` |
| Maintained scripts, tests, examples, and docs | `codeminer*` | `codenib*` |

The migration does not rewrite third-party source, Git history, or URLs and
checksums that identify frozen external releases. Project-controlled repository
and dataset identities now use CodeNib, so former product identifiers are not
allowlisted in maintained code, configuration, examples, or documentation.

## Milestones

### M0: Freeze the migration contract

- Record the baseline inventory and the external-identity exception rule.
- Replace the display-brand guard with a repository-wide namespace audit.
- Keep the implementation in a clean worktree based on `origin/main`.

Exit gate: the audit reports every legacy identifier by category and cannot
silently broaden its exception list.

### M1: Move the language namespaces

- Move `codeminer/` to `codenib/`.
- Change the distribution metadata and all Python imports.
- Rename public `CodeMiner*` APIs, package-bearing filenames, skill IDs, and
  the native `codenib_core` module/C++ namespace.
- Remove compatibility aliases and tests that require the old namespace.

Exit gates:

```bash
python -c "import codenib; from codenib.agent import CodeNibAgentOptions"
python -c "import importlib.util; assert importlib.util.find_spec('codeminer') is None"
pytest -m "not slow and not integration and not integration_serial and not integration_serial_consumer" -x --tb=short
make core-build
python -c "import codenib_core"
```

### M2: Move runtime identity and state

- Rename all `CODEMINER_*` inputs and Make variables to `CODENIB_*`.
- Move cache, config, workspace, temporary, and generated paths to CodeNib
  names without fallback reads from old locations.
- Rename MCP server/prompt IDs, skills, logger names, user agents, and emitted
  metadata.
- Review persisted manifests and graph/vector payloads. Change embedded product
  identifiers and bump a schema only when the serialized structure or declared
  identity actually changes.

Exit gate: runtime tests prove that only the new environment variables and
paths affect behavior.

#### Persisted-format decision

The rename does not change the field layout of repository manifests, graph
payloads, LSP occurrence indexes, or incremental vector state. Their schema
versions therefore remain unchanged. Pickle-bearing caches that may encode the
former Python module path are not migrated in place: CodeNib writes to new
state roots and never reads the former roots. Reproducing those frozen
artifacts remains the responsibility of the artifact branch. This keeps a
format version tied to payload structure while still enforcing a clean runtime
break on `main`.

### M3: Move repository integration

- Rename every maintained CLI entry point and remove old commands.
- Update scripts, Make targets and variables, workflows, cache keys, Docker
  inputs, web integration, examples, and developer instructions.
- Rename maintained files whose basename contains the old product name.
- Update source headers from `CodeMiner Contributors` to
  `CodeNib Contributors`.

Exit gates:

```bash
python -m build
python -m pip install --force-reinstall --no-deps dist/codenib-*.whl
codenib-mcp --help
codenib-web --help
make multilang-registry-check
python scripts/check_namespace.py
```

### M4: Prove the clean break

- Run focused package, CLI, MCP, compiler, graph, agent, web, and native-core
  tests before the broad unit tier.
- Run relevant integration and serial tiers after the branch is coherent.
- Inspect wheel contents and entry-point metadata.
- Scan tracked text, filenames, and generated configuration for residual old
  identifiers; only exact external identities may remain.
- Reconcile review comments and CI, then merge the migration PR.

Completion requires all gates to pass on the same commit. A green narrow test
or a display-only scan is not evidence that the namespace migration is done.

## Baseline

At `origin/main` commit `4c8299db6d0843bc4294949bf089b0ba9ef598ad`,
excluding `third_party/` and lock files:

- 3,942 legacy-name matches;
- 739 matched files out of 817 searched;
- 2,522 lowercase `codeminer` matches in 473 files;
- 526 uppercase `CODEMINER` matches in 69 files.

The implementation branch is `feat/codenib-namespace`.
