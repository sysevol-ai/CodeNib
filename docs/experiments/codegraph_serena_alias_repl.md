# Pinned Serena REPL alias-reference diagnostic

Serena's public REPL returns import-name bindings when asked for references to
the original function in the existing exposed alias fixture. Opening all source
documents explicitly gives the same result. This checks the actual pinned
competitor interface; it does not establish a caller benchmark or task benefit.

## Observed behavior

The control uses the completed cohort's immutable prepared image, Serena 2.1.0,
Python 3.13.18 and Pyright 1.1.403. All 242 copied package files with hashes in
the installed wheel `RECORD` match it. This is not a new archive download or a
comparison against the latest release.

The original 24 files retain the same bytes and selected fingerprint. They are
mounted read-only; Serena's generated project metadata has a separate writable
mount and is retained with its original permissions. The owned container uses
`network=none`; no SDK credential mount or model session is provided.

| Public query | Observed result |
| --- | --- |
| Find the original `target` definition | Exact original function body. |
| Find its references before the explicit document-opening pass | Eight import-name references. |
| Repeat after opening all 21 Python/stub documents | The same eight import-name references. |
| Find references to the replacement function | Two import-name references. |
| Find references by the name `rebound` in its module | Rendered `ValueError`: no matching symbol. |
| Process that failed assignment's result | Rendered `NameError`; no reference result exists. |

The two execution errors have MCP `is_error=false`. Retain their actual text
and missing results rather than counting the transport flag as query success.
There are eleven REPL requests, including documentation and preparation, with
nine ordinary replies and these two errors.

Each returned origin/replacement point binds the complete visible `target`
token to its original source file. Unicode coordinates are explicitly verified
as UTF-16 and converted to UTF-8 for this diagnostic. All these points are
inside imports: none overlaps the eleven previously specified call tokens.
Adjacent calls in snippets receive no reference credit. This is an auxiliary
source-overlap observation, not an end-to-end retrieval or coding score.

## Decision and evidence

The direct REPL query does not compose transitive aliases on this fixture.
Serena permits further search and composition; this control does not evaluate
that complete workflow. Keep the earlier CodeNib graph observation, including
its post-rebinding false positive, unchanged. Establish a real cross-file
maintenance need before adding another provider or running paid efficacy rounds.

The [exact packet](codegraph_serena_alias_repl_2026_10_31.json) retains 51 public
records in two parts and 243 local, hash-bound installed files. It preserves
the failed stopped-container inspection check, its independent absence audit,
both packaging-controller failures, all actual replies and generated settings.
All five query-container processes are independently absent. Pyright children
are gone before removal, while the sleep process and MCP server remain; the
exact labelled container is then forcibly removed before source capture closes.
Individual exit codes are unrecorded, so no graceful-exit claim is made.

No model question runs, no previous cohort or criterion changes, and no
comparative efficiency or quality advantage is established. Independent
successes remain 0/2 and external enrollment remains 0/5.
