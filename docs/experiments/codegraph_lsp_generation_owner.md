# Keep a language-server owner until cleanup finishes

The shared LSP client now bounds teardown and preserves a process that still
needs cleanup. This repairs a production lifecycle boundary used by Python
compiler identity, incremental patching and generic LSP indexing. Separate
virtual-source controls recover an exposed alias call, but still fail the
rebinding fixture; live Python MCP routing remains unpromoted.

The [terminal packet](codegraph_lsp_generation_owner_2026_10_31.json) retains
the original failures, both candidate versions, source-only controls and exact
installed outputs. Large provider logs, payloads, wheels and graph artifacts
remain local with byte/hash identities. No previous model outcome or frozen
400-query result is rescored.

## Production ownership and protocol

A client must retain its one process until exit is observed and its pipes
close. Clearing `self.process` is the release point. A failed kill/wait keeps
that owner and its transport state reachable for explicit cleanup retry;
`start()` cannot replace it. Lifecycle calls remain single-caller operations.
This introduces no new lock, backend, registry or persistence system.

Teardown no longer enters ordinary request backpressure. Its handshake uses
nonblocking pipe writes under one ten-second deadline, followed by five-second
exit and kill/wait budgets. A failed final wait raises with ownership retained;
it cannot fall through to an unbounded wait and erase the process. Closing
stdin discards any buffered bytes before closing the wrapper, so cleanup does
not try another blocking flush after process exit. These are transport phase
budgets, not complete-task latency or a process-tree isolation guarantee.

A missing/null initialize result now fails startup instead of announcing
readiness. Before an initialize result arrives, cleanup sends only `exit`;
after initialization it requests shutdown and sends exit. This follows the
[official initialization contract](https://github.com/microsoft/language-server-protocol/blob/gh-pages/_specifications/lsp/3.17/general/initialize.md),
whose source identity is retained. It does not claim full response-schema
validation. Startup cleanup preserves its initiating failure and attaches an
unreleased owner when cleanup also fails; a new interruption keeps priority.

Eight new regressions reproduce against unchanged merged source. They cover
an unreaped owner/retry, duplicate start, a real server after startup failure,
retained startup/cleanup errors, full request backpressure, missing initialize
result, an actual unread full stdin pipe, and real shutdown/null reply/exit
ordering with pipe closure. The pipe test has an independent watchdog so its
unchanged-code reproduction cannot hang the suite. Existing transport tests
still verify frames and late replies; their fresh-server fixture explicitly
releases its old owner before starting the replacement.

All **16** related cases and the final full local unit tier pass: **9,025
passed, 24 skipped, 221 heavier cases deselected and 46 existing warnings**,
with process-local umask `022`. The first lifecycle fixture omits `PIPE` from
its module-local subprocess double; retain that setup failure. After correcting
the fixture, all seven initial cases fail against base, and all eight final
cases independently fail against the exact unchanged module. A later check
also reproduces false readiness on the first repair before its correction.
The nearest test rules now require constants/exceptions in such doubles.

## Installed existing consumer

Both candidate versions receive separate developer-assembled wheels. Each
changes only the current `lsp_client.py` from the admitted predecessor and
regenerates RECORD; all 485 packaged Python files and native bytes verify.
They retain admitted native/frontend assets and are not source-built,
CI-produced or publicly published candidates. The final wheel SHA-256 is
`94486fd910729504a713e52663549372f7c47a282d8caee780eb9ab08bacd9f1`.

Each installed replay uses the normal Python post-SCIP pipeline on the same
complete raw artifact and source `8639802df6a6d3ba6f118c7a1a1aba535404bab4`.
It runs outside the analyzed checkout in the prepared offline two-CPU/4-GiB
container. An original-source authority brackets execution and verifies its
exit before publication. Pass-through start/shutdown wrappers record the
original provider's lifecycle, without replacing replies or graph results.

The real basedpyright 1.39.8 process initializes and exits with code zero;
all three pipes close and the client releases its owner. Both replays preserve
the **entire previous admission object**, apart from the added lifecycle
observations: exact normalized text, 33,178 nodes, 159,676 edges, all 786,078
ordered occurrences, query surface, complete source-selection report and both
artifact receipt objects. Raw compiler input remains unchanged and no compiler
generation or MCP/model task runs. Both exact owned containers are
independently absent.

## Alias and source-generation controls

The separate virtual-source prototype extends references only along real
`from ... import ... as ...` tokens. Each admitted location and alias query
must resolve to the same physical definition; same-name text alone is never
enough. One retained source read session encloses expansion, normalization and
the final publication check. Query/alias caps yield explicit partial results.
This wrapper remains outside product code and MCP routing.

A prospectively frozen generated project retains all 24 files, including a
binary asset and 21 Python/stub files. Its eleven positive calls and five
negative calls cover import chains, reexports, a cycle, wildcard use, local
imports, multiline syntax, non-BMP/Unicode columns, shadowing and rebinding.
Separate checks exercise project `extraPaths`, a selected type-stub package
and a real host-only dependency that the virtual filesystem must not resolve.
Expected positions stay outside provider inputs.

| Retained control | Observed outcome |
| --- | --- |
| Initial alias/lifecycle control | 10/11 positive calls, 0/5 negative calls included; overall gate fails. A visited-alias bookkeeping error falsely marks additional results partial. |
| Separate corrected diagnostic | Same 10/11 and 0/5; configuration/stub and full-source alias gates pass. The genuine rebinding uncertainty remains partial, so the overall gate still fails. |
| Change after a reply | A selected non-Python file changes while the source session is held; publication is rejected. The original raw reply stays retained and unpublished. |
| Query after that change | Stale rejection occurs before another RPC. |
| Explicit rename/line shift/Unicode recapture | Old owner closes before the new generation; source guards work, while the same rebinding semantic gate still fails. |
| Paused owned actor | Kernel state confirms the actor is stopped. Failed graceful cleanup is retained; bounded termination/kill reaps it within the prospectively fixed eight-second gate. |

In the rebinding case, the language server resolves the imported alias to
both the original function and a later assignment. The prototype refuses
to guess and misses one required call before that assignment. This is a
retained failed gate, not permission to drop the case, relax correctness or
present partial results as complete. A product path needs a proven resolution
or a source-current existing-graph fallback that includes its real cost.

On the unchanged complete **2,390-file/1,172-Python** source, the separate
corrected wrapper recovers the exposed `profile_main(...)` call. Its five
token locations differ from the compiler's four spans because the import name
and alias are separate tokens. This does not revise the earlier 346/400 or
354/400 exact-set outcomes, establish complete graph parity, or provide another
independent validation sample. All original structured replies and deliveries
verify; all eight actor owners are independently absent. Node `vm` remains
explicitly outside a security-sandbox claim.

## Current product decision

Ship the reviewed shared-client lifecycle repair. Keep the alias/source owner
experimental: its positive rebinding gate fails, its behavior differs from
compiler ranges, and its configuration/dependency checks cover only the named
fixture. Actual MCP position routing still prepares the persisted graph; this
work does not silently bypass that route or its source-selection guard.

Merged [#942](https://github.com/sysevol-ai/CodeNib/pull/942) now passes main
Docs build/deploy and all twenty executed release verifiers, with five tag-only
publication skips. Public package 0.2.4 is unchanged. This turn adds no model
question or participant. Whole-workflow correctness/time/token benefit,
voluntary useful calls, two independent successes and external repeat use
remain open; successes stay 0/2 and enrollment/reuse 0/5.
