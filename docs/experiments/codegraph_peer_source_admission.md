# Peer startup and source-query admission

Serena and `colbymchenry/codegraph` can initialize unattended and return the
complete source body needed by the original lookup task on each of the three
pinned pilot repositories. These are model-free controls with preinstalled,
warm dependencies. They do not establish cold-start cost, voluntary tool use,
answer quality or a task-time improvement.

The [archive index](codegraph_peer_source_admission_2026_10_28.json) retains all
twelve controls, their controllers, actual replies, stderr and source hashes.
Seven controls pass and five fail; these development attempts are not a
comparative success-rate sample. The original sixty model tasks, failed setups,
timings and assessment remain unchanged.

## Source-bound lookup checks

Both products return the complete selected body at these 1-based inclusive
locations. Serena reports 0-based body locations; the archive retains its
original coordinates and the first-line character mapping.

| Pinned repository | Original lookup target | Source location | Serena | Codegraph |
| --- | --- | --- | --- | --- |
| Requests `3c88e520da24ae6f736929a750876e7654accc3d` | `Session.prepare_request` | `requests/sessions.py:260–297` | Pass | Pass |
| Flask `7ee9ceb71e868944a46e1ff00b506772a53a4f1d` | `Flask.process_response` | `src/flask/app.py:1996–2022` | Pass | Pass |
| pytest `8aba863a634f40560e25055d179220f0eefabe9a` | `CallInfo.from_call` | `src/_pytest/runner.py:219–233` | Pass | Pass |

The targets come from the unchanged
[original lookup tasks](codegraph_matched_tasks_v1.json). The additional
`Flask.make_response` checks below serve a different query and remain separate.
No new benefit protocol or inference cohort is frozen by these controls.

## Unattended preparation

Serena uses `serena-agent 2.1.0`, Pyright `1.1.403` and server MCP `2.2.0` in the
previously prepared arm64 image. Project creation explicitly selects Python:
`serena project create --language python --name <unique-name> <workspace>`.
This avoids the mixed-language detection prompt that produced the original
pytest setup EOF. Eager full-project indexing is optional and is not added to
this startup path.

The first new pytest attempt creates that configuration but fails to start its
language server: uv attempts a PyPI lookup in the network-disabled container,
even though the pinned Pyright package is cached. The retained stderr identifies
the failed lookup and actual launch command. With `UV_OFFLINE=1`, the same cached
Pyright version starts and all three lookup checks pass. Each attempt uses a
fresh `SERENA_HOME`; the uv cache remains the prepared image's cache. This is a
preparation correction, not evidence of an intrinsic Serena indexing failure.

Serena's actual MCP mode remains `claude-code` context with the v2 REPL. The
controller calls `initial_instructions` and then `s.lsp.find_symbol` with the
relative file, symbol path and body requested. A nominal `is_error:false` alone
does not pass admission: the reply must contain the requested body and match
the pinned source.

Codegraph uses the pinned `1.6.2` arm64 runtime, `codegraph init --yes`, and
`codegraph serve --mcp --path <workspace>`. Default daemon behavior remains
enabled. Its `codegraph_explore` query contains the method name and source path.
All visible source rows are checked against their own file and captured source
version; the selected lookup body must be completely visible.

The runs use separate owned containers with networking disabled, two CPUs,
4 GiB memory and a 256-process limit. Only the attempt's source checkout is
bind-mounted. Inherited image workspace volumes are masked with empty tmpfs
mounts. The archive records exact image IDs, mounts, commands and timings.
Those timings are warm developer diagnostics, not matched cold-start or
comparative task results.

## Retained failures and coverage limits

All five failed controls retain their original terminal status:

| Control | Observed failure | Interpretation |
| --- | --- | --- |
| Serena pytest 1 | Pyright's uv launcher attempts a network lookup | Cached offline startup must be configured explicitly |
| Serena pytest 2 | Controller expects indentation before the first symbol token | Controller mapping error; the returned body is preserved |
| Codegraph Requests 1 | Controller treats a renderer's final empty row as a physical source line | Controller mapping error; no source credit is assigned to that extra row |
| Codegraph Flask 1 | The same empty-row mapping error stops validation | This early failure does not establish complete method delivery |
| Codegraph Flask 2 | The `make_response` reply contains references and selected source, but omits all 139 lines of the requested method | Successful transport and a named reference do not establish source-body coverage |

Later Serena checks remove only the known indentation preceding the first
symbol token from the source comparison; every subsequent byte must match.
Later Codegraph checks record final empty renderer rows explicitly outside
source credit. Neither correction replaces an earlier failed receipt.

The Flask `process_response` lookup passes for both peers. That is the target
of the original lookup task, and its success does not resolve Codegraph's
separate `make_response` omission. Retain selected-source gaps and native
follow-up reads in any future complete task cost and assessment.

## Preservation and remaining admission

All original tracked file hashes and Git HEADs remain unchanged. Newly created
`.serena` and `.codegraph` metadata are retained and visible in recorded Git
status; the workspaces are not described as completely unchanged. A separate
post-run audit covers failed controllers that ended before their own source
check. All fourteen owned control/help/diagnostic containers are confirmed
absent. All sixty-three earlier tracked CodeGraph JSON artifacts remain
byte-identical to source baseline `bc62135b`.

Complete the common runtime/source binding, installation and cold/update
preparation for every arm before another matched run. Source-check the full
corrected rubric and freeze a separate prospective model/tool/resource
protocol. Preserve native tools, automatic optional-tool choice, failures,
unused tools, complete task costs and unknown invoice charges. These controls
add no model inference and enroll no external users. The 20% benefit gate and
five-user week-long reuse gate remain open under
[#874](https://github.com/sysevol-ai/CodeNib/issues/874).
