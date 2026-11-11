---
description: Install CodeNib and prepare this repository's search index and code graph.
disable-model-invocation: true
---

Set CodeNib up for this repository and report each step's result.

1. Check for the CLI with `codenib --version`. If it is missing, install it
   with whichever of these the user already has, in this order:
   `uv tool install "codenib[graph,mcp]"`,
   `pipx install "codenib[graph,mcp]"`, or
   `python -m pip install "codenib[graph,mcp]"`. CodeNib needs Python 3.10 or
   later. Ask before installing anything.
2. From the repository root, install the language providers the code graph
   needs with `codenib toolchain install . --scope graph`, then build the
   search index and the graph with `codenib index . --preset graph`. The
   indexes are stored under `~/.codenib`, not in the checkout. If a provider
   needs something the project must prepare itself, such as `node_modules` or
   `compile_commands.json`, report that rather than changing the project.
3. Run `codenib doctor --require mcp --require graph` and report anything it
   marks as missing.
4. Tell the user to run `/mcp`, choose `plugin:codenib:codenib` and reconnect
   it, so the server picks up the new graph.
