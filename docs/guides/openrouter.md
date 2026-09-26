<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Connect your OpenRouter account

CodeNib's local grep → Jev route can use your OpenRouter account without
pasting a key into your repository or MCP configuration. Authorization and
model requests go directly from your machine to OpenRouter. A CodeNib hosted
service does not receive the key.

**Source-checkout preview:** these commands are not in PyPI 0.2.3. From a
checkout containing the feature, install the local authorization support:

```bash
python -m pip install -e ".[grep,mcp,auth]"
codenib auth login
```

The command opens OpenRouter in your browser. Review the account and key
settings there, authorize, and return to the terminal. CodeNib uses
[OpenRouter's PKCE flow](https://openrouter.ai/docs/guides/overview/auth/oauth)
with SHA-256 and a temporary localhost callback. The returned key is saved in
your OS credential store. CLI and MCP queries then use that saved login:

```bash
codenib explore /path/to/repository "Where is retry backoff implemented?"
codenib mcp /path/to/repository --retrieval-route grep-jev
```

For automatic Claude Code/Codex registration, use
`codenib init /path/to/repository`. It starts this same authorization flow when
you have no credential, checks an existing key, and registers the local MCP
command without copying credentials into agent configuration. See
[grep and Jev](grep-jev.md) for setup and retrieval behavior.

## SSH, containers, and existing keys

When a browser cannot reach the CLI's localhost listener:

```bash
codenib auth login --headless
```

Open the printed OpenRouter URL on your own device, authorize, and paste the
single-use authorization code into the hidden terminal prompt. The code cannot
be exchanged without this login attempt's verifier. The provider gives codes
a ten-minute lifetime; the CLI defaults to a five-minute attempt, configurable
with `--timeout` up to 600 seconds. A failed exchange is not retried.

To use an existing dedicated API key, enter it through a hidden prompt:

```bash
codenib auth login --import-key
```

Do not supply the key as a command argument. CodeNib checks key metadata and
rejects management/provisioning keys. It asks for a normal inference key; it
does not need permission to create or manage your other keys.

If an OS credential store is unavailable, automatic plaintext fallback is
disabled. You can explicitly choose local file storage:

```bash
codenib auth login --headless --store file
```

This fallback is **unencrypted** and POSIX-only. It uses a private directory
(0700) and regular file (0600), rejects symlinks/hardlinks and Git repository
locations, and atomically replaces the saved value. The default location is
`~/.codenib/credentials/openrouter.json`, under `CODENIB_HOME` if configured.
Your OS user and local processes running as that user can read it. Use the
OS keyring when available. CodeNib does not silently select third-party
plaintext keyring backends.

For automation, `OPENROUTER_API_KEY` remains supported. Credential precedence
is environment, explicitly saved file, then OS keyring. An environment value
overrides a saved login; unset it when switching accounts. Remove a saved file
before switching back to keyring storage.

## Check limits and disconnect

```bash
codenib auth status
codenib auth status --remote
codenib auth logout
```

Local status reports the active credential source. `--remote` also checks key
validity and credit usage on OpenRouter and supplies a link to that key's
settings. It does not print the key. Configure a provider credit limit there;
an OAuth-issued key is not automatically short-lived or restricted to one
model. CodeNib's per-query spending threshold stops subsequent calls and is
not a provider-enforced billing cap.

Logout removes local saved copies. Use `--store file` or `--store keyring` to
remove only that copy. It cannot change the parent shell's environment,
cancel an already running request, or revoke a provider key. To revoke,
delete the key in [OpenRouter settings](https://openrouter.ai/settings/keys).
The logout result explicitly reports that provider revocation has not occurred
and whether an environment key is still present. If the OS keyring is locked,
local removal reports the failure instead of claiming success.

Default logout removes credentials from the available local stores and lists
unavailable backends in `unavailable_stores`; those backends were not inspected.
An explicit `--store keyring` request fails when no supported backend exists.
File logout can remove a damaged credential without parsing it, and preparation
or logout recovers private temporary keys left by an interrupted file save.
These operations serialize with active saves so cleanup does not remove a
writer's temporary key. Unsafe linked entries cause a removal error.

## Source and credential boundaries

The planner receives your question and a directory overview. Jev receives your
question and selected code snippets, file names, and symbol names. Both use
remote model providers. Read [what leaves your machine](grep-jev.md#what-leaves-your-machine)
before connecting private code.

The login verifier lives only for the current process, the callback listens
only on loopback, and callback URLs are not logged. The browser confirmation
clears the grant from its address bar and loads no external resources. Model
results and MCP configuration contain no credential. The login key itself
remains in the chosen credential store until removed or revoked.

This page describes local CLI/MCP authorization. A hosted Wiki browser trial
has a separate trust boundary and is not enabled by these commands.
