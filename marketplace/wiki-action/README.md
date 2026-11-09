<!--
SPDX-FileCopyrightText: 2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib Wiki action

Publish a source-linked Wiki of your repository on GitHub Pages. The Wiki opens
as a map of the code's parts; every page explains what the code does and links
each claim to the exact lines. The code graph comes from compiler-grade
indexers, not from a model's guess.

```yaml
name: CodeNib Wiki
on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

jobs:
  wiki:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.wiki.outputs.page-url }}
    steps:
      - uses: actions/checkout@v6
        with:
          fetch-depth: 0
      - uses: sysevol-ai/wiki-action@v1
        id: wiki
        with:
          api-key: ${{ secrets.OPENROUTER_API_KEY }}
```

1. In **Settings > Pages**, choose **GitHub Actions** as the source.
2. Add an [OpenRouter](https://openrouter.ai/) key as the Actions secret
   `OPENROUTER_API_KEY`, with a spending limit on the key.
3. Push. The first run writes every page; later pushes rewrite only the pages
   whose source files changed. Run the workflow with `full-rebuild: true` to
   pick up new parts of the code.

Without `api-key`, set `wiki: index` for pages derived from the index alone.
Pages that fail their quality checks are left out and written again on the
next push. If the Overview fails, or fewer than 80% of the pages pass, the
index-derived pages are deployed instead, with a warning on the run.

Run `codenib pages badge` in the repository for a README badge that links to
the published Wiki.

## Publishing this action

This directory is the source of the `sysevol-ai/wiki-action` repository.
Marketplace lists an action only from the root of its own public repository,
which should hold only the action's own files. Publishing needs two-factor
authentication and the Marketplace Developer Agreement accepted for the
owning account; a listed action is not reviewed. To release it:

1. Replace `@main` in `action.yml` with the full commit SHA of the CodeNib
   release, keeping the version as a comment.
2. Copy `action.yml`, this README and `LICENSE` into the root of
   `sysevol-ai/wiki-action`, commit and tag `v1.x.y`, and move `v1` to it.
3. Draft a release from `action.yml`, tick **Publish this Action to the GitHub
   Marketplace**, and choose a primary category.
