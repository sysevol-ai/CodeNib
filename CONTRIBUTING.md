<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Contributing To CodeNib

CodeNib accepts bug reports, design proposals, documentation fixes, and focused
code changes. Open an issue before starting a broad architectural change so its
scope and ownership boundary are explicit.

## Development Setup

```bash
git clone https://github.com/sysevol-ai/CodeNib.git
cd CodeNib
make dev
pre-commit install
```

Toolchain-heavy graph work may also need:

```bash
make bootstrap
make toolchain-doctor
```

## Change Workflow

1. Create a focused branch from current `main`.
2. Add or update tests at the lightest tier that exercises the behavior.
3. Run the targeted test first, then the matching wider tier.
4. Run `pre-commit run --all-files`.
5. Open a PR using the repository template and include exact verification
   commands.

Use Conventional Commit subjects such as
`fix(cli): reject missing manifests`. Keep unrelated refactors out of the same
change. Do not commit generated caches, credentials, model weights, or
repository-local `.codenib_cache` artifacts.

## Test Tiers

The default unit tier is:

```bash
pytest -m "not slow and not integration and not integration_serial and not integration_serial_consumer" -x --tb=short
```

Run heavier tiers only when the changed surface requires them:

```bash
pytest -m integration --tb=short
pytest -m integration_serial -v --tb=short
make lsp-smoke
make scip-cold-start-smoke
```

Tests marked slow may require LLM credentials, GPU resources, external
repositories, or language toolchains. A failing infrastructure prerequisite
should be reported separately from a code regression.

## Code And Documentation

- Python targets 3.10+ and is formatted with Black at line length 88.
- Use the language registry and centralized graph types instead of adding
  isolated dispatch maps or string literals.
- Persisted graph schema changes require a schema-version bump and C++ decoder
  parity review.
- User-facing commands and docs use CodeNib's canonical package and repository
  names.

See [AGENTS.md](AGENTS.md) for the full repository contract and
[Contributing a Language](docs/contributing-a-language.md) for backend work.

## License

By submitting a contribution, you agree that it may be distributed under the
repository's [Apache License 2.0](LICENSE).
