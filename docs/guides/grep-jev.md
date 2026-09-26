<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Search your repository with grep and Jev

Ask a repository question without building an index or downloading an embedding
model. CodeNib uses your OpenRouter account to plan local `rg` searches, then
Jev ranks the matching code blocks. Results include verified source excerpts,
file paths, line numbers, actual model IDs, and reported API usage.

**Source-checkout preview:** these commands are not in the published 0.2.3
package. Use a checkout containing this feature. The released
[CodeGraph setup](../codegraph.md) remains available for model-free search and
typed graph navigation.

## Run a query

Install [ripgrep](https://github.com/BurntSushi/ripgrep#installation) so that
`rg` is on your PATH. From your CodeNib checkout:

```bash
python -m pip install -e ".[grep,mcp,auth]"
codenib explore /path/to/your/repository "Where is retry backoff implemented?"
```

Before querying, run `codenib auth login` to connect OpenRouter and save the key
in your OS credential store. See [OpenRouter authorization](openrouter.md) for
the browser, SSH, import, and disconnect paths. An environment variable also
works; this Bash prompt keeps its value out of shell history:

```bash
read -rsp 'OpenRouter API key: ' OPENROUTER_API_KEY; echo
export OPENROUTER_API_KEY
```

Use a dedicated key with a credit limit from
[OpenRouter settings](https://openrouter.ai/settings/keys). This path does not
upload the key to a CodeNib service. It sends the key directly to OpenRouter
over HTTPS; it does not persist it in the repository, results, or MCP config.
The local process and its environment can read it. Environment credentials
override a saved login; unset the variable to use your OS-stored key instead.

There is no graph, embedding model, GPU, or repository build step. Supported
source languages use the [language registry](../language_capabilities.md).
The `grep` installation extra adds the HTTP client; `mcp` adds the shared
response format and stdio server. Install both for these CLI commands.

## Connect your agent

Start the same route as an MCP server:

```bash
codenib mcp /path/to/your/repository --retrieval-route grep-jev
```

Register that command with your agent's MCP settings, using the absolute path
to the installed executable. For clients using `mcpServers` JSON:

```json
{
  "mcpServers": {
    "codenib": {
      "command": "/absolute/path/to/venv/bin/codenib",
      "args": ["mcp", "/absolute/path/to/repository", "--retrieval-route", "grep-jev"]
    }
  }
}
```

Run `codenib auth login` on the machine where the MCP process runs, or launch
the agent from an environment containing `OPENROUTER_API_KEY`. Do not paste a
key into this JSON. This mode exposes one tool, `explore_context`.
Ask your agent to call it with a precise repository question and cite the
returned source. Each call reads the current checkout, so edits between calls
do not require reindexing. Changes during a call invalidate source delivery.

`symbols`, `direction`, and `include_dependencies` do not enable graph
navigation in this mode. Choose the indexed [MCP route](../mcp.md) when you need
call relationships or definition/reference navigation. The response's
`route_unavailable` diagnostic makes that distinction explicit.

## What leaves your machine

The planning request contains your question, the repository directory name,
and a bounded directory/file-count overview. It contains no source bodies.
The scoring request contains your question and selected source snippets,
including file and symbol names. Both go to OpenRouter and its model providers.
Local search does not make this an offline or local-model workflow.

CodeNib applies its repository source selection, production path filters and
test exclusions before planning or scoring. It reuses saved source exclusions
when a manifest exists, skips internal symlinked files, rejects escaping
symlinks, and excludes tests by default;
`--include-tests` opts them in. These filters are not a secret scanner. Review
your source policy before using this route with private code.

## Calls, cost, and failure behavior

The default planner is `anthropic/claude-sonnet-4.6`; Jev uses
`typesafe/jev-1.13`. `--planner-model` can select another OpenRouter model that
supports JSON-schema output. Changing the planner changes the retrieval method;
the published experiment does not establish quality for every model.

One query permits at most six grep actions, 100 distinct candidate blocks,
3,000 characters per candidate, and ten candidates per Jev call: at most
one planning call and ten scoring calls. Searchable source is limited to
20,000 files and 256 MiB. Individual files over 10 MiB and minified files are
skipped. Before retaining the chunk corpus, the route also limits it to
50,000 chunks and 32 Mi characters of visible chunk text. Exceeding either
bound stops before planning or scoring. Each grep action has a ten-second
limit and bounded output. Planned regexes can span lines; each covered line
is mapped to its source chunk, with at most 500 matched lines retained per
action. A multiline pattern does not trigger a second planning call or retry.

`--max-cost-usd` defaults to `0.10`. It stops **subsequent** calls when reported
usage reaches that amount. An in-flight call can exceed it, and failed calls
can have unknown cost. Set a provider-side credit limit for a billing cap.
`--request-timeout` defaults to 90 seconds; cancellation/deadline checks stop
later work, while an in-flight HTTP request finishes or reaches its transport
timeout. The tool's `budget` argument limits returned context, not API spend.

Inspect `plan.retrieval.provider_calls`, `reported_cost_usd`, and
`unreported_call_cost` in the result. On an OpenRouter error, missing usage,
invalid search plan or exhausted budget, this route stops without automatic
retry or a BM25 fallback. A retrieval failure has an explicit diagnostic;
the CLI exits with status 1. Known usage remains in the failed retrieval plan.
Source mutation rejects the response; any calls already sent can still be
billed. Cancelling an MCP call prevents later calls and does not commit its
result to the agent session.

## Interpreting the quality claim

The [100-issue experiment](../evaluation/grep_jev.md) measured this method's
research implementation, not this product preview or agent task completion.
The product retains its planner prompt/schema, candidate limits, and scoring
criteria, while adding source verification, cancellation, failure reporting,
and corrected visible line ranges. A full production-route evaluation remains
necessary before claiming the same quality or recommending it as the default.
